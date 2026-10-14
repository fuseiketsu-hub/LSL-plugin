"""数据源层：LSL 接收端 + 合成源 + 回放源。

三种数据源实现同一个 :class:`SampleSource` 接口，因此上层（会话编排、GUI、
CLI、测试）完全不关心数据从哪来：

* :class:`LSLSource`      —— 真实 OpenSignals 流（说明文档 §3.2 的三种解析方式）。
* :class:`SyntheticSource`—— 内置 EMG 仿真，**无需硬件、无需 pylsl** 即可跑通
  整条链路，并给出可解析验证的 APDF 真值。
* :class:`ReplaySource`   —— 回放已导出的 ``raw.csv``，用于事后复算。
* :class:`OpenSignalsSource` —— 导入 OpenSignals(r)evolution 导出的 ``.txt``
  （原始 ADC），让已录好的数据走同一条滤波 → RMS → %MVC → APDF 分析链路。

LSL 相关约定（与说明文档一致）：

* OpenSignals 的流名称为 ``OpenSignals``，而 **``type`` 字段是设备 MAC 地址**，
  所以要定位某台设备应使用 ``resolve_stream("type", mac)``。
* 采样率一律从 ``inlet.info().nominal_srate()`` 读取，绝不写死。
* 通道标签/单位从 ``desc/channels/channel/{label,unit}`` 解析。
"""

from __future__ import annotations

import abc
import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .config import StreamConfig
from .dsp import StreamingFilter, design_filter_chain

try:  # pragma: no cover - 取决于运行环境
    import pylsl

    PYLSL_AVAILABLE = True
    PYLSL_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover
    pylsl = None  # type: ignore[assignment]
    PYLSL_AVAILABLE = False
    PYLSL_IMPORT_ERROR = str(exc)

__all__ = [
    "PYLSL_AVAILABLE",
    "PYLSL_IMPORT_ERROR",
    "StreamMeta",
    "SampleChunk",
    "SampleSource",
    "LSLSource",
    "SyntheticSource",
    "ReplaySource",
    "OpenSignalsSource",
    "load_opensignals_txt",
    "is_opensignals_txt",
    "StreamNotFoundError",
    "discover_streams",
    "require_pylsl",
]


class StreamNotFoundError(RuntimeError):
    """解析不到 LSL 流时抛出（含排查建议）。"""

    def __init__(self, message: str):
        super().__init__(
            message
            + "\nTroubleshooting checklist (see docs §6):"
            "\n  1) OpenSignals -> Settings -> INTEGRATIONS: is Lab Streaming Layer enabled?"
            "\n  2) Has the red record button been pressed? (the stream is sent only while recording)"
            "\n  3) Is a firewall blocking it? (usually not an issue when both run on the same PC)"
            "\n  4) Run `python -m lsl_apdf list` to see all discoverable streams."
        )


def require_pylsl() -> None:
    if not PYLSL_AVAILABLE:
        raise RuntimeError(
            "pylsl is not installed; cannot receive LSL streams. Run: pip install pylsl\n"
            f"(import error: {PYLSL_IMPORT_ERROR})"
        )


# --------------------------------------------------------------------------- #
# 基础数据结构
# --------------------------------------------------------------------------- #
@dataclass
class StreamMeta:
    """流的元数据（描述文档 §3.2 中 ``recv_stream_metadata`` 解析到的信息）。"""

    name: str = ""
    type: str = ""
    source_id: str = ""
    hostname: str = ""
    n_channels: int = 0
    nominal_srate: float = 0.0
    channel_labels: list[str] = field(default_factory=list)
    channel_units: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def label_for(self, channel_1based: int, fallback: str = "ch") -> str:
        idx = channel_1based - 1
        if 0 <= idx < len(self.channel_labels) and self.channel_labels[idx]:
            return self.channel_labels[idx]
        return f"{fallback}{channel_1based}"

    def unit_for(self, channel_1based: int, fallback: str = "a.u.") -> str:
        idx = channel_1based - 1
        if 0 <= idx < len(self.channel_units) and self.channel_units[idx]:
            return self.channel_units[idx]
        return fallback

    def describe(self) -> str:
        chans = ", ".join(
            f"{i + 1}:{self.label_for(i + 1)}({self.unit_for(i + 1)})"
            for i in range(self.n_channels)
        )
        if chans and not any(self.channel_labels):
            chans += " (labels are read from inlet.info() after connecting)"
        return (
            f"name={self.name} | type(MAC)={self.type} | hostname={self.hostname} | "
            f"channels={self.n_channels} | srate={self.nominal_srate:g} Hz"
            + (f" | labels=[{chans}]" if chans else "")
        )


