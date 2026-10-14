"""数字信号处理：滤波链、增量 RMS 提取、可增长数组。

设计要点
--------
* **流式**：采集过程中不能回头重算，所以滤波器必须带状态（``StreamingFilter``），
  RMS 必须增量产出（``RmsExtractor``）。
* **离线**：分析已录好的 CSV 时用零相位 ``filtfilt``（``filter_offline``），
  与流式结果会有极小差异，故导出目录里会记录用的是哪条路径。
* **不写死采样率**：说明文档 §6 明确要求用 ``inlet.info().nominal_srate()``
  读取流声明的采样率，本模块所有函数都显式接收 ``fs``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

__all__ = [
    "FilterDesign",
    "design_filter_chain",
    "StreamingFilter",
    "filter_offline",
    "RmsExtractor",
    "GrowableF64",
    "rms",
]


# --------------------------------------------------------------------------- #
# 滤波器设计
# --------------------------------------------------------------------------- #
@dataclass
class FilterDesign:
    """一次滤波链设计的完整描述（含被夹紧后的实际截止频率）。"""

    fs: float
    sos: np.ndarray | None
    highpass_hz: float | None
    lowpass_hz: float | None
    notch_hz: float | None
    order: int
    notes: list[str] = field(default_factory=list)

    @property
    def settle_sec(self) -> float:
        """估算滤波器暂态长度，用于决定要丢弃多少起始数据。"""
        parts = [0.0]
        if self.highpass_hz:
            parts.append(3.0 / max(self.highpass_hz, 1e-6))
        if self.notch_hz:
            parts.append(0.2)
        return max(parts)

    def describe(self) -> str:
        bits = []
        if self.highpass_hz:
            bits.append(f"HP {self.highpass_hz:g} Hz")
        if self.lowpass_hz:
            bits.append(f"LP {self.lowpass_hz:g} Hz")
        if self.notch_hz:
            bits.append(f"NOTCH {self.notch_hz:g} Hz")
        text = " + ".join(bits) if bits else "no filter"
        if self.notes:
            text += " | " + "; ".join(self.notes)
        return f"{text} (fs={self.fs:g} Hz, order={self.order})"


def design_filter_chain(
    fs: float,
    highpass_hz: float | None = 20.0,
    lowpass_hz: float | None = 450.0,
    notch_hz: float | None = 50.0,
    notch_q: float = 30.0,
    order: int = 4,
) -> FilterDesign:
    """设计 EMG 常用滤波链：带通（去 DC/漂移 + 抗混叠）+ 可选工频陷波。

    超出奈奎斯特频率的截止频率会被自动夹紧到 ``0.95 * fs/2``，
    并在 ``FilterDesign.notes`` 里留下说明（低采样率设备很常见）。
    """
    if fs <= 0:
        raise ValueError(f"Sample rate must be > 0, got fs={fs!r}")

    nyq = fs / 2.0
    limit = 0.95 * nyq
    notes: list[str] = []

    hp: float | None = None
    lp: float | None = None

    if highpass_hz and highpass_hz > 0:
        hp = float(highpass_hz)
        if hp >= limit:
            notes.append(f"High-pass {hp:g} Hz exceeds the Nyquist limit; high-pass disabled")
            hp = None
    if lowpass_hz and lowpass_hz > 0:
        lp = float(lowpass_hz)
        if lp > limit:
            notes.append(f"Low-pass {lp:g} Hz clamped to {limit:g} Hz (fs/2 limit)")
            lp = limit

    sos: np.ndarray | None = None
    if hp and lp:
        if hp < lp:
            sos = signal.butter(order, [hp, lp], btype="bandpass", fs=fs, output="sos")
        else:
            notes.append(
                f"High-pass {hp:g} Hz >= low-pass {lp:g} Hz; falling back to high-pass only"
            )
            sos = signal.butter(order, hp, btype="highpass", fs=fs, output="sos")
    elif hp:
        sos = signal.butter(order, hp, btype="highpass", fs=fs, output="sos")
    elif lp:
        sos = signal.butter(order, lp, btype="lowpass", fs=fs, output="sos")
    else:
        notes.append("No cutoff frequencies specified; signal will not be filtered")

    if notch_hz and notch_hz > 0:
        if notch_hz >= limit:
            notes.append(f"Notch {notch_hz:g} Hz exceeds the Nyquist limit; ignored")
            notch_hz = None
        else:
            b, a = signal.iirnotch(float(notch_hz), float(notch_q), fs=fs)
            notch_sos = signal.tf2sos(b, a)
            sos = notch_sos if sos is None else np.vstack([sos, notch_sos])

    return FilterDesign(
        fs=float(fs),
        sos=sos,
        highpass_hz=hp,
        lowpass_hz=lp,
        notch_hz=float(notch_hz) if notch_hz else None,
        order=int(order),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 流式滤波
# --------------------------------------------------------------------------- #
class StreamingFilter:
    """带内部状态的 SOS 滤波器，逐块处理而不产生块间不连续。"""

    def __init__(self, design: FilterDesign):
        self.design = design
        self._zi: np.ndarray | None = None
        self._warmed = False

    def reset(self) -> None:
        self._zi = None
        self._warmed = False

    def warmup(self, chunk: np.ndarray) -> None:
        """用首个数据块"预热"滤波器状态（**不消耗**样本）。

        做法：按首样本的稳态响应初始化状态，再对该块跑一遍建立状态、丢弃输出，
        后续 ``process`` 从该状态继续。这样可显著削弱开机暂态，配合
        ``SignalConfig.startup_skip_sec`` 一起使用。
        """
        if self.design.sos is None or chunk.size == 0:
            self._warmed = True
            return
        zi = signal.sosfilt_zi(self.design.sos) * float(chunk[0])
        _, self._zi = signal.sosfilt(self.design.sos, np.asarray(chunk, dtype=float), zi=zi)
        self._warmed = True

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """滤波一块样本；第一块会自动预热。返回同长度数组。"""
        chunk = np.asarray(chunk, dtype=float)
        if self.design.sos is None:
            return chunk.copy()
        if chunk.size == 0:
            return chunk.copy()
        if not self._warmed:
            self.warmup(chunk)
        out, self._zi = signal.sosfilt(self.design.sos, chunk, zi=self._zi)
        return out


def filter_offline(
    x: np.ndarray, fs: float, design: FilterDesign | None = None, zero_phase: bool = True
) -> np.ndarray:
    """离线（零相位）滤波，用于分析已保存的 CSV。

    ``zero_phase=False`` 时改用与实时路径一致的因果滤波，便于和线上结果对齐。
    """
    x = np.asarray(x, dtype=float)
    design = design or design_filter_chain(fs)
    if design.sos is None or x.size == 0:
        return x.copy()
    padlen = min(3 * (design.sos.shape[0] * 2), max(0, x.size - 1))
    if zero_phase:
        if x.size <= padlen:
            return signal.sosfilt(design.sos, x)
        return signal.sosfiltfilt(design.sos, x, padlen=padlen)
    return signal.sosfilt(design.sos, x)


# --------------------------------------------------------------------------- #
# 增量 RMS
# --------------------------------------------------------------------------- #
def rms(x: np.ndarray) -> float:
    """均方根。空数组返回 0.0。"""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


class RmsExtractor:
    """把连续的滤波样本切成固定长度窗口，增量输出每个窗口的 RMS。

    窗口长度为 ``window_sec``、步长为 ``hop_sec``；``hop < window`` 即为重叠窗。
    每个窗口的**时间戳取窗口末样本的 LSL 时间**，这样与其它程序的
    事件标记天然对齐（说明文档 §4 的时间同步要求）。

    用法::

        ex = RmsExtractor(fs=1000, window_sec=0.125, hop_sec=0.0625)
        while True:
            x, t = source.read_chunk()
            rms_vals, rms_times = ex.push(filtered, t)
    """

    _MAX_BATCH_WINDOWS = 4096

    def __init__(self, fs: float, window_sec: float, hop_sec: float):
        if fs <= 0:
            raise ValueError("fs must be > 0")
        if window_sec <= 0 or hop_sec <= 0:
            raise ValueError("window_sec / hop_sec must be > 0")
        if hop_sec > window_sec:
            raise ValueError("hop_sec must not exceed window_sec (otherwise data would be skipped)")
        self.fs = float(fs)
        self.window_sec = float(window_sec)
        self.hop_sec = float(hop_sec)
        self.window_samples = max(1, int(round(window_sec * fs)))
        self.hop_samples = max(1, int(round(hop_sec * fs)))
        self._buf = np.empty(0, dtype=float)
        self._tbuf = np.empty(0, dtype=float)
        self._emitted = 0

    # ------------------------------------------------------------------ #
    @property
    def emitted(self) -> int:
        """已经产出的 RMS 样本数。"""
        return self._emitted

    @property
    def pending(self) -> int:
        """缓冲区里尚未凑满一个窗口的样本数。"""
        return int(self._buf.size)

    def reset(self) -> None:
        self._buf = np.empty(0, dtype=float)
        self._tbuf = np.empty(0, dtype=float)
        self._emitted = 0

    # ------------------------------------------------------------------ #
    def push(self, x: np.ndarray, t: np.ndarray | None = None):
        """送入一块滤波后的样本，返回 ``(rms_values, end_times)`` 两个数组。"""
        x = np.asarray(x, dtype=float).reshape(-1)
        if x.size == 0:
            return np.empty(0), np.empty(0)
        if t is None:
            t = np.arange(x.size, dtype=float) / self.fs
        t = np.asarray(t, dtype=float).reshape(-1)
        if t.size != x.size:
            raise ValueError(f"Number of timestamps ({t.size}) does not match number of samples ({x.size})")

        self._buf = np.concatenate([self._buf, x])
        self._tbuf = np.concatenate([self._tbuf, t])

        outs: list[np.ndarray] = []
        times: list[np.ndarray] = []
        # 分批计算：单批最多 _MAX_BATCH_WINDOWS 个窗口，避免一次性构造超大索引矩阵
        # （回放很长的 CSV 时 push 的块可能非常大）。
        while self._buf.size >= self.window_samples:
            n_out = 1 + (self._buf.size - self.window_samples) // self.hop_samples
            n_out = min(n_out, self._MAX_BATCH_WINDOWS)

            # 用 stride trick 一次性算完这一批窗口，避免 Python 层逐窗循环
            starts = np.arange(n_out) * self.hop_samples
            idx = starts[:, None] + np.arange(self.window_samples)[None, :]
            windows = self._buf[idx]
            outs.append(np.sqrt(np.mean(windows * windows, axis=1)))
            times.append(self._tbuf[starts + self.window_samples - 1])

            consumed = n_out * self.hop_samples
            self._buf = self._buf[consumed:]
            self._tbuf = self._tbuf[consumed:]
            self._emitted += n_out

        if not outs:
            return np.empty(0), np.empty(0)
        return np.concatenate(outs), np.concatenate(times)


# --------------------------------------------------------------------------- #
# 可增长数组（APDF 需要存整段记录的全部 RMS 值）
# --------------------------------------------------------------------------- #
class GrowableF64:
    """容量翻倍的 float64 动态数组。

    APDF 需要对**整个记录过程**的幅值序列做排序统计，所以不能用环形缓冲
    （环形缓冲会丢掉早期数据，得到的就不是全程 APDF 了）。8 小时 ×
    16 Hz 的 RMS 序列约 46 万个样本 ≈ 3.7 MB，完全放得下内存。
    """

    def __init__(self, initial_capacity: int = 1 << 14):
        self._data = np.empty(max(1, initial_capacity), dtype=np.float64)
        self._size = 0

    def __len__(self) -> int:
        return self._size

    @property
    def size(self) -> int:
        return self._size

    def append(self, values) -> None:
        arr = np.asarray(values, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return
        self._ensure(self._size + arr.size)
        self._data[self._size : self._size + arr.size] = arr
        self._size += arr.size

    def extend(self, values) -> None:
        self.append(values)

    def _ensure(self, needed: int) -> None:
        if needed <= self._data.size:
            return
        cap = self._data.size
        while cap < needed:
            cap *= 2
        grown = np.empty(cap, dtype=np.float64)
        grown[: self._size] = self._data[: self._size]
        self._data = grown

    def to_array(self) -> np.ndarray:
        """返回**拷贝**，调用方可安全持有。"""
        return self._data[: self._size].copy()

    def view(self) -> np.ndarray:
        """返回只读视图（无拷贝，仅供即时计算）。"""
        out = self._data[: self._size]
        out.setflags(write=False)
        return out

    def clear(self) -> None:
        self._size = 0
