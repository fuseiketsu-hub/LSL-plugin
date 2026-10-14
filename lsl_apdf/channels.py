"""多通道 %MVC：把 LSL 的 A1–A4 映射到肌肉名，逐肌肉算 RMS → %MVC。

这是 Part 2 与 Part 3 之间的那一层：Part 3（DPI 插件的 balance 策略）需要的是
**"每块肌肉当前的 %MVC"**，而不是 ADC 原始值或 APDF 曲线。

处理链（每通道独立）：:

    raw[A_k] → 去偏移/带通 → 滑动窗 RMS → ÷ MVC_muscle × 100 → %MVC

MVC 是**外部输入**（第 3 回组会确定的标准姿势实测，每肌肉 2–3 次取最大 RMS），
不在本模块里测量 —— 与实验协议一致。

通道指定方式（``channel_spec``）三种都支持，按优先级解析：

===============  ==========================================
``"A1=ECU,A2=FDS,A3=UT,A4=EDC"``  按 bitalino 的 A 编号
``"1=ECU,2=FDS"``                 按 1 起通道号
``"EMG1=ECU,EMG2=FDS"``          按流里声明的通道标签
===============  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .config import SignalConfig
from .dsp import FilterDesign, RmsExtractor, StreamingFilter, design_filter_chain
from .sources import StreamMeta

__all__ = [
    "MuscleChannel",
    "MultiChannelMvc",
    "parse_channel_spec",
    "parse_mvc_spec",
    "resolve_channel_index",
]


# --------------------------------------------------------------------------- #
# 规格解析
# --------------------------------------------------------------------------- #
def parse_channel_spec(spec: str | Sequence[str] | dict[str, str] | None) -> dict[str, str]:
    """解析 ``"A1=ECU,A2=FDS"`` → ``{"A1": "ECU", "A2": "FDS"}``。"""
    if spec is None:
        return {}
    if isinstance(spec, dict):
        return {str(k).strip(): str(v).strip() for k, v in spec.items()}
    items = [spec] if isinstance(spec, str) else list(spec)
    out: dict[str, str] = {}
    for item in items:
        for part in str(item).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(
                    f"Channel mapping must be channel=muscle, got {part!r}"
                    " (e.g. A1=ECU,A2=FDS,A3=UT,A4=EDC)"
                )
            chan, muscle = part.split("=", 1)
            out[chan.strip()] = muscle.strip()
    return out


def parse_mvc_spec(spec: str | Sequence[str] | dict[str, float] | None) -> dict[str, float]:
    """解析 ``"ECU=80,FDS=57"`` → ``{"ECU": 80.0, "FDS": 57.0}``。"""
    if spec is None:
        return {}
    if isinstance(spec, dict):
        return {str(k).strip(): float(v) for k, v in spec.items()}
    items = [spec] if isinstance(spec, str) else list(spec)
    out: dict[str, float] = {}
    for item in items:
        for part in str(item).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(
                    f"MVC entries must be muscle=value, got {part!r} (e.g. ECU=80,FDS=57)"
                )
            muscle, value = part.split("=", 1)
            out[muscle.strip()] = float(value)
    return out


def resolve_channel_index(token: str, meta: StreamMeta | None = None) -> int:
    """把 ``A1`` / ``1`` / 流里的通道标签 解析成 **0 基** 通道索引。

    给出 ``meta`` 时会顺带做越界检查，避免"映射写错、采集中途才崩"。
    """
    token = str(token).strip()
    labels = list(meta.channel_labels) if meta is not None else []

    idx: int | None = None
    # 1) 优先按流声明的标签精确匹配（大小写不敏感）
    for i, label in enumerate(labels):
        if label and label.strip().lower() == token.lower():
            idx = i
            break
    if idx is None:
        # 2) A1..A8 / ch1..ch8 / 纯数字 → 1 起编号
        low = token.lower()
        for prefix in ("ch", "a"):
            if low.startswith(prefix) and low[len(prefix):].isdigit():
                idx = int(low[len(prefix):]) - 1
                break
        if idx is None and low.isdigit():
            idx = int(low) - 1

    if idx is None or idx < 0:
        hint = f"; stream channel labels: {labels}" if labels else ""
        raise ValueError(
            f"Cannot resolve channel {token!r} to a channel index. "
            f"Supported: A1/A2..., ch1/ch2..., 1/2..., or a stream channel label{hint}"
        )

    if meta is not None and meta.n_channels and idx >= meta.n_channels:
        raise ValueError(
            f"Channel {token!r} points to index {idx + 1}, but the stream has only "
            f"{meta.n_channels} channels; stream labels: {labels or '(none)'}"
        )
    return idx


# --------------------------------------------------------------------------- #
# 多通道流水线
# --------------------------------------------------------------------------- #
@dataclass
class MuscleChannel:
    """一块肌肉 ↔ 一个数据通道。"""

    muscle: str
    index: int
    """0 基通道索引。"""
    token: str = ""
    """用户写下的原始标记（A1 / 1 / EMG1），仅用于日志。"""
    mvc: float = float("nan")
    """MVC 基准（与信号同单位）。NaN 表示未提供 → 该肌肉 %MVC 不可用。"""
    label: str = ""

    @property
    def usable(self) -> bool:
        return self.mvc is not None and np.isfinite(self.mvc) and self.mvc > 0


class MultiChannelMvc:
    """多通道 → 每肌肉 %MVC。所有通道共用同一套窗长/步长，时间轴对齐。"""

    def __init__(
        self,
        fs: float,
        channels: Sequence[MuscleChannel],
        signal_cfg: SignalConfig | None = None,
    ):
        if fs <= 0:
            raise ValueError("fs must be > 0")
        if not channels:
            raise ValueError("At least one muscle channel mapping is required")
        self.fs = float(fs)
        self.cfg = signal_cfg or SignalConfig()
        self.channels = list(channels)
        self.design: FilterDesign = design_filter_chain(
            fs=fs,
            highpass_hz=self.cfg.highpass_hz,
            lowpass_hz=self.cfg.lowpass_hz,
            notch_hz=self.cfg.notch_hz,
            notch_q=self.cfg.notch_q,
            order=self.cfg.filter_order,
        )
        self._filters = [StreamingFilter(self.design) for _ in self.channels]
        self._rms = [
            RmsExtractor(fs, self.cfg.rms_window_sec, self.cfg.rms_hop_sec)
            for _ in self.channels
        ]
        self.n_windows = 0

    # ------------------------------------------------------------------ #
    @classmethod
    def from_spec(
        cls,
        fs: float,
        channel_spec: str | Sequence[str] | dict[str, str],
        mvc_spec: str | Sequence[str] | dict[str, float] | None = None,
        meta: StreamMeta | None = None,
        signal_cfg: SignalConfig | None = None,
    ) -> "MultiChannelMvc":
        """从命令行风格的映射字符串直接构造。"""
        mapping = parse_channel_spec(channel_spec)
        mvc = parse_mvc_spec(mvc_spec)
        channels: list[MuscleChannel] = []
        for token, muscle in mapping.items():
            channels.append(
                MuscleChannel(
                    muscle=muscle,
                    index=resolve_channel_index(token, meta),
                    token=token,
                    mvc=float(mvc.get(muscle, float("nan"))),
                    label=(meta.channel_labels[resolve_channel_index(token, meta)]
                           if meta and meta.channel_labels else ""),
                )
            )
        return cls(fs=fs, channels=channels, signal_cfg=signal_cfg)

    # ------------------------------------------------------------------ #
    @property
    def muscles(self) -> list[str]:
        return [c.muscle for c in self.channels]

    @property
    def missing_mvc(self) -> list[str]:
        return [c.muscle for c in self.channels if not c.usable]

    def describe(self) -> str:
        bits = []
        for c in self.channels:
            mvc = f"{c.mvc:g}" if c.usable else "not set"
            tag = c.token or f"#{c.index + 1}"
            bits.append(f"{tag}->{c.muscle}(MVC={mvc})")
        text = " | ".join(bits)
        if self.missing_mvc:
            text += f"  [missing MVC: {', '.join(self.missing_mvc)} -> %MVC recorded as empty]"
        return text

    # ------------------------------------------------------------------ #
    def push(
        self, data: np.ndarray, timestamps: np.ndarray | None = None
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """送入一块原始样本。

        Returns
        -------
        ``(window_end_times, {肌肉: %MVC 序列})``
        """
        data = np.asarray(data, dtype=float)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.size == 0:
            return np.empty(0), {c.muscle: np.empty(0) for c in self.channels}

        times_out: np.ndarray | None = None
        out: dict[str, np.ndarray] = {}
        for ch, filt, ex in zip(self.channels, self._filters, self._rms):
            if ch.index >= data.shape[1]:
                raise IndexError(
                    f"Channel {ch.token or ch.index + 1} ({ch.muscle}) is out of range: "
                    f"this chunk has only {data.shape[1]} channels"
                )
            raw = data[:, ch.index]
            physical = (raw - self.cfg.adc_offset) * self.cfg.adc_scale
            filtered = filt.process(physical)
            rms_values, rms_times = ex.push(filtered, timestamps)
            if times_out is None:
                times_out = rms_times
            if ch.usable:
                out[ch.muscle] = rms_values / float(ch.mvc) * 100.0
            else:
                out[ch.muscle] = np.full(rms_values.shape, np.nan)
        if times_out is None:  # pragma: no cover - 上面必有一条通道
            times_out = np.empty(0)
        self.n_windows += int(times_out.size)
        return times_out, out

    # ------------------------------------------------------------------ #
    def latest(self, values: dict[str, np.ndarray]) -> dict[str, float]:
        """取每条序列的最后一个值（用于按固定频率推送）。跳过 NaN。"""
        out: dict[str, float] = {}
        for muscle, series in values.items():
            if series.size == 0:
                continue
            v = float(series[-1])
            if np.isfinite(v):
                out[muscle] = v
        return out