@dataclass
class SampleChunk:
    """一块样本：``data`` 形状 ``(n_samples, n_channels)``。"""

    data: np.ndarray
    timestamps: np.ndarray
    first_sample_index: int = 0

    def __len__(self) -> int:
        return int(self.data.shape[0])

    @property
    def n_channels(self) -> int:
        return int(self.data.shape[1]) if self.data.ndim == 2 else 0

    def channel(self, channel_1based: int) -> np.ndarray:
        idx = channel_1based - 1
        if not (0 <= idx < self.n_channels):
            raise IndexError(
                f"Channel {channel_1based} does not exist (stream has {self.n_channels} channels)"
            )
        return self.data[:, idx]


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class SampleSource(abc.ABC):
    """统一数据源接口。"""

    def __init__(self) -> None:
        self._meta = StreamMeta()
        self._connected = False
        self._n_samples = 0
        self._n_chunks = 0
        self.warnings: list[str] = []
        """连接期间产生的、需要让上层（会话/界面/日志）知道的提示。

        典型场景：按 MAC 解析时发现了**多个**同名流（比如上一次的模拟发布端
        还在跑），此时随便挑一个可能连到错误的设备上，必须提醒用户。
        """

    # -- 生命周期 ------------------------------------------------------- #
    @abc.abstractmethod
    def connect(self) -> StreamMeta: ...

    @abc.abstractmethod
    def read_chunk(self, timeout: float = 0.05) -> SampleChunk | None: ...

    def close(self) -> None:
        self._connected = False

    # -- 只读属性 ------------------------------------------------------- #
    @property
    def meta(self) -> StreamMeta:
        return self._meta

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def srate(self) -> float:
        return float(self._meta.nominal_srate)

    @property
    def n_samples(self) -> int:
        return self._n_samples

    @property
    def n_chunks(self) -> int:
        return self._n_chunks

    def __enter__(self) -> "SampleSource":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# LSL
# --------------------------------------------------------------------------- #
def discover_streams(wait_time: float = 1.0) -> list[StreamMeta]:
    """列出当前网络/本机可见的所有 LSL 流（GUI 的"刷新流列表"用它）。

    .. warning::
       liblsl 的解析服务只回传**短信息**，``resolve_streams()`` 拿到的
       ``StreamInfo`` 里 ``<desc>`` 是空的 —— 也就是说此时**取不到通道
       label/unit**，只有 name/type/hostname/通道数/采样率。完整的通道描述
       要等建立 inlet 之后从 ``inlet.info()`` 读取（说明文档 §3.2 的
       ``recv_stream_metadata`` 正是这么做的）。本函数因此返回的
       ``channel_labels`` 通常为空，这是协议行为而非 bug。
    """
    require_pylsl()
    found: list[StreamMeta] = []
    for info in pylsl.resolve_streams(wait_time=wait_time):
        found.append(_meta_from_stream_info(info))
    return found


def _meta_from_stream_info(info: Any) -> StreamMeta:
    meta = StreamMeta(
        name=info.name() or "",
        type=info.type() or "",
        source_id=info.source_id() or "",
        hostname=info.hostname() or "",
        n_channels=int(info.channel_count() or 0),
        nominal_srate=float(info.nominal_srate() or 0.0),
        created_at=float(info.created_at() or 0.0),
    )
    meta.channel_labels, meta.channel_units = _parse_channel_desc(info, meta.n_channels)
    return meta


