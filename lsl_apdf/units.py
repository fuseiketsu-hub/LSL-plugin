"""ADC 原始值 → 物理单位的换算预设。

**重要提醒**：APDF 以 %MVE 表达，而 %MVE 是两个同单位量之比，
因此 **换算系数对 APDF 结果没有影响**（见 ``apdf.scale_invariance_note``）。
本模块的作用只是让日志、CSV 和报告里的绝对幅值好看、可解释。

数值来源（常见 PLUX / BITalino 配置，可在 GUI 中覆盖）：

* **BITalino (r)evolution**：10 位 ADC，参考电压 3.3 V，EMG 传感器增益 1000
  → 每个 ADC 码 ≈ 3.3 V / 1024 / 1000 ≈ 3.22 µV。
* **biosignalsplux**：16 位 ADC，参考电压 3.0 V，EMG 传感器增益 1000
  → 每个 ADC 码 ≈ 3.0 V / 65536 / 1000 ≈ 45.8 nV。

注意：这两个预设只是把 ADC 码线性映射到 mV；传感器自身还有
``VCC/2`` 的直流偏置，该偏置由高通滤波器去除，无需在换算里处理。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["UnitPreset", "PRESETS", "preset_names", "get_preset"]


@dataclass(frozen=True)
class UnitPreset:
    """一条换算法则：``physical = (raw - offset) * scale``。"""

    key: str
    label: str
    scale: float
    offset: float
    unit: str
    note: str = ""


PRESETS: dict[str, UnitPreset] = {
    "raw": UnitPreset(
        key="raw",
        label="Raw ADC codes / unknown unit",
        scale=1.0,
        offset=0.0,
        unit="a.u.",
        note="No conversion; amplitudes are reported in ADC codes. %MVE results are unaffected.",
    ),
    "bitalino_emg": UnitPreset(
        key="bitalino_emg",
        label="BITalino EMG (10bit, 3.3V, G=1000) → mV",
        scale=3.3 / 1024.0 / 1000.0 * 1000.0,
        offset=0.0,
        unit="mV",
        note="One ADC code ≈ 3.22 µV.",
    ),
    "biosignalsplux_emg": UnitPreset(
        key="biosignalsplux_emg",
        label="biosignalsplux EMG (16bit, 3.0V, G=1000) → mV",
        scale=3.0 / 65536.0 / 1000.0 * 1000.0,
        offset=0.0,
        unit="mV",
        note="One ADC code ≈ 45.8 nV.",
    ),
}


def preset_names() -> list[str]:
    return list(PRESETS)


def get_preset(key: str) -> UnitPreset:
    try:
        return PRESETS[key]
    except KeyError as exc:  # pragma: no cover - 防御性
        raise KeyError(f"Unknown unit preset {key!r}; available: {preset_names()}") from exc


def convert_adc_to_physical(
    raw, scale: float = 1.0, offset: float = 0.0
):
    """线性换算；同时接受标量与 numpy 数组。"""
    return (raw - offset) * scale
