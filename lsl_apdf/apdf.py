"""APDF（幅值概率分布函数）核心算法。

什么是 APDF
-----------
APDF 描述"幅值超过某一水平的时间占整个记录的多大比例"，是 EMG 工效学负荷
评价的标准工具（Jonsson 1982）。曲线读法：

* X 轴：超概率 P（%）—— 幅值**超过**该水平的时间占比；
* Y 轴：幅值水平（%MVE，相对最大随意收缩参考值）。

三个代表性负荷水平按**幅值分布百分位**定义（本插件的主口径）：

============  ==================  ==================  ==============
负荷水平      幅值分布百分位       超概率 P            曲线上的位置
============  ==================  ==================  ==============
static 静态    第 10 百分位        90%                 X=90% 处（低幅值）
median 中位    第 50 百分位        50%                 X=50% 处
peak 峰值      第 90 百分位        10%                 X=10% 处（高幅值）
============  ==================  ==================  ==============

方向容易记错，务必注意：**静态负荷是"几乎一直存在"的低水平**（被超过的时间
占比高），**峰值负荷是偶尔出现的高水平**（被超过的时间占比低）。

数学等价
--------
设幅值样本为 :math:`a_1..a_N`，则 :math:`P(L) = \\#\\{a_i > L\\}/N`，
即 1 减去经验 CDF。于是以"规则概率网格"绘制的曲线正好是幅值分布的
**逆 CDF（分位数函数）**：:math:`L(P) = Q_a(1-P)`。两种网格都在结果里给出。

尺度不变性
----------
%MVE 是两个同单位量之比，所以 ADC 码 → mV 之类的线性换算、乃至把整段信号
乘一个常数，都**不改变 APDF 曲线**（只要 MVE 参考值取自同一路信号）。
见 :func:`scale_invariance_note`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .config import ApdfConfig
from .dsp import GrowableF64

__all__ = [
    "ApdfResult",
    "ApdfAccumulator",
    "compute_apdf",
    "exceedance_probability",
    "level_percentiles",
    "levels_at_probabilities",
    "scale_invariance_note",
    "STATIC_LABEL",
    "MEDIAN_LABEL",
    "PEAK_LABEL",
]

STATIC_LABEL = "static (P10)"
MEDIAN_LABEL = "median (P50)"
PEAK_LABEL = "peak (P90)"

#: 幅值轴网格的硬上限（%MVE）。防止 MVE 参考值异常小导致数组爆炸。
MAX_LEVEL_AXIS_PCT = 1000.0
#: 幅值轴网格的最大点数。
MAX_LEVEL_BINS = 20000


def scale_invariance_note() -> str:
    return (
        "APDF is expressed in %MVE, a ratio of two quantities in the same unit, "
        "so linear unit conversions (ADC to mV) and constant scaling of the whole "
        "signal do not change the curve shape."
    )


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class ApdfResult:
    """一次 APDF 计算的完整结果（可直接绘图 / 导出 / 供插件消费）。"""

    mve_ref: float
    n_windows: int
    method: str = "exceedance"

    static_percentile: float = 10.0
    """静态（低）负荷水平所用的幅值分布百分位 —— 见模块开头的方向说明。"""
    median_percentile: float = 50.0
    peak_percentile: float = 90.0
    """峰值负荷水平所用的幅值分布百分位。"""

    static_level_pct: float = float("nan")
    """静态负荷水平（%MVE）："几乎一直存在"的低水平。"""
    median_level_pct: float = float("nan")
    """中位负荷水平（%MVE）。"""
    peak_level_pct: float = float("nan")
    """峰值负荷水平（%MVE）：偶尔出现的高水平。"""

    levels_pct: np.ndarray = field(default_factory=lambda: np.empty(0))
    """规则幅值网格（%MVE）。"""
    exceedance: np.ndarray = field(default_factory=lambda: np.empty(0))
    """各幅值水平对应的超概率（0..1）。"""

    probability_pct: np.ndarray = field(default_factory=lambda: np.empty(0))
    """规则概率网格（%），用于绘图与导出。"""
    level_at_probability_pct: np.ndarray = field(default_factory=lambda: np.empty(0))
    """各概率水平对应的幅值（%MVE），即 APDF 曲线。"""

    amplitude_percentiles: dict[str, float] = field(default_factory=dict)
    """幅值分布百分位：{'P10': .., 'P50': .., 'P90': ..}（%MVE）。"""

    mean_level_pct: float = 0.0
    std_level_pct: float = 0.0
    min_level_pct: float = 0.0
    max_level_pct: float = 0.0
    window_sec: float = 0.0
    """单个 RMS 窗口时长。"""
    hop_sec: float = 0.0
    """RMS 窗口步长（重叠窗时小于 ``window_sec``）。"""

    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def total_sec(self) -> float:
        """APDF 实际覆盖的时间跨度。

        窗口起点从 0 排到 ``(n-1)*hop``，最后一个窗口还延伸 ``window_sec``，
        故跨度为 ``(n-1)*hop + window_sec``。重叠窗下这个值**小于**
        ``n * window_sec``，按时间跨度报告才不会被高估。
        """
        if self.n_windows <= 0:
            return 0.0
        hop = self.hop_sec if self.hop_sec > 0 else self.window_sec
        return (self.n_windows - 1) * hop + self.window_sec

    @property
    def static_level(self) -> float:
        """静态负荷水平（%MVE）—— :attr:`static_level_pct` 的别名。"""
        return self.static_level_pct

    @property
    def median_level(self) -> float:
        """中位负荷水平（%MVE）。"""
        return self.median_level_pct

    @property
    def peak_level(self) -> float:
        """峰值负荷水平（%MVE）—— 注意这是**高**水平（幅值分布 P90）。"""
        return self.peak_level_pct

    @property
    def static_exceedance(self) -> float:
        """静态负荷水平对应的超概率（幅值 P10 → 超概率 90%）。"""
        return 1.0 - self.static_percentile / 100.0

    @property
    def median_exceedance(self) -> float:
        """中位负荷水平对应的超概率（50%）。"""
        return 1.0 - self.median_percentile / 100.0

    @property
    def peak_exceedance(self) -> float:
        """峰值负荷水平对应的超概率（幅值 P90 → 超概率 10%）。"""
        return 1.0 - self.peak_percentile / 100.0

    # ------------------------------------------------------------------ #
    def level_at_exceedance(self, probability: float) -> float:
        """读取曲线：给定超概率 P（0..1），返回对应幅值（%MVE）。

        曲线随 P 单调递减，故对已算好的规则概率网格做线性插值。
        **P 越大读到的是越低**的负荷水平（P=90% → 静态水平）。
        """
        p = float(np.clip(probability, 0.0, 1.0))
        if self.level_at_probability_pct.size == 0:
            return float("nan")
        return float(
            np.interp(p * 100.0, self.probability_pct, self.level_at_probability_pct)
        )

    def level_at_percentile(self, percentile: float) -> float:
        """按幅值分布百分位读取负荷水平（与 :meth:`level_at_exceedance` 互补）。"""
        return self.level_at_exceedance(1.0 - float(percentile) / 100.0)

    def level_percentiles(self) -> dict[str, float]:
        """三个代表性负荷水平（%MVE）。"""
        return {
            STATIC_LABEL: self.static_level,
            MEDIAN_LABEL: self.median_level,
            PEAK_LABEL: self.peak_level,
        }

    def marker_points(self) -> list[tuple[float, float, str]]:
        """绘图用标记点：``[(超概率%, 幅值%MVE, 标签), ...]``。"""
        return [
            (self.static_exceedance * 100.0, self.static_level, "static"),
            (self.median_exceedance * 100.0, self.median_level, "median"),
            (self.peak_exceedance * 100.0, self.peak_level, "peak"),
        ]

    def summary(self) -> dict[str, Any]:
        """扁平化摘要，便于写入 JSON / 日志 / 供疲劳检测插件消费。"""
        out: dict[str, Any] = {
            "n_windows": self.n_windows,
            "window_sec": round(self.window_sec, 6),
            "hop_sec": round(self.hop_sec, 6),
            "total_sec": round(self.total_sec, 3),
            "mve_ref": self.mve_ref,
            "mean_level_pct": self.mean_level_pct,
            "std_level_pct": self.std_level_pct,
            "min_level_pct": self.min_level_pct,
            "max_level_pct": self.max_level_pct,
            "static_level_pct": self.static_level,
            "median_level_pct": self.median_level,
            "peak_level_pct": self.peak_level,
            "static_amplitude_percentile": self.static_percentile,
            "median_amplitude_percentile": self.median_percentile,
            "peak_amplitude_percentile": self.peak_percentile,
            "static_exceedance_pct": self.static_exceedance * 100.0,
            "median_exceedance_pct": self.median_exceedance * 100.0,
            "peak_exceedance_pct": self.peak_exceedance * 100.0,
        }
        for key, value in self.amplitude_percentiles.items():
            out[f"amplitude_{key.lower()}_pct"] = value
        return out

    def level_grid_rows(self) -> Iterable[tuple[float, float, float]]:
        """规则幅值网格的导出行：``(幅值%MVE, 超概率0..1, 超概率%)``。"""
        for level, prob in zip(self.levels_pct, self.exceedance):
            yield float(level), float(prob), float(prob * 100.0)

    def probability_grid_rows(self) -> Iterable[tuple[float, float]]:
        """规则概率网格的导出行：``(概率%, 幅值%MVE)``。"""
        for prob, level in zip(self.probability_pct, self.level_at_probability_pct):
            yield float(prob), float(level)


# --------------------------------------------------------------------------- #
# 基础计算
# --------------------------------------------------------------------------- #
def exceedance_probability(
    levels_pct: np.ndarray, grid: np.ndarray
) -> np.ndarray:
    """``P(L) = Pr[幅值 > L]``，在给定幅值网格上取值（O(n log n)）。"""
    levels_pct = np.asarray(levels_pct, dtype=float).reshape(-1)
    grid = np.asarray(grid, dtype=float).reshape(-1)
    n = levels_pct.size
    if n == 0:
        return np.zeros_like(grid)
    ordered = np.sort(levels_pct)
    # searchsorted(side="right") = 小于等于该水平的样本数
    n_le = np.searchsorted(ordered, grid, side="right")
    return (n - n_le) / float(n)


def levels_at_probabilities(
    levels_pct: np.ndarray, probabilities: np.ndarray
) -> np.ndarray:
    """给定超概率 P，返回对应幅值 —— 即幅值分布的逆 CDF。"""
    levels_pct = np.asarray(levels_pct, dtype=float).reshape(-1)
    probabilities = np.asarray(probabilities, dtype=float).reshape(-1)
    if levels_pct.size == 0:
        return np.full(probabilities.shape, np.nan)
    q = (1.0 - np.clip(probabilities, 0.0, 1.0)) * 100.0
    return np.quantile(levels_pct, q / 100.0, method="linear")


def level_percentiles(levels_pct: np.ndarray) -> dict[str, float]:
    """幅值分布的 P10 / P50 / P90（%MVE）。"""
    levels_pct = np.asarray(levels_pct, dtype=float).reshape(-1)
    if levels_pct.size == 0:
        return {"P10": float("nan"), "P50": float("nan"), "P90": float("nan")}
    p10, p50, p90 = np.percentile(levels_pct, [10.0, 50.0, 90.0])
    return {"P10": float(p10), "P50": float(p50), "P90": float(p90)}


# --------------------------------------------------------------------------- #
# 主计算
# --------------------------------------------------------------------------- #
def compute_apdf(
    amplitudes: np.ndarray,
    mve_ref: float,
    cfg: ApdfConfig | None = None,
    window_sec: float = 0.0,
    hop_sec: float = 0.0,
    method: str = "exceedance",
) -> ApdfResult:
    """把 RMS 幅值序列换算成 APDF。

    Parameters
    ----------
    amplitudes:
        各 RMS 窗口的幅值（信号原始单位，与 ``mve_ref`` 同单位）。
    mve_ref:
        MVC/MVE 参考幅值；**必填且 > 0**（本插件强制 %MVE 归一化）。
    cfg:
        网格与分位点参数。
    window_sec / hop_sec:
        窗口时长与步长，仅用于报告 APDF 覆盖的时间跨度。
    """
    cfg = cfg or ApdfConfig()
    amplitudes = np.asarray(amplitudes, dtype=float).reshape(-1)

    if mve_ref is None:
        raise ValueError(
            "An MVE reference is required for a %MVE-normalized APDF "
            "(run the calibration flow first, or set mve_ref in the config)"
        )
    if not np.isfinite(mve_ref) or mve_ref <= 0:
        raise ValueError(f"mve_ref must be a positive finite value, got {mve_ref!r}")

    finite = np.isfinite(amplitudes)
    clean = amplitudes[finite]
    n_dropped = int(amplitudes.size - clean.size)

    notes: list[str] = []
    if n_dropped:
        notes.append(f"Ignored {n_dropped} non-finite amplitude samples")

    result = ApdfResult(
        mve_ref=float(mve_ref),
        n_windows=int(clean.size),
        method=method,
        static_percentile=cfg.static_percentile,
        median_percentile=cfg.median_percentile,
        peak_percentile=cfg.peak_percentile,
        window_sec=float(window_sec),
        hop_sec=float(hop_sec),
        notes=notes,
    )

    if clean.size == 0:
        result.notes.append("No usable RMS windows; APDF is empty")
        return result

    levels_pct = clean / float(mve_ref) * 100.0
    result.mean_level_pct = float(np.mean(levels_pct))
    result.std_level_pct = float(np.std(levels_pct, ddof=0))
    result.min_level_pct = float(np.min(levels_pct))
    result.max_level_pct = float(np.max(levels_pct))
    result.amplitude_percentiles = level_percentiles(levels_pct)

    # 三个代表性负荷水平：直接取幅值分布的百分位（主口径，无歧义）
    result.static_level_pct = float(np.percentile(levels_pct, cfg.static_percentile))
    result.median_level_pct = float(np.percentile(levels_pct, cfg.median_percentile))
    result.peak_level_pct = float(np.percentile(levels_pct, cfg.peak_percentile))

    # ---------------- 规则幅值网格（Y 轴等间隔） ---------------- #
    top = float(cfg.max_level_pct)
    if cfg.auto_expand and result.max_level_pct > top:
        step = max(cfg.level_step_pct, 1e-9)
        top = np.ceil(result.max_level_pct / step) * step
        notes.append(
            f"Observed amplitude reached {result.max_level_pct:.1f}%MVE, above the "
            f"nominal cap {cfg.max_level_pct:g}%; level grid auto-expanded to {top:g}%"
        )
    hard_cap = min(MAX_LEVEL_AXIS_PCT, MAX_LEVEL_BINS * cfg.level_step_pct)
    if top > hard_cap:
        notes.append(f"Level grid cap clamped to {hard_cap:g}% (MVE reference may be too small)")
        top = hard_cap

    grid = np.arange(0.0, top + cfg.level_step_pct * 0.5, cfg.level_step_pct)
    result.levels_pct = grid
    result.exceedance = exceedance_probability(levels_pct, grid)

    # ---------------- 规则概率网格（X 轴等间隔，绘图用） ---------------- #
    prob_grid = np.arange(
        0.0, 100.0 + cfg.probability_step_pct * 0.5, cfg.probability_step_pct
    )
    result.probability_pct = prob_grid
    result.level_at_probability_pct = levels_at_probabilities(levels_pct, prob_grid / 100.0)

    return result


# --------------------------------------------------------------------------- #
# 累加器
# --------------------------------------------------------------------------- #
class ApdfAccumulator:
    """在线累积 RMS 幅值，按需重算 APDF。

    存的是**原始单位**幅值而非 %MVE：这样标定完成（或事后修正 MVE 参考值）后
    无需重新采集，直接换一个 ``mve_ref`` 重算即可，标定之前的数据也不会浪费。
    """

    def __init__(self, cfg: ApdfConfig | None = None):
        self.cfg = cfg or ApdfConfig()
        self._values = GrowableF64()
        self._cache: ApdfResult | None = None
        self._cache_key: tuple[float, int] | None = None

    # ------------------------------------------------------------------ #
    def add(self, amplitudes: np.ndarray | float) -> None:
        self._values.append(amplitudes)

    def add_one(self, amplitude: float) -> None:
        self._values.append(np.asarray([amplitude], dtype=float))

    @property
    def n_samples(self) -> int:
        return len(self._values)

    def clear(self) -> None:
        self._values.clear()
        self._cache = None
        self._cache_key = None

    def amplitudes(self) -> np.ndarray:
        return self._values.to_array()

    # ------------------------------------------------------------------ #
    def compute(
        self,
        mve_ref: float,
        window_sec: float = 0.0,
        hop_sec: float = 0.0,
        use_cache: bool = True,
    ) -> ApdfResult:
        key = (float(mve_ref), len(self._values), float(window_sec), float(hop_sec))
        if use_cache and self._cache is not None and self._cache_key == key:
            return self._cache
        result = compute_apdf(
            self._values.view(),
            mve_ref=mve_ref,
            cfg=self.cfg,
            window_sec=window_sec,
            hop_sec=hop_sec,
        )
        self._cache = result
        self._cache_key = key
        return result