def _parse_channel_desc(info: Any, n_channels: int) -> tuple[list[str], list[str]]:
    """按说明文档 §3.2 的方式遍历 ``desc/channels/channel`` 取 label 与 unit。

    不同版本的 OpenSignals / pylsl 对 XML 细节处理略有差异，因此这里做足防御：
    解析失败时退回空标签，由界面显示 ``ch1``、``ch2``……，绝不因为元数据
    格式问题导致采集起不来。
    """
    labels: list[str] = []
    units: list[str] = []
    try:
        desc = info.desc()
        channels = desc.child("channels").child("channel")
    except Exception:
        return labels, units
    try:
        for _ in range(max(0, n_channels)):
            try:
                labels.append(channels.child_value("label") or "")
            except Exception:
                labels.append("")
            try:
                units.append(channels.child_value("unit") or "")
            except Exception:
                units.append("")
            try:
                channels = channels.next_sibling()
            except Exception:
                break
    except Exception:
        pass
    return labels, units


class LSLSource(SampleSource):
    """从 OpenSignals 的 LSL 流接收数据。

    Parameters
    ----------
    cfg:
        流定位与 inlet 参数（见 :class:`~lsl_apdf.config.StreamConfig`）。
    stream_info:
        已经解析好的 ``pylsl.StreamInfo``；提供时跳过再次解析（GUI 从列表里
        选流就是这条路径，避免"选中的流"和"再次解析到的流"不是同一个）。
    """

    def __init__(self, cfg: StreamConfig | None = None, stream_info: Any | None = None):
        super().__init__()
        self.cfg = cfg or StreamConfig()
        self._stream_info = stream_info
        self._inlet: Any | None = None
        self._first_lsl_t: float | None = None
        self._wall_t0: float | None = None

    # ------------------------------------------------------------------ #
    def connect(self) -> StreamMeta:
        require_pylsl()
        info = self._stream_info
        if info is None:
            prop, value = self.cfg.resolve_property()
            found = pylsl.resolve_byprop(prop, value, timeout=self.cfg.resolve_timeout)
            if not found:
                self._connected = False
                raise StreamNotFoundError(
                    f"No LSL stream matching {prop}={value!r} was found within "
                    f"{self.cfg.resolve_timeout:g} s."
                )
            info = found[0]
            if len(found) > 1:
                others = "; ".join(
                    _meta_from_stream_info(i).describe() for i in found[1:4]
                )
                self.warnings.append(
                    f"Found {len(found)} streams matching {prop}={value!r}; using the first one. "
                    f"Other candidates: {others}. "
                    "If the wrong device was picked (e.g. a previous publisher is still running), "
                    "close the extra programs or select the target stream from the list in the GUI."
                )
        self._stream_info = info
        self._inlet = pylsl.StreamInlet(
            info,
            max_buflen=int(self.cfg.max_buflen),
            recover=bool(self.cfg.recover),
        )
        # 通道 label/unit 只能从 **inlet.info()** 读到：resolve 返回的是短信息，
        # <desc> 为空（实测 liblsl 行为）。说明文档 §3.2 也是用 inlet.info()。
        try:
            self._meta = _meta_from_stream_info(self._inlet.info())
        except Exception:  # pragma: no cover - 退回短信息
            self._meta = _meta_from_stream_info(info)
        if not self._meta.n_channels:
            self._meta.n_channels = int(info.channel_count() or 0)
        if not self._meta.nominal_srate:
            self._meta.nominal_srate = float(info.nominal_srate() or 0.0)
        self._connected = True
        self._first_lsl_t = None
        self._wall_t0 = time.time()
        return self._meta

    def read_chunk(self, timeout: float = 0.05) -> SampleChunk | None:
        if self._inlet is None:
            raise RuntimeError("connect() must be called before read_chunk()")
        samples, timestamps = self._inlet.pull_chunk(timeout=timeout, max_samples=4096)
        if not samples:
            return None
        data = np.asarray(samples, dtype=float)
        if data.ndim == 1:  # 单通道流在某些版本下会退化成一维
            data = data.reshape(-1, 1)
        ts = np.asarray(timestamps, dtype=float)
        if ts.size != data.shape[0]:  # pragma: no cover - 防御
            ts = np.arange(data.shape[0], dtype=float) / max(self.srate, 1.0)

        if self._first_lsl_t is None and ts.size:
            self._first_lsl_t = float(ts[0])
        chunk = SampleChunk(
            data=data,
            timestamps=ts,
            first_sample_index=self._n_samples,
        )
        self._n_samples += data.shape[0]
        self._n_chunks += 1
        return chunk

    # ------------------------------------------------------------------ #
    @property
    def time_correction(self) -> float:
        """LSL 时钟与本机时钟的偏移，用于跨程序时间对齐日志。"""
        if self._inlet is None:
            return 0.0
        try:
            return float(self._inlet.time_correction())
        except Exception:  # pragma: no cover
            return 0.0

    def elapsed_sec(self) -> float:
        if self._wall_t0 is None:
            return 0.0
        return time.time() - self._wall_t0

    def close(self) -> None:
        if self._inlet is not None:
            try:
                self._inlet.close_stream()
            except Exception:  # pragma: no cover
                pass
            self._inlet = None
        super().close()


