"""离线分析：从已导出的 ``raw.csv`` 重新计算 APDF。

用途：现场采集时只用最省资源的参数（比如较长的 RMS 窗），事后用不同参数
重算；或者把别人发来的录制文件跑成本插件的 APDF 报告。

与实时路径的差别（两条路径都实现，结果应当高度一致）：

* 实时用**因果** IIR 滤波（不能预知未来），离线默认用**零相位** ``filtfilt``；
* 相位差异会让单窗 RMS 略有不同，但对 APDF 这种统计量影响很小
  （``zero_phase=False`` 可切换到与实时完全一致的因果滤波）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .apdf import ApdfResult, compute_apdf
from .config import AppConfig, SignalConfig
from .dsp import RmsExtractor, design_filter_chain, filter_offline

__all__ = ["Recording", "load_recording", "analyze_recording", "analyze_csv"]


@dataclass
class Recording:
    """从 CSV 读入的录制数据。"""

    path: Path
    times: np.ndarray
    channels: dict[str, np.ndarray] = field(default_factory=dict)
    local_times: np.ndarray | None = None
    header: list[str] = field(default_factory=list)

    @property
    def srate(self) -> float:
        if self.times.size < 2:
            return 0.0
        dt = float(np.median(np.diff(self.times)))
        return 1.0 / dt if dt > 0 else 0.0

    def channel(self, name: str | None = None, index_1based: int = 1) -> np.ndarray:
        """按列名取通道；``name=None`` 时按 1 起编号取第 N 个通道列。"""
        if name and name in self.channels:
            return self.channels[name]
        keys = list(self.channels)
        if not keys:
            raise ValueError(f"No channel columns in {self.path}")
        if 0 <= index_1based - 1 < len(keys):
            return self.channels[keys[index_1based - 1]]
        raise ValueError(
            f"Channel {index_1based} out of range (available columns: {keys})"
        )


def load_recording(path: str | Path, time_column: int = 0) -> Recording:
    """读取 ``raw.csv``（或任意"时间列 + 数值列"的 CSV）。

    自动跳过表头/注释行；表头存在时通道名取自表头（``ch1``、``ch2``…）。
    """
    path = Path(path)
    header: list[str] = []
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for raw in csv.reader(fh):
            if not raw:
                continue
            if raw[0].lstrip().startswith("#"):
                continue
            try:
                rows.append([float(c) for c in raw])
            except ValueError:
                if not header:
                    header = [c.strip() for c in raw]
                continue
    if not rows:
        raise ValueError(f"No numeric rows parsed from {path}")
    width = max(len(r) for r in rows)
    rows = [r for r in rows if len(r) == width]
    arr = np.asarray(rows, dtype=float)

    if not header or len(header) != width:
        header = ["col0", "col1"] + [f"ch{i}" for i in range(1, width - 1)]
        if width == 1:
            header = ["col0"]

    times = arr[:, time_column]
    local_times = arr[:, 1] if width > 2 and header[1] == "local_time" else None
    # 自动识别并排除时间列：避免把 local_time 当成一路信号
    time_names = {"lsl_time", "local_time", "time", "timestamp", "t"}
    skip = {time_column}
    if len(header) == width:
        for i, name in enumerate(header):
            if name.strip().lower() in time_names:
                skip.add(i)
    elif width > 2:
        skip = {0, 1}  # 无表头时按本插件默认格式（lsl_time, local_time, ch...）
    channel_indices = [i for i in range(width) if i not in skip] or [
        i for i in range(width) if i != time_column
    ]
    channels = {header[i]: arr[:, i] for i in channel_indices}
    return Recording(
        path=path, times=times, channels=channels, local_times=local_times, header=header
    )


def analyze_recording(
    recording: Recording,
    mve_ref: float,
    signal_cfg: SignalConfig | None = None,
    apdf_cfg=None,
    channel_name: str | None = None,
    channel: int | None = None,
    zero_phase: bool = True,
) -> tuple[ApdfResult, np.ndarray, np.ndarray]:
    """对一段录制数据重算 APDF。

    Returns
    -------
    ``(apdf_result, rms_times, rms_values)``
    """
    cfg = signal_cfg or SignalConfig()
    chan = channel if channel is not None else cfg.channel
    x = recording.channel(channel_name, chan)
    fs = recording.srate
    if fs <= 0:
        raise ValueError(f"{recording.path}: cannot derive sample rate from the time column")

    physical = (x - cfg.adc_offset) * cfg.adc_scale
    design = design_filter_chain(
        fs=fs,
        highpass_hz=cfg.highpass_hz,
        lowpass_hz=cfg.lowpass_hz,
        notch_hz=cfg.notch_hz,
        notch_q=cfg.notch_q,
        order=cfg.filter_order,
    )
    filtered = filter_offline(physical, fs, design, zero_phase=zero_phase)

    extractor = RmsExtractor(fs, cfg.rms_window_sec, cfg.rms_hop_sec)
    rms_values, rms_times = extractor.push(filtered, recording.times)

    if rms_times.size:
        keep = (rms_times - rms_times[0]) >= cfg.startup_skip_sec
        rms_values = rms_values[keep]
        rms_times = rms_times[keep]

    result = compute_apdf(
        rms_values,
        mve_ref=mve_ref,
        cfg=apdf_cfg,
        window_sec=cfg.rms_window_sec,
        hop_sec=cfg.rms_hop_sec,
        method="offline_zero_phase" if zero_phase else "offline_causal",
    )
    return result, rms_times, rms_values


def analyze_csv(
    path: str | Path,
    mve_ref: float,
    cfg: AppConfig | None = None,
    channel_name: str | None = None,
    channel: int | None = None,
    zero_phase: bool = True,
) -> tuple[ApdfResult, np.ndarray, np.ndarray, Recording]:
    """便捷入口：读文件 → 重算 APDF。"""
    recording = load_recording(path)
    cfg = cfg or AppConfig()
    result, rms_t, rms_v = analyze_recording(
        recording,
        mve_ref=mve_ref,
        signal_cfg=cfg.signal,
        apdf_cfg=cfg.apdf,
        channel_name=channel_name,
        channel=channel,
        zero_phase=zero_phase,
    )
    return result, rms_t, rms_v, recording
