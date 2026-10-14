"""实时导出层：后台写盘线程 + 会话目录布局 + 处理后信号回发 LSL。

为什么要单独一个写线程
----------------------
采集线程/界面线程一旦被磁盘 IO 阻塞，就会在 LSL inlet 里堆积数据甚至丢样本。
所以采集线程只负责把数据**塞进队列**（非阻塞），由本模块的写线程负责格式化
与 flush。队列满时丢弃并计数（``dropped``），绝不反过来阻塞采集。

导出的文件（会话目录，见 :func:`SessionPaths.create`）
------------------------------------------------------
=========================  ====================================================
``raw.csv``                **实时导出的原始信号**：lsl_time, local_time, ch1..chN
``rms.csv``                每个 RMS 窗口的幅值与 %MVE 负荷（实时）
``apdf_curve.csv``         最新一次 APDF 曲线快照（概率网格 + 幅值网格）
``percentiles.csv``        静态/中位/峰值负荷随时间的变化（每快照一条）
``calibration.json``       MVC 标定结果（MVE 参考值、各试次、警告）
``config.json``            本次运行的全部参数（保证可复现）
``summary.json``           结束时写入的总汇（时长、样本数、APDF 摘要、QC）
``events.log``             人类可读的运行日志
=========================  ====================================================
"""

from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .apdf import ApdfResult
from .config import ExportConfig
from .sources import PYLSL_AVAILABLE, PYLSL_IMPORT_ERROR, pylsl, require_pylsl

__all__ = ["SessionPaths", "SessionWriter", "ProcessedOutlet", "export_apdf_result"]


# --------------------------------------------------------------------------- #
# 目录布局
# --------------------------------------------------------------------------- #
@dataclass
class SessionPaths:
    """一次采集会话的全部输出路径。"""

    root: Path
    raw_csv: Path
    rms_csv: Path
    apdf_curve_csv: Path
    percentiles_csv: Path
    calibration_json: Path
    config_json: Path
    summary_json: Path
    events_log: Path

    @classmethod
    def create(
        cls,
        root_dir: str | Path,
        prefix: str = "session",
        when: float | None = None,
        subject_id: str = "",
    ) -> "SessionPaths":
        """按 ``{prefix}_{时间}[_{被试}]`` 建立唯一的会话目录。"""
        base = Path(root_dir)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
        name = f"{prefix}_{stamp}"
        if subject_id:
            safe = "".join(c for c in subject_id if c.isalnum() or c in "-_")
            if safe:
                name = f"{name}_{safe}"
        target = base / name
        suffix = 1
        while target.exists():
            target = base / f"{name}_{suffix}"
            suffix += 1
        target.mkdir(parents=True, exist_ok=True)
        return cls(
            root=target,
            raw_csv=target / "raw.csv",
            rms_csv=target / "rms.csv",
            apdf_curve_csv=target / "apdf_curve.csv",
            percentiles_csv=target / "percentiles.csv",
            calibration_json=target / "calibration.json",
            config_json=target / "config.json",
            summary_json=target / "summary.json",
            events_log=target / "events.log",
        )


# --------------------------------------------------------------------------- #
# 写盘线程
# --------------------------------------------------------------------------- #
@dataclass
class _Item:
    kind: str
    payload: tuple