# --------------------------------------------------------------------------- #
# 合成源（无硬件验证 / 单元测试真值）
# --------------------------------------------------------------------------- #
@dataclass
class SyntheticProfile:
    """合成信号的幅值包络定义。

    ``segments`` 为 ``[(时长秒, 包络=%MVE), ...]`` 的分段常值序列，循环播放；
    这样 APDF 有**解析真值**：处于水平 L 以下的时间占比是已知的，
    因此可以严格校验整条链路（滤波 → RMS → %MVE → APDF）。
    """

    segments: list[tuple[float, float]] = field(
        default_factory=lambda: [(30.0, 5.0), (20.0, 60.0), (30.0, 5.0)]
    )
    noise_floor_pct: float = 1.5
    """静息底噪水平（%MVE），叠加在分段包络上。"""
    mve_unit: float = 1000.0
    """MVE 参考值对应的信号单位幅值（例如 ADC 码）。"""
    dc_offset: float = 1650.0
    """直流偏置（模拟 BITalino 的 VCC/2 中点），用于验证高通去 DC。"""
    line_noise_pct: float = 0.0
    """50/60 Hz 工频干扰强度（%MVE），用于验证陷波器。"""
    line_freq: float = 50.0
    fatigue_drift: float = 0.0
    """包络随时间下降的百分比（0.3 表示结束时幅值降到 70%）。"""

    @classmethod
    def calibration(cls, rest_sec: float = 5.0, mvc_sec: float = 5.0, trials: int = 3,
                    between_sec: float = 20.0, mvc_pct: float = 100.0,
                    cycle: bool = True) -> "SyntheticProfile":
        """构造"静息 → 若干次 MVC"的标定用包络，可用于端到端验证标定流程。"""
        segments: list[tuple[float, float]] = [(rest_sec, 0.0)]
        for i in range(trials):
            segments.append((mvc_sec, mvc_pct))
            if i != trials - 1:
                segments.append((between_sec, 0.0))
        if cycle:
            segments.append((between_sec, 0.0))
        return cls(segments=segments, noise_floor_pct=1.0)

    def envelope_pct(self, t: np.ndarray) -> np.ndarray:
        """在时间 ``t``（相对起点的秒数）上的包络值（%MVE）。"""
        t = np.asarray(t, dtype=float)
        if not self.segments:
            return np.zeros_like(t)
        durations = np.asarray([max(1e-9, s[0]) for s in self.segments], dtype=float)
        levels = np.asarray([s[1] for s in self.segments], dtype=float)
        period = float(np.sum(durations))
        phase = np.mod(t, period)
        cum = np.concatenate([[0.0], np.cumsum(durations)])
        idx = np.clip(np.searchsorted(cum, phase, side="right") - 1, 0, len(levels) - 1)
        env = levels[idx]
        if self.fatigue_drift > 0:
            env = env * (1.0 - self.fatigue_drift * (phase / period))
        return env

    def analytic_apdf_pct(self) -> dict[str, float]:
        """分段常值包络下的解析时间占比：返回 {水平%MVE: 时间占比}。

        仅统计包络本身（不含底噪、不含段间过渡），用于与实测 APDF 对照。
        """
        total = sum(max(1e-9, d) for d, _ in self.segments)
        shares: dict[float, float] = {}
        for d, level in self.segments:
            key = round(float(level) + float(self.noise_floor_pct), 6)
            shares[key] = shares.get(key, 0.0) + max(1e-9, d) / total
        return shares


