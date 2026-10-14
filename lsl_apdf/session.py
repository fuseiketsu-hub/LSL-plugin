"""会话编排层：把数据源 → 滤波 → RMS → 标定 → APDF → 导出 串成一条流水线。

这是整个插件对外的主要入口（GUI / CLI / 你自己的管道都调用它）::

    cfg = AppConfig()
    session = AcquisitionSession(cfg, LSLSource(cfg.stream))
    session.open()                     # 连接流、建立滤波器与导出目录
    session.start_calibration()        # 强制 %MVE 标定（3 次 MVC）
    while running:
        session.step(timeout=0.05)     # 采集 + 处理 + 落盘
    summary = session.close()          # 写 summary.json

线程模型
--------
``step()`` 只做很短的处理（除阻塞等待数据的那一段），状态更新与
``snapshot()`` 共用一把锁；GUI 在另一个线程里按 10 Hz 取快照即可拿到一致的
视图（波形、RMS、APDF、标定进度、统计）。写盘由 ``SessionWriter`` 的独立线程
负责，采集路径不碰磁盘。
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .apdf import ApdfAccumulator, ApdfResult
from .calibration import (
    CalibrationOutcome,
    CalibrationPhase,
    CalibrationState,
    MvcCalibrator,
)
from .config import AppConfig
from .dsp import FilterDesign, RmsExtractor, StreamingFilter, design_filter_chain
from .recording import ProcessedOutlet, SessionPaths, SessionWriter
from .sources import SampleSource, StreamMeta

__all__ = ["AcquisitionSession", "SessionSnapshot", "SessionStats"]


# --------------------------------------------------------------------------- #
# 环形缓冲（只用于显示，不参与统计）
# --------------------------------------------------------------------------- #
class _Ring:
    """定长环形缓冲：保留最近 ``capacity`` 个样本，用于界面显示。"""

    def __init__(self, capacity: int):
        self.capacity = int(max(1, capacity))
        self._x = np.empty(0, dtype=float)
        self._t = np.empty(0, dtype=float)

    def append(self, t: np.ndarray, x: np.ndarray) -> None:
        if x.size == 0:
            return
        self._t = np.concatenate([self._t, np.asarray(t, dtype=float).reshape(-1)])
        self._x = np.concatenate([self._x, np.asarray(x, dtype=float).reshape(-1)])
        if self._x.size > self.capacity:
            self._t = self._t[-self.capacity :]
            self._x = self._x[-self.capacity :]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return self._t.copy(), self._x.copy()

    def clear(self) -> None:
        self._x = np.empty(0, dtype=float)
        self._t = np.empty(0, dtype=float)


# --------------------------------------------------------------------------- #
# 快照数据结构
# --------------------------------------------------------------------------- #
@dataclass
class SessionStats:
    elapsed_sec: float = 0.0
    n_samples: int = 0
    n_chunks: int = 0
    srate_measured: float = 0.0
    n_rms_windows: int = 0
    n_apdf_samples: int = 0
    gap_events: int = 0
    gap_sec: float = 0.0
    last_chunk_age_sec: float = 0.0
    writer: dict[str, int] = field(default_factory=dict)
    dropped_writer_items: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "elapsed_sec": round(self.elapsed_sec, 3),
            "n_samples": self.n_samples,
            "n_chunks": self.n_chunks,
            "srate_measured": round(self.srate_measured, 3),
            "n_rms_windows": self.n_rms_windows,
            "n_apdf_samples": self.n_apdf_samples,
            "gap_events": self.gap_events,
            "gap_sec": round(self.gap_sec, 4),
            "writer": dict(self.writer),
        }


@dataclass
class SessionSnapshot:
    """给界面/回调使用的一致视图（所有数组都是拷贝）。"""

    connected: bool = False
    meta: StreamMeta | None = None
    unit_label: str = "a.u."
    mve_ref: float | None = None
    channel: int = 1
    filter_description: str = ""

    raw_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    raw_y: np.ndarray = field(default_factory=lambda: np.empty(0))
    rms_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    rms_value: np.ndarray = field(default_factory=lambda: np.empty(0))
    rms_level_pct: np.ndarray = field(default_factory=lambda: np.empty(0))

    latest_rms: float = float("nan")
    latest_level_pct: float = float("nan")
    apdf: ApdfResult | None = None
    calibration: CalibrationState | None = None
    stats: SessionStats = field(default_factory=SessionStats)
    messages: list[str] = field(default_factory=list)
    synthetic: bool = False


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
class AcquisitionSession:
    """一次采集会话：从 LSL（或合成/回放源）到 APDF 与导出文件。"""

    def __init__(
        self,
        cfg: AppConfig,
        source: SampleSource,
        writer: SessionWriter | None = None,
        create_writer: bool = True,
    ):
        self.cfg = cfg
        self.source = source
        self._lock = threading.RLock()

        self.meta: StreamMeta = StreamMeta()
        self.filter_design: FilterDesign | None = None
        self.unit_label: str = cfg.signal.unit_label

        self._filter: StreamingFilter | None = None
        self._rms: RmsExtractor | None = None
        self._accumulator = ApdfAccumulator(cfg.apdf)
        self._calibrator = MvcCalibrator(cfg.calibration, unit_label=self.unit_label)
        self._outlet: ProcessedOutlet | None = None

        self.paths: SessionPaths | None = None
        self.writer = writer
        self._owns_writer = False
        if self.writer is None and create_writer and cfg.export.enabled:
            self.paths = SessionPaths.create(
                cfg.export.root_dir,
                prefix=cfg.export.session_prefix,
                subject_id=cfg.subject_id,
            )
            self.writer = SessionWriter(self.paths, cfg.export)
            self._owns_writer = True

        self._display_raw = _Ring(20000)
        self._display_rms = _Ring(20000)

        self._t0: float | None = None
        self._local_t0: float | None = None
        self._wall_t0: float | None = None
        self._last_chunk_wall: float | None = None
        self._last_ts: float | None = None
        self._srate_measured = 0.0
        self._gap_events = 0
        self._gap_sec = 0.0
        self._messages: list[str] = []
        self._opened = False

        self._apdf: ApdfResult | None = None
        self._last_apdf_n = 0
        self._last_apdf_time = 0.0
        self._data_time = 0.0
        self._last_apdf_data_t = -1e9
        self._last_snapshot_data_t = -1e9
        self._apdf_callbacks: list[Callable[[ApdfResult], None]] = []
        self._unit_hint_done = False
        self._closed = False

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def open(self) -> StreamMeta:
        """连接数据源、建立滤波器与导出目录。可重复调用以更换数据源。"""
        self.meta = self.source.connect()
        for warning in getattr(self.source, "warnings", []):
            self._messages.append(warning)
        chan = self.cfg.signal.channel
        if self.meta.n_channels and chan > self.meta.n_channels:
            raise ValueError(
                f"Config uses channel {chan}, but the stream has only {self.meta.n_channels} channels"
            )

        # 单位标签：优先用 LSL 元数据（说明文档 §3.2 会解析出 unit）
        meta_unit = self.meta.unit_for(chan, fallback="") if self.meta.n_channels else ""
        if meta_unit and self.cfg.signal.unit_label in ("a.u.", ""):
            self.unit_label = meta_unit
        else:
            self.unit_label = self.cfg.signal.unit_label

        self.filter_design = design_filter_chain(
            fs=self.source.srate,
            highpass_hz=self.cfg.signal.highpass_hz,
            lowpass_hz=self.cfg.signal.lowpass_hz,
            notch_hz=self.cfg.signal.notch_hz,
            notch_q=self.cfg.signal.notch_q,
            order=self.cfg.signal.filter_order,
        )
        self._filter = StreamingFilter(self.filter_design)
        self._rms = RmsExtractor(
            fs=self.source.srate,
            window_sec=self.cfg.signal.rms_window_sec,
            hop_sec=self.cfg.signal.rms_hop_sec,
        )
        self._calibrator.unit_label = self.unit_label

        # 显示缓冲按真实采样率定容（说明文档 §6：采样率要读流声明，勿写死）
        srate = max(self.source.srate, 1.0)
        self._display_raw = _Ring(
            int(max(1.0, self.cfg.display_window_sec) * srate * 1.2) + 1024
        )
        self._display_rms = _Ring(int(600.0 / max(self.cfg.signal.rms_hop_sec, 1e-6)))

        # 重连时清空运行时状态，但保留已累积的 APDF（会话内换流不该丢数据）
        self._t0 = None
        self._local_t0 = None
        self._last_ts = None
        self._last_chunk_wall = None
        self._srate_measured = 0.0
        self._unit_hint_done = False
        self._silence_reported = False
        self._filter.reset()

        if self.cfg.mve_ref is not None:
            self._messages.append(
                f"Loaded MVE reference {self.cfg.mve_ref:.4g} {self.unit_label} from config; calibration skipped"
            )

        if self.writer is not None:
            self.writer.start()
            assert self.paths is not None
            self.writer.write_now(
                self.paths.config_json,
                _config_json(self.cfg, self.meta, self.filter_design, self.unit_label),
            )
            self.writer.log(f"Session started | stream: {self.meta.describe()}")
            self.writer.log(f"Filter chain: {self.filter_design.describe()}")

        if self.cfg.export.publish_processed:
            try:
                self._outlet = ProcessedOutlet(
                    self.cfg.export.publish_name,
                    srate=1.0 / max(self.cfg.signal.rms_hop_sec, 1e-6),
                )
                self._outlet.open()
                self._messages.append(
                    f"Published processed stream {self.cfg.export.publish_name} (LSL)"
                )
            except Exception as exc:
                self._outlet = None
                self._messages.append(f"Failed to publish processed stream: {exc}")

        self._wall_t0 = time.time()
        self._opened = True
        self._closed = False
        return self.meta

    def close(self, duration_note: str = "") -> dict[str, Any]:
        """停止导出、写 summary.json，返回汇总 dict。"""
        summary: dict[str, Any] = {}
        with self._lock:
            if self._closed:
                return self._summary(duration_note)
            self._closed = True
            if self._outlet is not None:
                self._outlet.close()
                self._outlet = None
            try:
                self.source.close()
            except Exception:
                pass
            summary = self._summary(duration_note)

        if self.writer is not None:
            if self.paths is not None:
                self.writer.submit_json(self.paths.summary_json, summary)
                self.writer.log("Session ended")
            self.writer.stop()
        return summary

    # ================================================================== #
    # 标定
    # ================================================================== #
    def start_calibration(self) -> None:
        """开始（或重新开始）%MVE 标定。"""
        with self._lock:
            self._calibrator.start()
            self._messages.append(
                f"Calibration started: rest {self.cfg.calibration.rest_sec:g}s + "
                f"{self.cfg.calibration.mvc_trials} MVC trials "
                f"({self.cfg.calibration.mvc_sec:g}s each)"
            )

    def skip_calibration_phase(self) -> None:
        """手动跳到下一标定阶段（例如提前结束 MVC）。"""
        with self._lock:
            self._calibrator.skip_phase()
            self._after_calibration_locked()

    def abort_calibration(self) -> None:
        with self._lock:
            self._calibrator.abort()
            self._messages.append("Calibration aborted; APDF unavailable until an MVE reference is set")

    def set_mve_ref(self, value: float, source: str = "manual") -> None:
        """手动设定 MVE 参考值（跳过或修正标定结果）。"""
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError("MVE reference must be a positive finite value")
        with self._lock:
            self.cfg.mve_ref = float(value)
            self._apdf = None
            self._last_apdf_n = 0
            self._last_apdf_data_t = -1e9
            self._messages.append(
                f"MVE reference set manually to {float(value):.4g} {self.unit_label} (source: {source})"
            )
            if self.writer is not None and self.paths is not None:
                self.writer.submit_json(
                    self.paths.calibration_json,
                    {"mve_ref": float(value), "unit_label": self.unit_label,
                     "source": source, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                )
                self.writer.log(f"Manual MVE reference: {float(value):.6g}")

    @property
    def calibration_state(self) -> CalibrationState:
        with self._lock:
            return self._calibrator.state()

    @property
    def calibration_outcome(self) -> CalibrationOutcome | None:
        with self._lock:
            return self._calibrator.outcome

    def _after_calibration_locked(self) -> None:
        """标定结束时记录结果、刷新 APDF。"""
        outcome = self._calibrator.outcome
        if outcome is None or self._calibrator.phase is not CalibrationPhase.DONE:
            return
        if not math.isfinite(outcome.mve_ref):
            self._messages.append("Calibration produced no valid MVE reference")
            return
        self.cfg.mve_ref = float(outcome.mve_ref)
        self._apdf = None
        self._last_apdf_n = 0
        self._last_apdf_data_t = -1e9
        self._messages.append("Calibration complete -> " + outcome.summary_line())
        for warn in outcome.warnings:
            self._messages.append("Calibration warning: " + warn)
        if self.writer is not None and self.paths is not None:
            self.writer.submit_json(self.paths.calibration_json, outcome.to_dict())
            self.writer.log("Calibration complete: " + outcome.summary_line())
            for warn in outcome.warnings:
                self.writer.log("Calibration warning: " + warn)

    # ================================================================== #
    # 主循环
    # ================================================================== #
    def step(self, timeout: float = 0.05) -> bool:
        """拉取一块数据并走完整条流水线。返回 True 表示处理了数据。"""
        chunk = self.source.read_chunk(timeout=timeout)
        now = time.time()
        if chunk is None or len(chunk) == 0:
            with self._lock:
                self._last_chunk_wall = self._last_chunk_wall or now
                self._check_stream_silence_locked(now)
            return False

        with self._lock:
            self._process_chunk(chunk, now)
        return True

    def run(
        self,
        duration: float | None = None,
        on_tick: Callable[[SessionSnapshot], None] | None = None,
        tick_interval: float = 0.1,
    ) -> dict[str, Any]:
        """阻塞式采集循环（CLI / 无界面场景）。``duration=None`` 表示一直采到 Ctrl+C。"""
        start = time.time()
        next_tick = 0.0
        try:
            while True:
                self.step(timeout=0.05)
                now = time.time()
                if on_tick is not None and now >= next_tick:
                    on_tick(self.snapshot())
                    next_tick = now + tick_interval
                if duration is not None and (now - start) >= duration:
                    break
        except KeyboardInterrupt:
            self._messages.append("Received Ctrl+C; stopping acquisition early")
        return self.close()

    # ------------------------------------------------------------------ #
    def _process_chunk(self, chunk, now: float) -> None:
        ts = chunk.timestamps
        if ts.size == 0:
            return
        if self._t0 is None:
            self._t0 = float(ts[0])
            self._local_t0 = now
        if self._last_ts is not None and self.source.srate > 0:
            dt_expected = 1.0 / self.source.srate
            gap = float(ts[0] - self._last_ts) - dt_expected
            if gap > 3.0 * dt_expected:
                self._gap_events += 1
                self._gap_sec += gap
                self._messages.append(
                    f"Data gap detected: {gap * 1000:.0f} ms (cumulative {self._gap_sec:.2f} s)"
                )
        self._last_ts = float(ts[-1])
        span = self._last_ts - (self._t0 or self._last_ts)
        if span > 0.5:
            self._srate_measured = (self.source.n_samples - 1) / span
        self._last_chunk_wall = now

        chan = self.cfg.signal.channel
        try:
            raw = chunk.channel(chan)
        except IndexError as exc:
            self._messages.append(str(exc))
            return

        # 1) 单位换算（仅影响显示与绝对值；%MVE 结果与换算无关）
        physical = (raw - self.cfg.signal.adc_offset) * self.cfg.signal.adc_scale

        # 2) 滤波
        assert self._filter is not None and self._rms is not None
        filtered = self._filter.process(physical)

        # 3) 实时导出原始信号（全部通道）
        if self.writer is not None:
            local_times = self._local_t0 + (ts - self._t0) if self._local_t0 else np.zeros_like(ts)
            self.writer.submit_raw(ts, local_times, chunk.data)

        # 4) 增量 RMS
        rms_values, rms_times = self._rms.push(filtered, ts)

        t_rel = rms_times - self._t0
        skip = self.cfg.signal.startup_skip_sec
        usable = t_rel >= skip
        if np.any(usable):
            rms_values = rms_values[usable]
            rms_times = rms_times[usable]
            t_rel = t_rel[usable]
            self._ingest_rms(rms_values, rms_times, t_rel, now)

        # 5) 显示缓冲
        self._display_raw.append(ts, physical)
        if rms_values.size:
            self._display_rms.append(rms_times, rms_values)

        # 6) 统计
        self._stats_elapsed = now - (self._wall_t0 or now)
        self._check_unit_hint_locked(physical)

    def _ingest_rms(
        self,
        rms_values: np.ndarray,
        rms_times: np.ndarray,
        t_rel: np.ndarray,
        now: float,
    ) -> None:
        cal_active = self._calibrator.phase.active
        if cal_active:
            for value, t in zip(rms_values, rms_times):
                self._calibrator.push_amplitude(float(value), float(t))
            if self._calibrator.phase is CalibrationPhase.DONE:
                self._after_calibration_locked()
            if not self.cfg.include_calibration_in_apdf:
                pass  # 标定段不计入任务 APDF
            else:
                self._accumulator.add(rms_values)
        else:
            self._accumulator.add(rms_values)

        # 实时落盘 RMS（含当前 MVE 下的 %MVE 负荷）
        if self.writer is not None:
            mve = self.cfg.mve_ref
            level = rms_values / mve * 100.0 if mve else None
            self.writer.submit_rms(rms_times, rms_values, level)

        # 处理后信号回发（供下游插件订阅）
        if self._outlet is not None and self.cfg.mve_ref:
            level = rms_values / self.cfg.mve_ref * 100.0
            result = self._apdf
            for i in range(rms_values.size):
                static = result.static_level if result else float("nan")
                median = result.median_level if result else float("nan")
                peak = result.peak_level if result else float("nan")
                self._outlet.push([level[i], static, median, peak], float(rms_times[i]))

        # 用**数据时间**而不是墙钟节流：快速回放/离线跑批时行为与实时一致
        self._data_time = float(t_rel[-1])
        self._maybe_recompute_apdf(now)

    # ------------------------------------------------------------------ #
    def _maybe_recompute_apdf(self, now: float) -> None:
        if self.cfg.mve_ref is None or self._accumulator.n_samples == 0:
            return
        n = self._accumulator.n_samples
        data_dt = self._data_time - self._last_apdf_data_t
        grew_enough = n >= self._last_apdf_n + max(1, self._last_apdf_n // 50)
        forced = data_dt >= 3.0
        if self._apdf is not None and not (data_dt >= 0.25 and (grew_enough or forced)):
            return

        result = self._accumulator.compute(
            self.cfg.mve_ref,
            window_sec=self.cfg.signal.rms_window_sec,
            hop_sec=self.cfg.signal.rms_hop_sec,
        )
        self._apdf = result
        self._last_apdf_n = n
        self._last_apdf_time = now
        self._last_apdf_data_t = self._data_time

        if self.writer is not None and (
            self._data_time - self._last_snapshot_data_t
        ) >= self.cfg.export.apdf_snapshot_interval_sec:
            self._last_snapshot_data_t = self._data_time
            self.writer.submit_apdf(result, now)

        for cb in list(self._apdf_callbacks):
            try:
                cb(result)
            except Exception as exc:  # pragma: no cover - 回调不应影响采集
                self._messages.append(f"APDF callback error: {exc}")

    def add_apdf_callback(self, callback: Callable[[ApdfResult], None]) -> None:
        """注册 APDF 更新回调 —— 下游插件（如疲劳/DPI 切换）的接入点。"""
        self._apdf_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    def _check_stream_silence_locked(self, now: float) -> None:
        if self._last_chunk_wall is None:
            return
        silence = now - self._last_chunk_wall
        if silence > 2.0 and not getattr(self, "_silence_reported", False):
            self._silence_reported = True
            self._messages.append(
                f"No data received for {silence:.0f} s: is OpenSignals still recording?"
            )
        elif silence <= 1.0:
            self._silence_reported = False

    def _check_unit_hint_locked(self, physical: np.ndarray) -> None:
        """首秒数据到手后，如果幅值量级像 ADC 码而单位却写成 mV，给一条提示。"""
        if self._unit_hint_done or physical.size == 0:
            return
        if self.source.n_samples < max(500, int(self.source.srate * 0.5)):
            return
        self._unit_hint_done = True
        if self.unit_label == "mV" and float(np.max(np.abs(physical))) > 500.0:
            self._messages.append(
                "Hint: the stream declares mV, but the amplitude magnitude looks like raw ADC codes. "
                "To display real mV, set adc_scale in the config (see the presets in units.py). "
                "(%MVE normalization is unaffected)"
            )

    # ================================================================== #
    # 快照
    # ================================================================== #
    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            raw_t, raw_y = self._display_raw.arrays()
            rms_t, rms_x = self._display_rms.arrays()
            mve = self.cfg.mve_ref
            level = rms_x / mve * 100.0 if mve else np.full(rms_x.shape, np.nan)
            stats = SessionStats(
                elapsed_sec=getattr(self, "_stats_elapsed", 0.0),
                n_samples=self.source.n_samples,
                n_chunks=self.source.n_chunks,
                srate_measured=self._srate_measured,
                n_rms_windows=self._rms.emitted if self._rms else 0,
                n_apdf_samples=self._accumulator.n_samples,
                gap_events=self._gap_events,
                gap_sec=self._gap_sec,
                last_chunk_age_sec=(
                    time.time() - self._last_chunk_wall if self._last_chunk_wall else 0.0
                ),
                writer=self.writer.stats() if self.writer else {},
            )
            return SessionSnapshot(
                connected=self.source.connected,
                meta=self.meta,
                unit_label=self.unit_label,
                mve_ref=mve,
                channel=self.cfg.signal.channel,
                filter_description=(
                    self.filter_design.describe() if self.filter_design else ""
                ),
                raw_t=raw_t,
                raw_y=raw_y,
                rms_t=rms_t,
                rms_value=rms_x,
                rms_level_pct=level,
                latest_rms=float(rms_x[-1]) if rms_x.size else float("nan"),
                latest_level_pct=float(level[-1]) if level.size else float("nan"),
                apdf=self._apdf,
                calibration=self._calibrator.state(),
                stats=stats,
                messages=list(self._messages[-12:]),
                synthetic=self.meta.source_id == "lsl_apdf.synthetic",
            )

    # ------------------------------------------------------------------ #
    @property
    def apdf(self) -> ApdfResult | None:
        with self._lock:
            return self._apdf

    def current_apdf(self) -> ApdfResult | None:
        """立即重算并返回当前 APDF（用于"导出快照"按钮）。"""
        with self._lock:
            if self.cfg.mve_ref is None:
                return None
            if self._accumulator.n_samples == 0:
                return None
            self._apdf = self._accumulator.compute(
                self.cfg.mve_ref,
                window_sec=self.cfg.signal.rms_window_sec,
                hop_sec=self.cfg.signal.rms_hop_sec,
                use_cache=False,
            )
            self._last_apdf_n = self._accumulator.n_samples
            if self.writer is not None:
                self.writer.submit_apdf(self._apdf, time.time())
            return self._apdf

    def reset_accumulator(self) -> None:
        """清空 APDF 累积（例如"从此刻开始算任务段"）。"""
        with self._lock:
            self._accumulator.clear()
            self._apdf = None
            self._last_apdf_n = 0
            self._last_apdf_data_t = -1e9
            self._messages.append("APDF accumulator cleared; stats restart from now")

    def _summary(self, note: str = "") -> dict[str, Any]:
        stats = self.snapshot().stats
        outcome = self._calibrator.outcome
        payload: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            "stream": {
                "name": self.meta.name,
                "type": self.meta.type,
                "hostname": self.meta.hostname,
                "n_channels": self.meta.n_channels,
                "nominal_srate": self.meta.nominal_srate,
                "channel_labels": self.meta.channel_labels,
                "channel_units": self.meta.channel_units,
            },
            "analysis_channel": self.cfg.signal.channel,
            "unit_label": self.unit_label,
            "filter": self.filter_design.describe() if self.filter_design else "",
            "rms_window_sec": self.cfg.signal.rms_window_sec,
            "rms_hop_sec": self.cfg.signal.rms_hop_sec,
            "stats": stats.summary(),
            "mve_ref": self.cfg.mve_ref,
            "apdf": self._apdf.summary() if self._apdf else None,
            "calibration": outcome.to_dict() if outcome else None,
            "messages": list(self._messages),
            "files": {
                "raw_csv": str(self.paths.raw_csv) if self.paths else None,
                "rms_csv": str(self.paths.rms_csv) if self.paths else None,
                "apdf_curve_csv": str(self.paths.apdf_curve_csv) if self.paths else None,
                "percentiles_csv": str(self.paths.percentiles_csv) if self.paths else None,
                "calibration_json": str(self.paths.calibration_json) if self.paths else None,
                "config_json": str(self.paths.config_json) if self.paths else None,
            },
            "notes": [
                "APDF convention: X axis = amplitude level (%MVE), Y axis = exceedance "
                "probability P (share of time above the level). static/median/peak map to P=90/50/10%.",
                "Calibration segments are excluded from the task APDF by default (include_calibration_in_apdf=False).",
            ],
        }
        return payload

    # ------------------------------------------------------------------ #
    @property
    def messages(self) -> list[str]:
        with self._lock:
            return list(self._messages)


def _config_json(
    cfg: AppConfig, meta: StreamMeta, design: FilterDesign | None, unit_label: str
) -> str:
    payload = cfg.to_dict()
    payload["_runtime"] = {
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stream": meta.describe(),
        "stream_nominal_srate": meta.nominal_srate,
        "filter": design.describe() if design else "",
        "analysis_unit": unit_label,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
