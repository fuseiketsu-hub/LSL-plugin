"""配置数据类。

所有配置均可 JSON 序列化，便于 GUI 保存 / 命令行加载 / 实验结果复现。
设计原则：

* **任何一个影响 APDF 数值的参数都必须出现在这里**，并被写进导出目录的
  ``config.json``，否则事后无法复现结果。
* 默认值面向 PLUX / BITalino 的 EMG 场景（见 ``OpenSignals_LSL使用说明.md``
  §6：LSL 发送的是原始数据，需要自行去偏移 → EMG 转换）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "StreamConfig",
    "SignalConfig",
    "CalibrationConfig",
    "ApdfConfig",
    "ExportConfig",
    "AppConfig",
]


# --------------------------------------------------------------------------- #
# 1. LSL 流定位（对应说明文档 §3.2 的三种接收方式）
# --------------------------------------------------------------------------- #
@dataclass
class StreamConfig:
    """LSL 流定位与连接参数。

    三种解析方式的优先级（与说明文档 §3.2 表格一致）：

    1. ``hostname`` 非空 → ``resolve_stream("hostname", ...)``
    2. ``type`` 非空     → ``resolve_stream("type", ...)``（OpenSignals 把设备
       MAC 地址放在 type 字段里）
    3. 否则              → ``resolve_stream("name", ...)``（默认 ``OpenSignals``）
    """

    name: str = "OpenSignals"
    type: str = ""
    hostname: str = ""
    resolve_timeout: float = 10.0
    max_buflen: int = 3600
    """inlet 内部缓冲上限（秒，**必须是整数**：pylsl 会把它原样传给 C 的 int 参数）。
    长时间无人读取时防止内存无限增长。"""
    recover: bool = True
    """流中断后是否尝试自动恢复（``StreamInlet(recover=True)``）。"""
    use_lsl_clock: bool = True
    """True 时导出的时间轴用 LSL 时钟（跨程序天然对齐，见文档 §4）；
    False 时用本机墙钟。"""

    def resolve_property(self) -> tuple[str, str]:
        """返回 (属性名, 属性值)，供 pylsl 的 ``resolve_byprop`` 使用。"""
        if self.hostname.strip():
            return "hostname", self.hostname.strip()
        if self.type.strip():
            return "type", self.type.strip()
        return "name", self.name.strip() or "OpenSignals"

    def describe(self) -> str:
        prop, value = self.resolve_property()
        return f"{prop}={value!r}"


# --------------------------------------------------------------------------- #
# 2. 信号调理 + RMS 提取
# --------------------------------------------------------------------------- #
@dataclass
class SignalConfig:
    """从 LSL 原始样本到 RMS 幅值序列的处理参数。"""

    channel: int = 1
    """使用第几个通道（**1 起编号**，与说明文档 §3.2 的 metadata 示例一致）。"""

    # --- ADC → 物理单位（仅用于显示与报告；%MVE 归一化与单位无关） --- #
    adc_scale: float = 1.0
    """物理值 = (原始值 - adc_offset) * adc_scale。默认 1.0 表示"保持原单位"。"""
    adc_offset: float = 0.0
    unit_label: str = "a.u."
    """显示用单位；连接后若 LSL metadata 里有 unit 会被自动覆盖。"""

    # --- 频带（EMG 惯例 20–450 Hz） --- #
    highpass_hz: float = 20.0
    lowpass_hz: float = 450.0
    notch_hz: float | None = 50.0
    notch_q: float = 30.0
    filter_order: int = 4
    """Butterworth 阶数（兼顾实时性与滚降）。"""

    # --- RMS 窗口 --- #
    rms_window_sec: float = 0.125
    """RMS 窗长。工效学 APDF 惯例为 0.125 s（也可用 1 s，见 README）。"""
    rms_hop_sec: float = 0.0625
    """窗间步长；小于窗长即为重叠窗，曲线更平滑。"""

    startup_skip_sec: float = 0.5
    """丢弃开机后前 N 秒的 RMS —— 滤波器与电极稳定暂态不计入 APDF。"""

    def validate(self) -> list[str]:
        """返回配置问题列表（空列表表示通过）。"""
        problems: list[str] = []
        if self.channel < 1:
            problems.append("channel must be >= 1 (1-based)")
        if self.adc_scale == 0:
            problems.append("adc_scale must not be 0")
        if self.rms_window_sec <= 0:
            problems.append("rms_window_sec must be > 0")
        if self.rms_hop_sec <= 0:
            problems.append("rms_hop_sec must be > 0")
        if self.rms_hop_sec > self.rms_window_sec:
            problems.append("rms_hop_sec must not exceed rms_window_sec (otherwise data is skipped)")
        if self.highpass_hz < 0:
            problems.append("highpass_hz must not be negative")
        return problems


# --------------------------------------------------------------------------- #
# 3. %MVE 强制标定（静息基线 + 多试次 MVC）
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationConfig:
    """MVC/MVE 标定流程参数。

    流程：``静息(REST)`` → 若干次 ``MVC 试次``（试次之间有休息）→ 自动取
    最强试次的稳定平台均值作为 **MVE 参考值**，后续所有幅值除以它得到 %MVE。
    """

    rest_sec: float = 5.0
    """静息段时长：用于估计基线噪声（并对 SNR 做健全性检查）。"""
    mvc_sec: float = 5.0
    """单次最大随意收缩（MVC）的保持时长。"""
    mvc_trials: int = 3
    """MVC 试次数量。"""
    between_trials_sec: float = 20.0
    """两次 MVC 之间的休息时长（避免叠加疲劳污染参考值）。"""
    plateau_sec: float = 2.0
    """在每个试次内取最长的、长度为 plateau_sec 的连续平台均值作为该试次结果。"""
    average_best_trials: bool = False
    """True 时对"达到最强试次 90% 以上"的试次求均值；False 时直接取最大值。"""
    best_trial_ratio: float = 0.90
    snr_warn_db: float = 8.0
    """MVE 与静息基线之比低于该值（dB）时给出警告。"""
    cv_warn: float = 0.15
    """各试次之间的变异系数超过该值时给出警告（提示用力不稳定）。"""
    baseline_statistic: str = "median"
    """静息基线统计量：``median``（稳健，默认）或 ``mean``。"""

    def total_duration(self) -> float:
        """标定全过程预计耗时（秒），用于 GUI 进度条。"""
        return (
            self.rest_sec
            + self.mvc_trials * self.mvc_sec
            + max(0, self.mvc_trials - 1) * self.between_trials_sec
        )


# --------------------------------------------------------------------------- #
# 4. APDF 曲线参数
# --------------------------------------------------------------------------- #
@dataclass
class ApdfConfig:
    """APDF（幅值概率分布函数，Amplitude Probability Distribution Function）。

    曲线读法（Jonsson 工效学惯例）
    ------------------------------
    APDF 给出"幅值超过某一水平的时间占比 P"（超概率）。绘图时

    * X 轴 = 幅值水平（%MVE）；
    * Y 轴 = 超概率 P（%），即幅值**超过**该水平的时间百分比。

    由曲线读取三个代表性负荷水平。**注意方向**：静态负荷水平就是
    "几乎一直存在"的那个低水平（超概率高），峰值负荷水平是偶尔出现的
    高水平（超概率低）：

    ============  ==================  ==================  ==========
    负荷水平      幅值分布百分位       超概率 P            曲线上的位置
    ============  ==================  ==================  ==========
    static 静态    第 10 百分位        90%                 Y=90% 处
    median 中位    第 50 百分位        50%                 Y=50% 处
    peak 峰值      第 90 百分位        10%                 Y=10% 处
    ============  ==================  ==================  ==========

    依据："Reporting the APDF of the EMG signals provides low-level (10th
    percentile), median (50th percentile), and peak (90th percentile)"。
    本插件以**幅值分布百分位**为主口径（无歧义），同时输出对应的超概率。
    """

    level_step_pct: float = 0.5
    """Y 轴（%MVE）采样步长，越小曲线越平滑、导出文件越大。"""
    max_level_pct: float = 100.0
    """Y 轴名义上限；默认 100 %MVE。实际网格会自动扩展到观测到的最大值。"""
    auto_expand: bool = True
    """观测幅值超过 max_level_pct 时自动扩展网格（MVC 参考值偏低时会用到）。"""
    probability_step_pct: float = 0.5
    """概率轴插值步长，用于导出规整的 P–幅值 表。"""

    static_percentile: float = 10.0
    """静态（低）负荷水平 = 幅值分布的第 10 百分位。"""
    median_percentile: float = 50.0
    """中位负荷水平 = 幅值分布的第 50 百分位。"""
    peak_percentile: float = 90.0
    """峰值负荷水平 = 幅值分布的第 90 百分位。"""

    def percentile_levels(self) -> dict[str, float]:
        return {
            "static": self.static_percentile,
            "median": self.median_percentile,
            "peak": self.peak_percentile,
        }

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.level_step_pct <= 0:
            problems.append("level_step_pct must be > 0")
        if self.max_level_pct <= 0:
            problems.append("max_level_pct must be > 0")
        if not (
            0.0
            < self.static_percentile
            < self.median_percentile
            < self.peak_percentile
            < 100.0
        ):
            problems.append("require 0 < static_percentile < median_percentile < peak_percentile < 100")
        return problems


# --------------------------------------------------------------------------- #
# 5. 实时导出
# --------------------------------------------------------------------------- #
@dataclass
class ExportConfig:
    """实时导出（落盘）参数。"""

    enabled: bool = True
    root_dir: str = "exports"
    """会话子目录的父目录。"""
    write_raw: bool = True
    """是否把 LSL 原始样本实时写入 raw.csv（"实时导出信号"的主体）。"""
    flush_interval_sec: float = 1.0
    """写盘线程的强制 flush 周期，掉电最多丢这么多秒的数据。"""
    apdf_snapshot_interval_sec: float = 5.0
    """APDF 快照（曲线 + 分位点）的快照周期。"""
    publish_processed: bool = False
    """是否把处理后的 RMS / %MVE 追加发布为新的 LSL 流（供其它程序订阅）。"""
    publish_name: str = "OpenSignals_APDF"
    session_prefix: str = "session"


# --------------------------------------------------------------------------- #
# 6. 总配置
# --------------------------------------------------------------------------- #
@dataclass
class AppConfig:
    """顶层配置：把上面几组参数聚合在一起，便于整体保存 / 加载。"""

    stream: StreamConfig = field(default_factory=StreamConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    apdf: ApdfConfig = field(default_factory=ApdfConfig)
    export: ExportConfig = field(default_factory=ExportConfig)

    mve_ref: float | None = None
    """已知 MVE 参考值时可直接填入（跳过标定）；单位与信号幅值一致。"""

    include_calibration_in_apdf: bool = False
    """是否把标定段（静息/MVC 试次）的 RMS 也计入 APDF。

    默认 False：标定期的用力不属于"工作任务"，混进去会显著抬高静态/峰值
    负荷水平。只有做方法学对比时才建议打开。
    """

    display_window_sec: float = 5.0
    """GUI 波形显示窗长。"""

    subject_id: str = ""
    condition: str = ""
    notes: str = ""

    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        problems: list[str] = []
        problems += [f"signal: {p}" for p in self.signal.validate()]
        problems += [f"apdf: {p}" for p in self.apdf.validate()]
        if self.mve_ref is not None and self.mve_ref <= 0:
            problems.append("mve_ref must be > 0")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """从嵌套 dict 构造；未知键会被忽略（向前兼容旧配置文件）。"""
        sections = {
            "stream": StreamConfig,
            "signal": SignalConfig,
            "calibration": CalibrationConfig,
            "apdf": ApdfConfig,
            "export": ExportConfig,
        }
        kwargs: dict[str, Any] = {}
        for key, klass in sections.items():
            sub = data.get(key) or {}
            kwargs[key] = _build_dataclass(klass, sub)
        for key in (
            "mve_ref",
            "include_calibration_in_apdf",
            "display_window_sec",
            "subject_id",
            "condition",
            "notes",
        ):
            if key in data:
                kwargs[key] = data[key]
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """按 ``{"signal.rms_window_sec": 0.25}`` 形式的点分路径就地覆写。"""
        for dotted, value in overrides.items():
            if value is None:
                continue
            target: Any = self
            parts = dotted.split(".")
            for part in parts[:-1]:
                if not hasattr(target, part):
                    raise KeyError(f"Unknown config key: {dotted}")
                target = getattr(target, part)
            if not hasattr(target, parts[-1]):
                raise KeyError(f"Unknown config key: {dotted}")
            setattr(target, parts[-1], value)


def _build_dataclass(klass: type, data: dict[str, Any]) -> Any:
    """只挑出 dataclass 声明过的字段，忽略多余键。"""
    allowed = {f.name for f in fields(klass) if f.init}
    return klass(**{k: v for k, v in data.items() if k in allowed})