class SyntheticSource(SampleSource):
    """内置 EMG 仿真源：带限噪声 × 包络 + 可选工频干扰 / 疲劳漂移。

    特点：

    * 包络可分段设定（见 :class:`SyntheticProfile`），因此 APDF 有解析真值；
    * ``realtime=True`` 时按声明的采样率节流输出（GUI / 实时链路测试用）；
      ``realtime=False`` 时尽快吐出（单元测试用）。
    """

    def __init__(
        self,
        profile: SyntheticProfile | None = None,
        srate: float = 1000.0,
        n_channels: int = 1,
        realtime: bool = True,
        chunk_sec: float = 0.05,
        channel_label: str = "EMG",
        channel_unit: str = "a.u.",
        seed: int | None = 20240517,
        profiles: Sequence[SyntheticProfile] | None = None,
        channel_labels: Sequence[str] | None = None,
    ):
        super().__init__()
        self.profile = profile or SyntheticProfile()
        #: 每通道各自的包络轮廓（长度须等于 n_channels）；给出时优先于 ``profile``。
        #: 用于模拟"不同肌肉在不同 DPI 下负荷不同"这类多肌肉场景。
        self.profiles = list(profiles) if profiles else None
        self._srate = float(srate)
        self.n_channels = int(n_channels)
        if self.profiles is not None and len(self.profiles) != self.n_channels:
            raise ValueError(
                f"profiles length ({len(self.profiles)}) must equal n_channels ({self.n_channels})"
            )
        self.realtime = bool(realtime)
        self.chunk_sec = float(chunk_sec)
        self.channel_label = channel_label
        self._channel_labels = list(channel_labels) if channel_labels else None
        self.channel_unit = channel_unit
        self._rng = np.random.default_rng(seed)
        self._rngs = [
            np.random.default_rng((seed if seed is not None else 0) + 1000 + i)
            for i in range(self.n_channels)
        ]
        self._t = 0.0
        self._index = 0
        self._wall_next: float | None = None
        # 生成用的带通滤波：把白噪声整形成 EMG 频段（每通道一套独立状态）
        hp = 20.0
        lp = min(450.0, 0.45 * self._srate)
        self._shape_design = design_filter_chain(
            self._srate, highpass_hz=hp, lowpass_hz=lp, notch_hz=None, order=4
        )
        self._shape_filters = [
            StreamingFilter(self._shape_design) for _ in range(max(1, self.n_channels))
        ]
        self._noise_normalizer = self._measure_noise_normalizer()

    @property
    def srate(self) -> float:
        """合成源在构造时就知道采样率，不必等到 connect()。"""
        return self._srate

    def profile_for(self, channel_index: int) -> SyntheticProfile:
        """取第 channel_index（0 基）通道的包络轮廓。"""
        if self.profiles:
            return self.profiles[channel_index]
        return self.profile

    # ------------------------------------------------------------------ #
    def _measure_noise_normalizer(self) -> float:
        """测出带通白噪声的 RMS，用于把噪声归一化到单位功率。

        这样"包络值"就等于"RMS 幅值"，合成源的 %MVE 真值才严格成立。
        """
        probe = self._rng.standard_normal(int(max(self._srate, 1000) * 20))
        design = self._shape_design
        if design.sos is None:
            filtered = probe
        else:
            from scipy import signal as _sig

            filtered = _sig.sosfilt(design.sos, probe)
        filtered = filtered[int(self._srate * 2):]  # 丢弃暂态
        rms = float(np.sqrt(np.mean(filtered ** 2)))
        return rms if rms > 1e-12 else 1.0

    # ------------------------------------------------------------------ #
    def connect(self) -> StreamMeta:
        labels = self._channel_labels
        if labels is None:
            if self.n_channels > 1:
                labels = [f"{self.channel_label}{i + 1}" for i in range(self.n_channels)]
            else:
                labels = [self.channel_label]
        self._meta = StreamMeta(
            name="SyntheticEMG",
            type="00:00:00:00:00:00",
            source_id="lsl_apdf.synthetic",
            hostname="localhost",
            n_channels=self.n_channels,
            nominal_srate=self._srate,
            channel_labels=list(labels),
            channel_units=[self.channel_unit] * self.n_channels,
            created_at=time.time(),
        )
        self._connected = True
        self._t = 0.0
        self._index = 0
        self._wall_next = None
        for f in self._shape_filters:
            f.reset()
        return self._meta

    def read_chunk(self, timeout: float = 0.05) -> SampleChunk | None:
        n = max(1, int(round(self.chunk_sec * self._srate)))
        if self.realtime:
            now = time.time()
            if self._wall_next is None:
                self._wall_next = now
            sleep = self._wall_next - now
            if sleep > 0:
                time.sleep(sleep)
            self._wall_next += self.chunk_sec
            if self._wall_next < now - 5.0 * self.chunk_sec:
                self._wall_next = now  # 落后过多则重新对齐，避免无限追赶

        t = self._t + np.arange(n, dtype=float) / self._srate
        cols = []
        for ch in range(max(1, self.n_channels)):
            prof = self.profile_for(ch)
            env_pct = prof.envelope_pct(t) + prof.noise_floor_pct
            amp = env_pct / 100.0 * prof.mve_unit
            noise = self._rngs[ch % len(self._rngs)].standard_normal(n)
            shaped = self._shape_filters[ch % len(self._shape_filters)].process(noise)
            shaped = shaped / self._noise_normalizer
            sig = amp * shaped + prof.dc_offset
            if prof.line_noise_pct > 0:
                sig = sig + (
                    prof.line_noise_pct
                    / 100.0
                    * prof.mve_unit
                    * np.sin(2 * np.pi * prof.line_freq * t)
                )
            cols.append(sig)
        data = (
            np.stack(cols, axis=1) if cols else np.zeros((n, 0), dtype=float)
        )
        if self.n_channels == 1 and data.shape[1] == 1:
            pass  # 保持 (n, 1) 形状，与 LSL 单通道流一致

        self._t = float(t[-1] + 1.0 / self.srate)
        self._index += n
        self._n_samples += n
        self._n_chunks += 1
        return SampleChunk(
            data=data,
            timestamps=t.copy(),
            first_sample_index=self._index - n,
        )


