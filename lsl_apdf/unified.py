"""统一插件：把"信号侧"与"执行侧"合成**一个进程**。

与之前的"集成"（两个进程 + TCP）的区别
--------------------------------------
====================  ==========================  ==========================
                     集成（两进程 + TCP）          统一插件（本模块）
====================  ==========================  ==========================
进程                  2 个，启动顺序敏感            1 个
通信                  TCP 127.0.0.1:9000 + 文本协议  直接函数调用
时间轴                我的 LSL 时钟 / 插件的模拟时钟  同一个时钟
配置                  2 份（需保证同源）             1 份
肌肉名错配            能发生（收不到就静默无动作）    不可能（同一个对象）
端口冲突              可能                          不存在
====================  ==========================  ==========================

复用的库（不重写）
------------------
* 信号侧：``channels.MultiChannelMvc``（多通道 %MVC）、``apdf``（APDF 核心）；
* 执行侧：插件的 ``rawaccel_bridge``（写 Raw Accel）、``emg_server.BalancePolicy``
  （高暴露区域比较 + 交替判定 + 死区/确认/死时间/安全阀）、``schedule`` 不涉及。

产物（同一个会话目录）
----------------------
``pctmvc.csv``（%MVC 时序）、``apdf_summary.csv``（每肌肉 APDF，单位 %MVC）、
``balance_events_<ts>.csv``（交叉/切换事件）、``sens_log_<ts>.csv``（感度时序列，
列格式与插件原版一致，``tools/plot_log.py`` 可直接画）、``session_summary.json``。
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .apdf import compute_apdf
from .channels import parse_channel_spec
from .dsp import GrowableF64
from .emg_link import MvcCsvRecorder
from .session_runner import IntegratedConfig, check_contract, session_dir_for

__all__ = ["DpiActuator", "UnifiedSession", "run_unified", "APDF_COLUMNS",
           "compute_muscle_apdf", "apdf_rows_and_curves", "make_demo_source",
           "write_apdf_curve", "write_load_disparity", "thr_default"]

#: 与插件原版 ``sens_log_*.csv`` 相同的列，保证 ``tools/plot_log.py`` 还能画
APDF_COLUMNS = [
    "unix_ms", "elapsed_s", "iso", "mode", "event", "block_idx",
    "target_dpi", "target_sens", "applied_sens", "effective_dpi",
    "emg_EDC", "emg_ECU", "emg_FDS", "emg_UT", "reason",
]


# --------------------------------------------------------------------------- #
# 执行侧：进程内直接驱动
# --------------------------------------------------------------------------- #
class DpiActuator:
    """直接调用插件的 ``rawaccel_bridge`` 写灵敏度，并记插件的 sens_log 格式。

    渐变在主循环里按时间推进（``tick``），**不用线程** —— 单进程下这样更简单，
    也避免两个线程同时写驱动。
    """

    def __init__(
        self,
        cfg: IntegratedConfig,
        session_dir: Path,
        muscles: list[str],
    ):
        self.cfg = cfg
        self.session_dir = Path(session_dir)
        self.muscles = list(muscles)
        self.dry_run = bool(cfg.dry_run)
        self.ramp_sec = float(cfg.switch_ramp_sec)
        self.levels = {k: dict(v) for k, v in cfg.levels.items()}

        plugin_dir = str(Path(cfg.plugin_dir).resolve())
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        import rawaccel_bridge  # type: ignore  # 复用插件的驱动桥

        raw_cfg = {}
        plugin_cfg = Path(cfg.plugin_dir) / "config.json"
        if plugin_cfg.exists():
            try:
                raw_cfg = json.loads(plugin_cfg.read_text(encoding="utf-8")).get(
                    "rawaccel", {})
            except (OSError, ValueError):
                raw_cfg = {}
        raw_cfg["dry_run"] = self.dry_run
        self.bridge = rawaccel_bridge.make_bridge(raw_cfg, quiet=True)

        # 起始档位：默认基准档（low）；方向验证等需要"锁在固定档"时由
        # cfg.fixed_level 指定，此时起始状态就直接写该档。
        start = getattr(self.cfg, "fixed_level", "") or "low"
        if start not in ("low", "high"):
            start = "low"
        self.level = start
        self.sens = self._sens_of(start)
        self.target_level = start
        self._ramp: dict[str, Any] | None = None
        self.rows_written = 0

        self.path = self.session_dir / f"sens_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        self._fh = self.path.open("w", encoding="utf-8", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(APDF_COLUMNS)
        self._lock = threading.Lock()
        # 起始状态：基准档
        self._write_driver(self.sens)

    # ------------------------------------------------------------------ #
    def _sens_of(self, level: str) -> float:
        return float(self.levels.get(level, {}).get("sens", 1.0))

    def _dpi_of(self, level: str) -> float:
        return float(self.levels.get(level, {}).get("dpi", 0))

    def _write_driver(self, sens: float) -> None:
        try:
            self.bridge.apply(sens, accel_mode="off")
        except Exception as exc:  # pragma: no cover - 驱动相关
            self.log_row(0.0, "DRIVER_ERROR", reason=f"{exc}")

    # ------------------------------------------------------------------ #
    @property
    def effective_dpi(self) -> float:
        base = self.levels.get("low", {}).get("dpi", 800.0)
        return base * self.sens

    @property
    def ramping(self) -> bool:
        return self._ramp is not None

    def request(self, level: str, reason: str, now: float) -> bool:
        """请求切到某档；返回是否真的启动了新渐变。"""
        if level not in self.levels:
            return False
        if level == self.level and not self.ramping:
            return False
        if self.ramping and self.target_level == level:
            return False
        self.target_level = level
        self._ramp = {"t0": now, "from": self.sens, "to": self._sens_of(level),
                      "reason": reason, "level": level}
        self.log_row(now, "EMG_SWITCH", reason=reason,
                     target_dpi=self._dpi_of(level),
                     target_sens=self._sens_of(level))
        return True

    def tick(self, now: float, emg: Mapping[str, float] | None = None) -> None:
        """按时间推进渐变（每个采集周期调一次）。"""
        if self._ramp is None:
            return
        r = self._ramp
        span = max(self.ramp_sec, 1e-6)
        p = min(1.0, (now - r["t0"]) / span)
        sens = r["from"] + (r["to"] - r["from"]) * p
        self.sens = sens
        self._write_driver(sens)
        done = p >= 1.0
        self.log_row(now, "EMG_SWITCH" if done else "EMG_SWITCH_RAMP",
                     reason=r["reason"], target_dpi=self._dpi_of(r["level"]),
                     target_sens=self._sens_of(r["level"]), emg=emg)
        if done:
            self.level = r["level"]
            self._ramp = None

    def reset(self, now: float = 0.0) -> None:
        """复位到基准档（会话结束/异常时调用）。"""
        self._ramp = None
        self.target_level = "low"
        self.sens = self._sens_of("low")
        self._write_driver(self.sens)
        self.level = "low"
        self.log_row(now, "SESSION_END", reason="restored base level")

    # ------------------------------------------------------------------ #
    def log_row(self, elapsed: float, event: str, reason: str = "",
                target_dpi: Any = "", target_sens: Any = "",
                applied_sens: float | None = None,
                emg: Mapping[str, float] | None = None) -> None:
        if self._fh is None:
            return
        emg = emg or {}
        row = {
            "unix_ms": int(time.time() * 1000),
            "elapsed_s": f"{elapsed:.3f}",
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "unified",
            "event": event,
            "block_idx": "",
            "target_dpi": "" if target_dpi == "" else f"{float(target_dpi):.0f}",
            "target_sens": "" if target_sens == "" else f"{float(target_sens):.3f}",
            "applied_sens": f"{(applied_sens if applied_sens is not None else self.sens):.4f}",
            "effective_dpi": f"{self.effective_dpi:.1f}",
            "emg_EDC": f"{emg.get('EDC', float('nan')):.2f}" if "EDC" in emg else "",
            "emg_ECU": f"{emg.get('ECU', float('nan')):.2f}" if "ECU" in emg else "",
            "emg_FDS": f"{emg.get('FDS', float('nan')):.2f}" if "FDS" in emg else "",
            "emg_UT": f"{emg.get('UT', float('nan')):.2f}" if "UT" in emg else "",
            "reason": reason,
        }
        with self._lock:
            self._w.writerow([row.get(c, "") for c in APDF_COLUMNS])
            self._fh.flush()
            self.rows_written += 1

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


# --------------------------------------------------------------------------- #
# 信号侧 + 判定的胶水
# --------------------------------------------------------------------------- #
def thr_default(cfg: IntegratedConfig) -> float:
    return float(cfg.high_thr)


def compute_muscle_apdf(
    accum: dict[str, GrowableF64],
    muscles: list[str],
    thr: float = 20.0,
    signal_cfg: Mapping[str, Any] | None = None,
) -> list["MuscleApdf"]:
    """把每肌肉的 %MVC 累积序列算成 APDF（只要汇总行）。"""
    rows, _ = apdf_rows_and_curves(accum, muscles, thr, signal_cfg)
    return rows


def apdf_rows_and_curves(
    accum: dict[str, GrowableF64],
    muscles: list[str],
    thr: float = 20.0,
    signal_cfg: Mapping[str, Any] | None = None,
) -> tuple[list["MuscleApdf"], dict[str, tuple[np.ndarray, np.ndarray]]]:
    """一次算完汇总行 + 绘图曲线。

    返回 ``(rows, curves)``，``curves[muscle] = (超概率%, 幅值%MVC)``。

    曲线必须在这里（**工作线程**）算好：累积数组由采集线程写入，界面线程若直接
    去读会在扩容时读到撕裂的数据；而且每帧重算 APDF 在长会话里会越来越慢。
    """
    signal_cfg = signal_cfg or {}
    win = float(signal_cfg.get("rms_window_sec", 0.125))
    hop = float(signal_cfg.get("rms_hop_sec", 0.0625))
    rows: list[MuscleApdf] = []
    curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for m in muscles:
        values = accum[m].to_array() if m in accum else np.empty(0)
        if values.size == 0:
            rows.append(MuscleApdf(m, 0, 0.0, np.nan, np.nan, np.nan, 0.0,
                                   np.nan))
            continue
        result = compute_apdf(values, mve_ref=100.0, window_sec=win, hop_sec=hop)
        curves[m] = (result.probability_pct.copy(),
                     result.level_at_probability_pct.copy())
        excess = np.maximum(values - thr, 0.0)
        rows.append(MuscleApdf(
            muscle=m,
            n_windows=int(values.size),
            total_sec=float(values.size * hop),
            static_pct=result.static_level,
            median_pct=result.median_level,
            peak_pct=result.peak_level,
            high_area=float(np.sum(excess) * hop),
            high_ratio=float(np.mean(values > thr) * 100.0),
        ))
    return rows, curves


def write_apdf_curve(curves, muscles, session_dir) -> Path | None:
    """把 **APDF 曲线本身**落盘（长表：muscle, probability_pct, level_pct_mvc）。

    只存 P10/P50/P90 三个点没法比较"曲线形状" —— 而"改变 DPI 是否把负荷在两块
    肌肉之间重新分配"恰恰表现为**整条曲线的平移/变形**，不只是三个点移动。
    没有这个文件，方向验证就只能比三个数字。
    """
    path = Path(session_dir) / "apdf_curve.csv"
    wrote = False
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["muscle", "probability_pct", "level_pct_mvc"])
        for m in muscles:
            xy = curves.get(m)
            if xy is None:
                continue
            prob, level = xy
            for p, lv in zip(prob, level):
                w.writerow([m, f"{float(p):.4f}", f"{float(lv):.4f}"])
            wrote = True
    return path if wrote else None


def write_load_disparity(rows, pair, session_dir) -> dict:
    """写出**肌间负荷差异** —— 负荷再分配研究的核心验证量。

    只用已发表量（APDF 百分位水平），不做任何加权合成：

        diff_X = X_A − X_B          X ∈ {P10(static), P50(median), P90(peak)}

    ``diff`` 有符号（正 = A 更重），``absdiff`` 为差异幅度。若动态 DPI 真的
    重新分配了负荷，``absdiff`` 应显著小于固定 DPI 条件 —— 这就是假设的检验量。
    同时给出各水平超出 Jonsson 参考限值的倍数（需按原文核对数值）。
    """
    by = {r.muscle: r for r in rows}
    a, b = (list(pair) + [None, None])[:2]
    ra, rb = by.get(a), by.get(b)
    out: dict = {"pair": [a, b]}
    limits = {"static": 5.0, "median": 14.0, "peak": 70.0}   # 上限，需核对原文
    for key, att in (("static", "static_pct"), ("median", "median_pct"),
                     ("peak", "peak_pct")):
        va = getattr(ra, att, float("nan")) if ra else float("nan")
        vb = getattr(rb, att, float("nan")) if rb else float("nan")
        if not (np.isfinite(va) and np.isfinite(vb)):
            continue
        diff = float(va - vb)
        out[key] = {"A": float(va), "B": float(vb), "diff": diff,
                    "absdiff": abs(diff),
                    "ratio": float(va / vb) if vb > 0 else float("nan"),
                    "exceed_A": float(va / limits[key]),
                    "exceed_B": float(vb / limits[key])}
    # 也报告自造面积（仅作对照，论文不应使用）
    if ra is not None and rb is not None:
        ha, hb = getattr(ra, "high_area", float("nan")), getattr(rb, "high_area", float("nan"))
        if np.isfinite(ha) and np.isfinite(hb):
            out["excess_area_for_contrast"] = {"A": float(ha), "B": float(hb),
                                               "diff": float(ha - hb)}

    path = Path(session_dir) / "load_disparity.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["muscle_pair", "level", "A_value", "B_value", "diff_A_minus_B",
                    "abs_diff", "ratio_A_over_B", "A_over_jonsson_limit",
                    "B_over_jonsson_limit", "limit_used"])
        for key in ("static", "median", "peak"):
            d = out.get(key)
            if not d:
                continue
            w.writerow([f"{a}-{b}", key, f"{d['A']:.4f}", f"{d['B']:.4f}",
                        f"{d['diff']:.4f}", f"{d['absdiff']:.4f}",
                        f"{d['ratio']:.4f}", f"{d['exceed_A']:.4f}",
                        f"{d['exceed_B']:.4f}", limits[key]])
    return out


def make_demo_source(channels_spec: str, mvc: Mapping[str, float],
                     seg_sec: float = 25.0):
    """无硬件演示源：负荷在相邻肌肉间反相交替（ECU/UT 与 FDS/EDC）。

    按**配置里的 MVC** 反推合成幅值（``mve_unit = MVC``），所以包络百分数
    直接等于 %MVC —— 无论 MVC 填 80 还是 1000，结果都自洽。
    """
    from .channels import parse_channel_spec
    from .sources import SyntheticProfile, SyntheticSource

    mapping = parse_channel_spec(channels_spec)
    profiles, labels = [], []
    for i, (token, muscle) in enumerate(mapping.items()):
        unit = float(mvc.get(muscle) or 1000.0)
        first, second = (30.0, 10.0) if i % 2 == 0 else (10.0, 30.0)
        profiles.append(SyntheticProfile(
            segments=[(seg_sec, first), (seg_sec, second)],
            noise_floor_pct=0.0, mve_unit=unit, dc_offset=0.0))
        labels.append(token)
    return SyntheticSource(profiles=profiles, channel_labels=labels,
                           n_channels=len(profiles), srate=1000.0,
                           realtime=True)


class _Monitor:
    """给 BalancePolicy 用的最小接口：只需要 ``.last``。"""

    def __init__(self) -> None:
        self.last: dict[str, float] = {}

    def set(self, values: Mapping[str, float]) -> None:
        self.last.update(values)


@dataclass
class MuscleApdf:
    muscle: str
    n_windows: int
    total_sec: float
    static_pct: float
    median_pct: float
    peak_pct: float
    high_area: float
    high_ratio: float

    @staticmethod
    def header() -> list[str]:
        return ["muscle", "n_windows", "total_sec", "static_pct_mvc",
                "median_pct_mvc", "peak_pct_mvc", "high_area_pct_s",
                "high_ratio_pct"]

    def as_row(self) -> list[Any]:
        return [self.muscle, self.n_windows, f"{self.total_sec:.2f}",
                f"{self.static_pct:.2f}", f"{self.median_pct:.2f}",
                f"{self.peak_pct:.2f}", f"{self.high_area:.1f}",
                f"{self.high_ratio:.2f}"]


class UnifiedSession:
    """一个进程里跑完：采集 → %MVC → APDF → balance 判定 → 写 DPI。"""

    def __init__(self, cfg: IntegratedConfig, source, printer=print):
        self.cfg = cfg
        self.source = source
        self.printer = printer
        self.session_dir = session_dir_for(cfg)
        self.summary: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def run(self) -> dict[str, Any]:
        from .channels import MultiChannelMvc
        from .config import AppConfig

        cfg = self.cfg
        problems = check_contract(cfg)
        if problems:
            for p in problems:
                self.printer(f"  x {p}")
            raise ValueError(f"Config failed check ({len(problems)} problems)")

        meta = self.source.connect()
        signal_cfg = (AppConfig.from_dict({"signal": cfg.signal}).signal
                      if cfg.signal else None)
        pipeline = MultiChannelMvc.from_spec(
            fs=meta.nominal_srate, channel_spec=cfg.channels,
            mvc_spec=cfg.mvc, meta=meta, signal_cfg=signal_cfg,
        )

        # 判定：直接复用插件的 BalancePolicy
        plugin_dir = str(Path(cfg.plugin_dir).resolve())
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        from emg_server import BalancePolicy  # type: ignore

        emg_cfg = {
            "switch_ramp_sec": cfg.switch_ramp_sec,
            "balance": cfg.balance_block(
                event_log=str(self.session_dir / "balance_events.csv")),
        }
        policy = BalancePolicy(emg_cfg)
        monitor = _Monitor()

        actuator = DpiActuator(cfg, self.session_dir, pipeline.muscles)
        recorder = MvcCsvRecorder(self.session_dir / "pctmvc.csv",
                                  pipeline.muscles)
        recorder.open()

        accum = {m: GrowableF64() for m in pipeline.muscles}
        self._banner(meta, pipeline, policy, actuator)

        fixed_level = str(getattr(cfg, "fixed_level", "") or "").strip().lower()
        if fixed_level:
            self.printer(f"  Locked level: {fixed_level} "
                         f"({actuator._dpi_of(fixed_level):.0f} DPI) "
                         f"- automatic switching disabled")

        t0 = time.time()
        next_send = 0.0
        next_report = 0.0
        latest: dict[str, float] = {}
        interrupted = False
        try:
            while True:
                chunk = self.source.read_chunk(timeout=0.05)
                now = time.time()
                elapsed = now - t0
                if chunk is not None and len(chunk):
                    times, values = pipeline.push(chunk.data, chunk.timestamps)
                    if times.size:
                        latest = pipeline.latest(values)
                        for m, series in values.items():
                            if series.size:
                                good = series[np.isfinite(series)]
                                if good.size:
                                    accum[m].append(good)
                        if latest:
                            recorder.write(float(times[-1]), latest, t_wall=now)
                if latest and elapsed >= next_send:
                    monitor.set(latest)
                    actuator.tick(elapsed, latest)
                    if fixed_level:
                        # 锁档模式（方向验证用）：不做任何自动判定，档位保持不动。
                        # 之所以必须能锁档：判定规则本身就是"哪块肌肉更重"，
                        # 而那正是要被检验的东西 —— 让它自己决定就是循环论证。
                        next_send = elapsed + 1.0 / max(cfg.send_hz, 0.01)
                        continue
                    action, reason = policy.evaluate(elapsed, actuator.level,
                                                     monitor)
                    if action:
                        # 策略给的是**动作**（up/down），执行器要的是**档位**
                        # （low/high）—— 这里必须做一次映射，否则 request() 会被
                        # 档位校验当成非法档位直接拒绝，DPI 永远不会动。
                        target = "high" if action == "up" else "low"
                        if actuator.request(target, reason, elapsed):
                            self.printer(
                                f"    [decision] t={elapsed:6.1f}s  {action:<4} "
                                f"-> {target} ({actuator._dpi_of(target):.0f} DPI)  "
                                f"{reason}")
                    next_send = elapsed + 1.0 / max(cfg.send_hz, 0.01)
                for row in self._drain_policy_events(policy):
                    self.printer(f"    [balance] t={row['t_sim']:>8}s {row['event']:<14} "
                                 f"{row.get('action', '')!s:<5} {row.get('reason', '')}")
                if elapsed >= next_report:
                    mv = " ".join(f"{k}={v:5.1f}" for k, v in sorted(latest.items())) or "-"
                    self.printer(
                        f"  t={elapsed:6.1f}s | %MVC {mv} | DPI {actuator.effective_dpi:6.0f}"
                        f" (level {actuator.level}{', ramping' if actuator.ramping else ''})"
                        f" | switches {policy.stats['switches']} crossings {policy.stats['crossings']}")
                    next_report = elapsed + cfg.report_sec
                if cfg.duration is not None and elapsed >= cfg.duration:
                    break
        except KeyboardInterrupt:
            interrupted = True
            self.printer("\n  Received Ctrl+C; shutting down safely...")
        finally:
            actuator.reset(now - t0)
            apdf_rows, apdf_curves = apdf_rows_and_curves(
                accum, pipeline.muscles, cfg.high_thr, cfg.signal)
            self._write_apdf(apdf_rows)
            curve_path = write_apdf_curve(apdf_curves, pipeline.muscles,
                                          self.session_dir)
            policy.close()
            recorder.close()
            actuator.close()
            try:
                self.source.close()
            except Exception:
                pass
            self.summary = {
                "mode": "unified(single process)",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "session_dir": str(self.session_dir),
                "duration_sec": round(time.time() - t0, 2),
                "dry_run": cfg.dry_run,
                "channels": cfg.channels,
                "mvc": dict(cfg.mvc),
                "pair": list(cfg.pair),
                "stream": {"name": meta.name, "n_channels": meta.n_channels,
                           "nominal_srate": meta.nominal_srate},
                "pctmvc_rows": recorder.rows,
                "sens_log_rows": actuator.rows_written,
                "balance": dict(policy.stats),
                "final_level": actuator.level,
                "apdf": {r.muscle: r.as_row() for r in apdf_rows},
                "interrupted": interrupted,
                "files": {
                    "pctmvc_csv": str(recorder.path),
                    "sens_log": str(actuator.path),
                    "balance_events": policy.log_path,
                    "load_disparity": str(self.session_dir / "load_disparity.csv"),
                },
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if curve_path:
                self.summary["files"]["apdf_curve"] = str(curve_path)
            (self.session_dir / "session_summary.json").write_text(
                json.dumps(self.summary, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8")

            disp = write_load_disparity(apdf_rows, cfg.pair, self.session_dir)
            self.summary["load_disparity"] = disp
            self.summary["files"]["load_disparity"] = str(
                self.session_dir / "load_disparity.csv")

        self._print_summary(apdf_rows)
        return self.summary

    # ------------------------------------------------------------------ #
    def _drain_policy_events(self, policy) -> list[dict[str, str]]:
        """把 policy 刚写进事件日志的新行读出来（用于实时打印）。"""
        if not getattr(self, "_ev_offset", None):
            self._ev_offset = 0
            self._ev_header = None
        path = policy.log_path
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                if self._ev_header is None:
                    first = fh.readline()
                    if not first:
                        return []
                    self._ev_header = [c.strip() for c in first.rstrip("\r\n").split(",")]
                    self._ev_offset = fh.tell()
                fh.seek(self._ev_offset)
                raw = fh.read()
                if not raw or not raw.endswith("\n"):
                    return []
                self._ev_offset += len(raw.encode("utf-8"))
        except OSError:
            return []
        import io as _io

        out = []
        for row in csv.reader(_io.StringIO(raw)):
            if len(row) == len(self._ev_header):
                out.append(dict(zip(self._ev_header, row)))
        return out

    def _compute_apdf(self, accum: dict[str, GrowableF64],
                      muscles: list[str]) -> list[MuscleApdf]:
        """%MVC 序列 → APDF（单位 %MVC），见 :func:`compute_muscle_apdf`。"""
        return compute_muscle_apdf(accum, muscles, self.cfg.high_thr,
                                   self.cfg.signal)

    def _write_apdf(self, rows: list[MuscleApdf]) -> None:
        path = self.session_dir / "apdf_summary.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(MuscleApdf.header())
            for r in rows:
                w.writerow(r.as_row())

    # ------------------------------------------------------------------ #
    def _banner(self, meta, pipeline, policy, actuator) -> None:
        p = self.printer
        p("=" * 76)
        p("  Unified plugin: acquisition -> %MVC -> APDF -> balance decision -> DPI write  (single process, no TCP)")
        p("=" * 76)
        p(f"  Session dir : {self.session_dir}")
        p(f"  Source      : {meta.describe()}")
        p(f"  Channels    : {pipeline.describe()}")
        p(f"  Decision    : pair={policy.pair} threshold={policy.threshold}%MVC "
          f"window={policy.window:g}s baseline={policy.baseline_sec:g}s "
          f"deadband={policy.deadband:g} confirm={policy.confirm:g}s")
        p(f"  Actuator    : {self.cfg.levels['low']['dpi']:.0f} <-> "
          f"{self.cfg.levels['high']['dpi']:.0f} DPI, ramp {actuator.ramp_sec:g}s"
          f"  driver={'DRY-RUN' if actuator.dry_run else '* writing'}")
        p("=" * 76)

    def _print_summary(self, rows: list[MuscleApdf]) -> None:
        p = self.printer
        p("-" * 76)
        p("  Per-muscle APDF (unit: %MVC)")
        p(f"    {'muscle':<6}{'windows':>7}{'span_s':>9}{'static':>9}{'median':>9}"
          f"{'peak':>9}{'>20%area':>11}{'>20%ratio':>10}")
        for r in rows:
            p(f"    {r.muscle:<6}{r.n_windows:>7}{r.total_sec:>9.1f}"
              f"{r.static_pct:>9.1f}{r.median_pct:>9.1f}{r.peak_pct:>9.1f}"
              f"{r.high_area:>11.1f}{r.high_ratio:>9.1f}%")
        b = self.summary.get("balance", {})
        p("-" * 76)
        p(f"  Switches {b.get('switches', 0)} / crossings {b.get('crossings', 0)} / "
          f"saturated {b.get('saturated', 0)} / mode {b.get('mode_used') or '-'}")
        p(f"  Output: {self.session_dir}")
        p("=" * 76)


def run_unified(cfg: IntegratedConfig, source_factory, printer=print) -> dict[str, Any]:
    """一条命令跑完的统一插件入口。"""
    session = UnifiedSession(cfg, source_factory(cfg), printer=printer)
    return session.run()
