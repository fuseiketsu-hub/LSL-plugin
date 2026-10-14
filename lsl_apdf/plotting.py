"""APDF 绘图（离线出图 / 报告用）。

界面里的实时曲线由 ``gui.py`` 自己维护；本模块面向"事后出图"：
把 :class:`~lsl_apdf.apdf.ApdfResult` 画成符合工效学惯例的 APDF 图并保存 PNG。

坐标轴约定（Jonsson 原图摆法）：X = 幅值水平（%MVE），Y = 超概率 P（%）。
静态水平在 Y=90% 处（低幅值），峰值水平在 Y=10% 处（高幅值）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .apdf import ApdfResult

__all__ = ["plot_apdf", "save_apdf_plot", "configure_cjk_font"]

_MARKER_STYLE = {
    "static": ("#1f77b4", "static (P10)"),
    "median": ("#ff7f0e", "median (P50)"),
    "peak": ("#d62728", "peak (P90)"),
}


def configure_cjk_font() -> None:
    """让 matplotlib 能正常显示中文（缺失时自动退回英文标签，不影响出图）。"""
    from matplotlib import rcParams

    rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Source Han Sans SC",
        "Noto Sans CJK SC",
        "DejaVu Sans",
    ]
    rcParams["axes.unicode_minus"] = False


def plot_apdf(
    result: ApdfResult,
    ax=None,
    title: str | None = None,
    show_markers: bool = True,
    unit_label: str = "%MVE",
):
    """把 APDF 结果画到 ``ax``（缺省新建一个）。返回 ``ax``。"""
    if ax is None:
        import matplotlib.pyplot as plt

        configure_cjk_font()
        _, ax = plt.subplots(figsize=(6.2, 4.6), constrained_layout=True)

    if result.n_windows == 0:
        ax.set_title((title or "APDF") + ": no data")
        return ax

    ax.plot(
        result.level_at_probability_pct,
        result.probability_pct,
        lw=1.8,
        color="#2ca02c",
        label="APDF",
    )

    if show_markers:
        for px, py, name in result.marker_points():
            if not np.isfinite(py):
                continue
            color, label = _MARKER_STYLE.get(name, ("#333333", name))
            ax.plot([py], [px], "o", ms=7, color=color)
            ax.annotate(
                f"{label}\n{py:.1f} {unit_label}",
                xy=(py, px),
                xytext=(6, 8),
                textcoords="offset points",
                fontsize=8,
                color=color,
            )

    ax.grid(alpha=0.3)
    ax.set_xlim(0, max(10.0, result.max_level_pct * 1.12))
    ax.set_ylim(0, 100)
    ax.set_xlabel(f"Amplitude level [{unit_label}]")
    ax.set_ylabel("Exceedance probability P (% of time above level)")
    ax.set_title(
        title
        or (
            f"APDF ({result.total_sec:.0f} s / {result.n_windows} windows, "
            f"RMS window {result.window_sec:g} s, MVE={result.mve_ref:.4g})"
        )
    )
    return ax


def save_apdf_plot(result: ApdfResult, path: str | Path, **kwargs) -> Path:
    """把 APDF 图画成 PNG（或按扩展名决定格式）。"""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    configure_cjk_font()
    fig, ax = plt.subplots(figsize=(6.6, 4.8), constrained_layout=True)
    plot_apdf(result, ax=ax, **kwargs)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path