# --------------------------------------------------------------------------- #
# 回放源
# --------------------------------------------------------------------------- #
class ReplaySource(SampleSource):
    """回放导出的 ``raw.csv``（或任意含时间列/数值列的 CSV）。

    列约定：第一列 LSL 时间戳，其后各列为通道值（与 ``recording.py`` 写出的
    ``raw.csv`` 一致）。也接受纯数值矩阵（无表头）。
    """

    is_file = True
    """文件型数据源：读完自动 ``finished``，引擎据此收尾（不用于实时流）。"""

    def __init__(
        self,
        path: str | Path,
        realtime: bool = False,
        time_column: int = 0,
        channel_columns: Sequence[int] | None = None,
        chunk_sec: float = 0.05,
    ):
        super().__init__()
        self.path = Path(path)
        self.realtime = bool(realtime)
        self.time_column = int(time_column)
        self.channel_columns = list(channel_columns) if channel_columns else None
        self.chunk_sec = float(chunk_sec)
        self._values: np.ndarray | None = None
        self._times: np.ndarray | None = None
        self._pos = 0
        self._wall_next: float | None = None

    @property
    def finished(self) -> bool:
        """文件是否已回放完毕（``connect()`` 之前恒为 False）。"""
        return (self._values is not None
                and self._pos >= self._values.shape[0])

    # ------------------------------------------------------------------ #
    def connect(self) -> StreamMeta:
        times, values = _load_csv_channels(
            self.path, self.time_column, self.channel_columns
        )
        self._times = times
        self._values = values
        n = values.shape[0]
        if n > 1:
            dt = float(np.median(np.diff(times)))
            srate = 1.0 / dt if dt > 0 else 0.0
        else:  # pragma: no cover
            srate = 0.0
        self._meta = StreamMeta(
            name=f"Replay:{self.path.name}",
            type="replay",
            source_id=str(self.path),
            hostname="local",
            n_channels=values.shape[1] if values.ndim == 2 else 1,
            nominal_srate=srate,
            channel_labels=[f"ch{i + 1}" for i in range(values.shape[1])],
            channel_units=["a.u."] * values.shape[1],
            created_at=time.time(),
        )
        self._pos = 0
        self._connected = True
        self._wall_next = None
        return self._meta

    def read_chunk(self, timeout: float = 0.05) -> SampleChunk | None:
        if self._values is None or self._times is None:
            raise RuntimeError("connect() must be called before read_chunk()")
        if self._pos >= self._values.shape[0]:
            return None
        srate = max(self.srate, 1e-9)
        n = max(1, int(round(self.chunk_sec * srate)))
        end = min(self._pos + n, self._values.shape[0])
        data = self._values[self._pos:end]
        times = self._times[self._pos:end]
        if self.realtime:
            now = time.time()
            if self._wall_next is None:
                self._wall_next = now
            sleep = self._wall_next - now
            if sleep > 1e-4:
                time.sleep(sleep)
            self._wall_next += (end - self._pos) / srate
        chunk = SampleChunk(
            data=data, timestamps=times.copy(), first_sample_index=self._pos
        )
        self._pos = end
        self._n_samples += chunk.data.shape[0]
        self._n_chunks += 1
        return chunk