class SessionWriter:
    """把数据写盘的工作放到独立线程，采集路径只做入队。"""

    def __init__(
        self,
        paths: SessionPaths,
        cfg: ExportConfig | None = None,
        precision: int = 6,
        time_precision: int = 6,
        max_queue: int = 4096,
    ):
        self.paths = paths
        self.cfg = cfg or ExportConfig()
        self.precision = int(precision)
        self.time_precision = int(time_precision)
        """时间列的**小数位**数。

        注意不能对时间列用 ``%.6g`` 这种"有效数字"格式：LSL 时间戳是
        "开机以来秒数"（量级约 1e6），6 位有效数字会把小数部分全部抹掉
        （863672.077137 → 863672），导致导出的时间轴失去分辨率、离线重算
        无法推回采样率。时间列一律用定点 ``%.6f``。
        """
        self._time_fmt = f"%.{self.time_precision}f"
        self._queue: "queue.Queue[_Item | None]" = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._dropped = 0
        self._written = 0
        self._errors: list[str] = []
        self._last_flush = 0.0
        self._files: dict[str, Any] = {}
        self._writers: dict[str, Any] = {}
        self._raw_header_written = False
        self._rms_header_written = False
        self._percentiles_header_written = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None:
            return
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lsl-apdf-writer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:  # pragma: no cover
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        self._close_files()

    # ------------------------------------------------------------------ #
    # 入队接口（采集线程调用，非阻塞）
    # ------------------------------------------------------------------ #
    def submit_raw(
        self, lsl_times: np.ndarray, local_times: np.ndarray, data: np.ndarray
    ) -> None:
        if not (self.cfg.enabled and self.cfg.write_raw):
            return
        self._put("raw", (lsl_times, local_times, data))

    def submit_rms(
        self, times: np.ndarray, rms_values: np.ndarray, level_pct: np.ndarray | None
    ) -> None:
        if not self.cfg.enabled:
            return
        self._put("rms", (times, rms_values, level_pct))

    def submit_apdf(self, result: ApdfResult, wall_time: float | None = None) -> None:
        if not self.cfg.enabled:
            return
        self._put("apdf", (result, wall_time or time.time()))

    def submit_json(self, path: Path, obj: Any) -> None:
        self._put("json", (Path(path), obj))

    def log(self, message: str) -> None:
        self._put("log", (message,))

    def write_now(self, path: Path, text: str) -> None:
        """同步写文本文件（用于 config.json 这类必须在开始前落盘的内容）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ #
    def _put(self, kind: str, payload: tuple) -> None:
        try:
            self._queue.put_nowait(_Item(kind, payload))
        except queue.Full:
            with self._lock:
                self._dropped += 1

    # ------------------------------------------------------------------ #
    # 线程主体
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_flush(force=True)
                if self._stop.is_set():
                    break
                continue
            if item is None:  # 哨兵：退出
                break
            try:
                self._handle(item)
                self._written += 1
            except Exception as exc:  # pragma: no cover - 防御
                with self._lock:
                    self._errors.append(f"{item.kind}: {exc}")
            self._maybe_flush(force=self._queue.empty())
        self._maybe_flush(force=True)

    def _maybe_flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_flush) < self.cfg.flush_interval_sec:
            return
        for fh in self._files.values():
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except Exception:  # pragma: no cover
                pass
        self._last_flush = now

    # ------------------------------------------------------------------ #
    def _handle(self, item: _Item) -> None:
        kind = item.kind
        if kind == "raw":
            self._write_raw(*item.payload)
        elif kind == "rms":
            self._write_rms(*item.payload)
        elif kind == "apdf":
            self._write_apdf(*item.payload)
        elif kind == "json":
            path, obj = item.payload
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(
                path, json.dumps(obj, indent=2, ensure_ascii=False, default=_jsonable)
            )
        elif kind == "log":
            self._append_log(item.payload[0])

    # ------------------------------------------------------------------ #
    def _file(self, key: str, path: Path):
        fh = self._files.get(key)
        if fh is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("w", encoding="utf-8", newline="")
            self._files[key] = fh
        return fh

    def _writer(self, key: str, path: Path):
        w = self._writers.get(key)
        if w is None:
            w = csv.writer(self._file(key, path))
            self._writers[key] = w
        return w

    def _write_raw(self, lsl_times, local_times, data) -> None:
        lsl_times = np.asarray(lsl_times, dtype=float).reshape(-1)
        local_times = np.asarray(local_times, dtype=float).reshape(-1)
        data = np.asarray(data, dtype=float)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        writer = self._writer("raw", self.paths.raw_csv)
        if not self._raw_header_written:
            writer.writerow(
                ["lsl_time", "local_time"]
                + [f"ch{i + 1}" for i in range(data.shape[1])]
            )
            self._raw_header_written = True
        fmt = f"%.{self.precision}g"
        tfmt = self._time_fmt
        rows = (
            [tfmt % lsl_times[i], tfmt % local_times[i]]
            + [fmt % v for v in data[i]]
            for i in range(data.shape[0])
        )
        writer.writerows(rows)

    def _write_rms(self, times, values, level_pct) -> None:
        times = np.asarray(times, dtype=float).reshape(-1)
        values = np.asarray(values, dtype=float).reshape(-1)
        if level_pct is None:
            level_pct = np.full(values.shape, np.nan)
        else:
            level_pct = np.asarray(level_pct, dtype=float).reshape(-1)
        writer = self._writer("rms", self.paths.rms_csv)
        if not self._rms_header_written:
            writer.writerow(["lsl_time", "rms", "level_pct_mve"])
            self._rms_header_written = True
        fmt = f"%.{self.precision}g"
        tfmt = self._time_fmt
        writer.writerows(
            [tfmt % times[i], fmt % values[i], fmt % level_pct[i]]
            for i in range(values.size)
        )

    def _write_apdf(self, result: ApdfResult, wall_time: float) -> None:
        # 1) 曲线快照（原子替换，始终是"最新")
        lines = ["probability_pct,level_pct_mve", "level_pct_mve,exceedance_prob"]
        prob_rows = "\n".join(
            f"%.{self.precision}g,%.{self.precision}g" % (p, l)
            for p, l in result.probability_grid_rows()
        )
        level_rows = "\n".join(
            f"%.{self.precision}g,%.{self.precision}g" % (l, p)
            for l, p, _ in result.level_grid_rows()
        )
        self._atomic_write(
            self.paths.apdf_curve_csv,
            "# APDF: top half is the probability grid (probability_pct, level_pct_mve), "
            "bottom half is the level grid (level_pct_mve, exceedance_prob)\n"
            f"{lines[0]}\n{prob_rows}\n{lines[1]}\n{level_rows}\n",
        )

        # 2) 分位点时间序列（追加）
        path = self.paths.percentiles_csv
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if not self._percentiles_header_written:
                writer.writerow(
                    [
                        "local_time",
                        "n_windows",
                        "total_sec",
                        "mve_ref",
                        "static_pct",
                        "median_pct",
                        "peak_pct",
                        "mean_pct",
                        "max_pct",
                    ]
                )
                self._percentiles_header_written = True
            writer.writerow(
                [
                    self._time_fmt % wall_time,
                    result.n_windows,
                    f"%.{self.precision}g" % result.total_sec,
                    f"%.{self.precision}g" % result.mve_ref,
                    f"%.{self.precision}g" % result.static_level,
                    f"%.{self.precision}g" % result.median_level,
                    f"%.{self.precision}g" % result.peak_level,
                    f"%.{self.precision}g" % result.mean_level_pct,
                    f"%.{self.precision}g" % result.max_level_pct,
                ]
            )

    def _append_log(self, message: str) -> None:
        fh = self._file("log", self.paths.events_log)
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------ #
    def _close_files(self) -> None:
        self._maybe_flush(force=True)
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:  # pragma: no cover
                pass
        self._files.clear()
        self._writers.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": self._queue.qsize(),
                "written": self._written,
                "dropped": self._dropped,
                "errors": len(self._errors),
            }

    def error_messages(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def __enter__(self) -> "SessionWriter":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# 处理后信号回发 LSL（供其它程序订阅）
# --------------------------------------------------------------------------- #
class ProcessedOutlet:
    """把处理后的负荷指标再发布成一个 LSL 流。

    通道：``level_pct_mve``、``static_pct``、``median_pct``、``peak_pct``。
    标称采样率取 RMS 产出速率（``1/hop``），这样订阅方的窗口计算天然对齐。
    用途：让"疲劳检测/DPI 切换"等下游插件直接订阅处理结果，而不必自己重算。
    """

    def __init__(self, name: str, srate: float, source_id: str = "lsl_apdf.processed"):
        require_pylsl()
        self.name = name
        self.srate = float(max(srate, 1e-6))
        self.source_id = source_id
        self._outlet: Any | None = None
        self._pushed = 0

    def open(self) -> None:
        info = pylsl.StreamInfo(
            name=self.name,
            type="APDF",
            channel_count=4,
            nominal_srate=self.srate,
            channel_format="float32",
            source_id=self.source_id,
        )
        desc = info.desc()
        channels = desc.append_child("channels")
        for label, unit in (
            ("level_pct_mve", "%MVE"),
            ("static_pct", "%MVE"),
            ("median_pct", "%MVE"),
            ("peak_pct", "%MVE"),
        ):
            ch = channels.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", unit)
        desc.append_child_value("manufacturer", "lsl_apdf")
        self._outlet = pylsl.StreamOutlet(info)

    def push(self, values: Sequence[float], timestamp: float | None = None) -> None:
        if self._outlet is None:
            return
        self._outlet.push_sample([float(v) for v in values], timestamp)
        self._pushed += 1

    @property
    def pushed(self) -> int:
        return self._pushed

    def close(self) -> None:
        self._outlet = None


# --------------------------------------------------------------------------- #
# 离线复用
# --------------------------------------------------------------------------- #
def export_apdf_result(result: ApdfResult, out_dir: str | Path, stem: str = "apdf") -> list[Path]:
    """把一次 APDF 结果导出成 CSV（离线/事后分析路径复用同一套格式）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    curve = out_dir / f"{stem}_curve.csv"
    with curve.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["probability_pct", "level_pct_mve"])
        writer.writerows(result.probability_grid_rows())
    levels = out_dir / f"{stem}_levels.csv"
    with levels.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["level_pct_mve", "exceedance_prob", "exceedance_pct"])
        writer.writerows(result.level_grid_rows())
    summary = out_dir / f"{stem}_summary.json"
    summary.write_text(
        json.dumps(result.summary(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return [curve, levels, summary]


def _jsonable(obj: Any) -> Any:
    """json.dumps 的 default 钩子：处理 numpy / dataclass / Path。"""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)
