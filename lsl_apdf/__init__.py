"""lsl_apdf —— OpenSignals LSL 实时信号导出 + APDF 曲线插件。

依据 ``OpenSignals_LSL使用说明.md`` 实现：从 OpenSignals 发布的 LSL 流实时
接收 EMG 原始数据，做偏移去除 / 带通滤波 / 增量 RMS，按 **%MVE 归一化** 后
实时绘制并导出 APDF（幅值概率分布函数）曲线。

最短上手
--------
::

    python -m lsl_apdf gui        # 打开实时界面（推荐）
    python -m lsl_apdf mock       # 另开一个终端：无硬件模拟 OpenSignals 流
    python -m lsl_apdf run --duration 60
    python -m lsl_apdf list       # 查看当前可见的 LSL 流

作为插件嵌入你自己的管道
------------------------
::

    from lsl_apdf import AppConfig, AcquisitionSession, LSLSource

    cfg = AppConfig()
    cfg.mve_ref = 1.23                      # 或用 session.start_calibration() 标定
    session = AcquisitionSession(cfg, LSLSource(cfg.stream))
    session.open()
    session.add_apdf_callback(lambda r: print(r.static_level, r.median_level, r.peak_level))
    session.run(duration=300)
"""

from __future__ import annotations

__version__ = "1.0.0"

from .apdf import (
    ApdfAccumulator,
    ApdfResult,
    compute_apdf,
    exceedance_probability,
    level_percentiles,
    levels_at_probabilities,
    scale_invariance_note,
)
from .channels import (
    MultiChannelMvc,
    MuscleChannel,
    parse_channel_spec,
    parse_mvc_spec,
    resolve_channel_index,
)
from .emg_link import MvcCsvRecorder, MvcTcpSender, format_line
from .calibration import (
    CalibrationOutcome,
    CalibrationPhase,
    CalibrationState,
    MvcCalibrator,
    TrialRecord,
)
from .config import (
    ApdfConfig,
    AppConfig,
    CalibrationConfig,
    ExportConfig,
    SignalConfig,
    StreamConfig,
)
from .dsp import (
    FilterDesign,
    GrowableF64,
    RmsExtractor,
    StreamingFilter,
    design_filter_chain,
    filter_offline,
    rms,
)
from .recording import ProcessedOutlet, SessionPaths, SessionWriter, export_apdf_result
from .session import AcquisitionSession, SessionSnapshot, SessionStats
from .sources import (
    PYLSL_AVAILABLE,
    LSLSource,
    OpenSignalsSource,
    ReplaySource,
    SampleChunk,
    SampleSource,
    StreamMeta,
    StreamNotFoundError,
    SyntheticProfile,
    SyntheticSource,
    discover_streams,
    is_opensignals_txt,
    load_opensignals_txt,
)
from .units import PRESETS, UnitPreset, get_preset, preset_names

__all__ = [
    "__version__",
    # 配置
    "AppConfig",
    "StreamConfig",
    "SignalConfig",
    "CalibrationConfig",
    "ApdfConfig",
    "ExportConfig",
    # 数据源
    "SampleSource",
    "SampleChunk",
    "StreamMeta",
    "LSLSource",
    "SyntheticSource",
    "SyntheticProfile",
    "ReplaySource",
    "OpenSignalsSource",
    "is_opensignals_txt",
    "load_opensignals_txt",
    "StreamNotFoundError",
    "discover_streams",
    "PYLSL_AVAILABLE",
    # 多通道 %MVC 与 Part2→Part3 传输
    "MultiChannelMvc",
    "MuscleChannel",
    "parse_channel_spec",
    "parse_mvc_spec",
    "resolve_channel_index",
    "MvcTcpSender",
    "MvcCsvRecorder",
    "format_line",
    # 算法
    "compute_apdf",
    "ApdfResult",
    "ApdfAccumulator",
    "exceedance_probability",
    "levels_at_probabilities",
    "level_percentiles",
    "scale_invariance_note",
    "MvcCalibrator",
    "CalibrationOutcome",
    "CalibrationState",
    "CalibrationPhase",
    "TrialRecord",
    # DSP
    "design_filter_chain",
    "FilterDesign",
    "StreamingFilter",
    "RmsExtractor",
    "filter_offline",
    "GrowableF64",
    "rms",
    # 会话与导出
    "AcquisitionSession",
    "SessionSnapshot",
    "SessionStats",
    "SessionWriter",
    "SessionPaths",
    "ProcessedOutlet",
    "export_apdf_result",
    # 单位
    "PRESETS",
    "UnitPreset",
    "get_preset",
    "preset_names",
]