def _load_csv_channels(
    path: Path, time_column: int, channel_columns: Sequence[int] | None
) -> tuple[np.ndarray, np.ndarray]:
    """读取 CSV，自动跳过表头/注释行。返回 ``(时间, 通道矩阵)``。

    ``channel_columns=None`` 时会自动**排除时间列**：既排除 ``time_column``，
    也排除表头里叫 ``lsl_time`` / ``local_time`` / ``timestamp`` 的列。
    本插件导出的 ``raw.csv`` 前两列正是 ``lsl_time, local_time``，
    不排除的话回放时会把时间戳当成一路信号。
    """
    path = Path(path)
    rows: list[list[float]] = []
    header: list[str] | None = None
    with path.open("r", encoding="utf-8", newline="") as fh:
        for raw in csv.reader(fh):
            if not raw:
                continue
            if raw[0].lstrip().startswith("#"):
                continue
            try:
                rows.append([float(c) for c in raw])
            except ValueError:
                if header is None:
                    header = [c.strip() for c in raw]
                continue
    if not rows:
        raise ValueError(f"No numeric rows parsed from {path}")
    width = max(len(r) for r in rows)
    rows = [r for r in rows if len(r) == width]
    arr = np.asarray(rows, dtype=float)

    if channel_columns:
        values = arr[:, list(channel_columns)]
    else:
        time_names = {"lsl_time", "local_time", "time", "timestamp", "t"}
        skip = {time_column}
        if header is not None and len(header) == width:
            for i, name in enumerate(header):
                if name.strip().lower() in time_names:
                    skip.add(i)
        keep = [i for i in range(width) if i not in skip]
        if not keep:  # 只有一列且被判定为时间列 —— 退化为"除时间列外全要"
            keep = [i for i in range(width) if i != time_column]
        values = arr[:, keep]
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    return arr[:, time_column], values


# --------------------------------------------------------------------------- #
# OpenSignals 导出文件（.txt）
# --------------------------------------------------------------------------- #
def is_opensignals_txt(path: str | Path) -> bool:
    """文件是不是 OpenSignals(r)evolution 导出的文本（看首行魔数）。"""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    return "opensignals text file format" in first.lstrip("\ufeff").lower()


def load_opensignals_txt(
    path: str | Path,
    channel_columns: Sequence[int] | None = None,
) -> tuple[np.ndarray, list[str], float, dict[str, Any]]:
    """解析 OpenSignals(r)evolution 导出的 ``.txt``。

    文件结构：``# OpenSignals Text File Format``（魔数）→ ``# {JSON 元数据}``
    → ``# EndOfHeader`` → 制表符分隔的数值行。数据行的第一列是**循环的**块
    序号（nSeq 0..15），不是时间也不是采样计数 —— 时间轴因此按
    ``行号 / 元数据里的 sampling rate`` 重建。

    Returns
    -------
    ``(values, labels, srate, info)``：通道矩阵 ``(n, m)``、每列标签（取自
    元数据 ``column``）、采样率、设备元数据字典。
    """
    path = Path(path)
    info: dict[str, Any] = {}
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                body = line.lstrip("#").strip()
                if not info and body.startswith("{"):
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        for entry in parsed.values():
                            if isinstance(entry, dict) and "sampling rate" in entry:
                                info = entry
                                break
                continue
            try:
                rows.append([float(tok) for tok in line.replace(",", " ").split()])
            except ValueError:
                continue
    if not rows:
        raise ValueError(f"No numeric rows parsed from {path} (is this an OpenSignals export file?)")
    width = max(len(r) for r in rows)
    rows = [r for r in rows if len(r) == width]
    arr = np.asarray(rows, dtype=float)

    columns = info.get("column")
    if isinstance(columns, list) and len(columns) == width:
        labels = [str(c) for c in columns]
    elif isinstance(info.get("label"), list) and len(info["label"]) == width:
        labels = [str(c) for c in info["label"]]
    else:
        labels = [f"ch{i + 1}" for i in range(width)]

    if channel_columns:
        keep = list(channel_columns)
        values = arr[:, keep]
        labels = [labels[i] for i in keep]
    else:
        values = arr

    srate = float(info.get("sampling rate") or 0.0)
    if srate <= 0:
        raise ValueError(
            f"Metadata of {path} has no sampling rate; cannot rebuild the time axis "
            "(make sure this is a raw OpenSignals .txt export)"
        )
    return values, labels, srate, info


class OpenSignalsSource(ReplaySource):
    """导入 OpenSignals 导出的 ``.txt``，当作数据源喂给分析链路。

    与 :class:`ReplaySource` 的区别只在 ``connect()``：时间轴按文件行号与
    元数据采样率重建；通道标签取自元数据的 ``column``（nSeq/I1/I2/O1/O2/
    A1..A6），所以界面里的映射 ``A1=ECU,A2=FDS,...`` 与实时流完全一致。
    默认 ``realtime=False``：导入即尽快跑完。
    """

    def __init__(
        self,
        path: str | Path,
        realtime: bool = False,
        chunk_sec: float = 0.05,
        channel_columns: Sequence[int] | None = None,
    ):
        super().__init__(
            path, realtime=realtime, time_column=0,
            channel_columns=channel_columns, chunk_sec=chunk_sec,
        )

    def connect(self) -> StreamMeta:
        values, labels, srate, info = load_opensignals_txt(
            self.path, self.channel_columns
        )
        self._values = values
        self._times = np.arange(values.shape[0], dtype=float) / srate
        n_ch = int(values.shape[1])
        self._meta = StreamMeta(
            name=f"OpenSignals:{self.path.name}",
            type="opensignals-file",
            source_id=str(self.path),
            hostname=str(info.get("device name") or "local"),
            n_channels=n_ch,
            nominal_srate=srate,
            channel_labels=list(labels),
            channel_units=["ADC"] * n_ch,
            created_at=time.time(),
        )
        if info.get("convertedValues"):
            self.warnings.append("This export contains converted physical values (convertedValues=1); "
                                 "make sure the ADC scale and MVC baseline use the same unit")
        self._pos = 0
        self._connected = True
        self._wall_next = None
        return self._meta
