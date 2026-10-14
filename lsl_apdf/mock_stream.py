"""模拟一个 OpenSignals LSL 发布端，用于**无硬件**验证整条链路。

为什么需要它
------------
真实 OpenSignals 只在"点击录制按钮后"才发布流（说明文档 §2），而且必须有
设备在手。为了在没有 bitalino/biosignalsplux 的情况下也能：

* 验证 ``resolve_stream`` / inlet 连接是否正常；
* 演示完整的 %MVE 标定流程；
* 让 CI / 单元测试能跑真实 LSL 往返（而不是只测合成数据源）；

本模块按 OpenSignals 的**真实命名约定**发布流：

====================  ==================================================
``name``              ``OpenSignals``
``type``              **设备 MAC 地址**（因此 ``resolve_stream("type", mac)`` 可用）
``source_id``         设备 MAC + 通道描述
每通道 ``desc``        ``channels/channel/{label,unit}``
====================  ==================================================

用法::

    python -m lsl_apdf mock                    # 工作负荷轮廓
    python -m lsl_apdf mock --profile calibration   # 静息+MVC，用于演示标定
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np

from .sources import SyntheticProfile, SyntheticSource, require_pylsl

__all__ = ["MockOpenSignals", "publish_mock_stream", "WORK_PROFILE", "CALIBRATION_PROFILE"]

#: 默认工作负荷轮廓：轻负荷 → 中等 → 高 → 回落（近似办公/装配作业）
WORK_PROFILE = SyntheticProfile(
    segments=[(20.0, 6.0), (20.0, 25.0), (10.0, 55.0), (20.0, 6.0)],
    noise_floor_pct=1.5,
    mve_unit=1000.0,
    dc_offset=1650.0,
    line_noise_pct=3.0,
)

#: 标定演示轮廓：静息 → 3×MVC（每次 5 s，间隔 10 s）
CALIBRATION_PROFILE = SyntheticProfile(
    segments=[
        (5.0, 0.0),
        (5.0, 100.0), (10.0, 0.0),
        (5.0, 96.0), (10.0, 0.0),
        (5.0, 103.0), (10.0, 0.0),
        (20.0, 10.0), (15.0, 40.0), (20.0, 8.0),
    ],
    noise_floor_pct=1.0,
    mve_unit=1000.0,
    dc_offset=1650.0,
)


@dataclass
class MockOpenSignals:
    """一个假的 OpenSignals 发布端。"""

    mac: str = "00:07:80:AA:BB:CC"
    srate: float = 1000.0
    n_channels: int = 1
    channel_label: str = "EMG"
    channel_unit: str = "mV"
    profile: SyntheticProfile = None  # type: ignore[assignment]
    realtime: bool = True

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = WORK_PROFILE

    # ------------------------------------------------------------------ #
    def outlet(self):
        """建立并返回 LSL outlet（类型字段放 MAC，与 OpenSignals 一致）。"""
        require_pylsl()
        import pylsl

        info = pylsl.StreamInfo(
            name="OpenSignals",
            type=self.mac,
            channel_count=self.n_channels,
            nominal_srate=self.srate,
            channel_format="float32",
            source_id=f"{self.mac}_mock",
        )
        desc = info.desc()
        desc.append_child_value("manufacturer", "PLUX (mock)")
        desc.append_child_value("device", "biosignalsplux (mock)")
        channels = desc.append_child("channels")
        for _ in range(self.n_channels):
            ch = channels.append_child("channel")
            ch.append_child_value("label", self.channel_label)
            ch.append_child_value("unit", self.channel_unit)
            ch.append_child_value("type", "EMG")
        return pylsl.StreamOutlet(info)

    # ------------------------------------------------------------------ #
    def run(
        self,
        duration: float | None = None,
        on_tick: Callable[[float], None] | None = None,
        tick_interval: float = 5.0,
    ) -> int:
        """发布数据，直到 ``duration`` 秒用尽或被 Ctrl+C 中断。返回发布的样本数。"""
        outlet = self.outlet()
        source = SyntheticSource(
            profile=self.profile,
            srate=self.srate,
            n_channels=self.n_channels,
            realtime=self.realtime,
            chunk_sec=0.05,
            channel_label=self.channel_label,
            channel_unit=self.channel_unit,
        )
        source.connect()
        start = time.time()
        next_tick = 0.0
        pushed = 0
        try:
            while True:
                chunk = source.read_chunk(timeout=0.05)
                if chunk is None:  # pragma: no cover
                    continue
                outlet.push_chunk(chunk.data.tolist())
                pushed += len(chunk)
                now = time.time()
                if on_tick is not None and now >= next_tick:
                    on_tick(now - start)
                    next_tick = now + tick_interval
                if duration is not None and (now - start) >= duration:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            source.close()
        return pushed


def publish_mock_stream(
    duration: float | None = None,
    profile: str | SyntheticProfile = "work",
    srate: float = 1000.0,
    n_channels: int = 1,
    mac: str = "00:07:80:AA:BB:CC",
    verbose: bool = True,
) -> int:
    """便捷函数：按预设轮廓发布假 OpenSignals 流。"""
    if isinstance(profile, str):
        chosen = {
            "work": WORK_PROFILE,
            "calibration": CALIBRATION_PROFILE,
            "calib": CALIBRATION_PROFILE,
        }.get(profile.lower())
        if chosen is None:
            raise ValueError(f"Unknown profile {profile!r}; options: work / calibration")
    else:
        chosen = profile

    mock = MockOpenSignals(
        mac=mac, srate=srate, n_channels=n_channels, profile=chosen, realtime=True
    )

    def report(elapsed: float) -> None:
        if verbose:
            print(f"  ... published {elapsed:.0f} s (profile {profile if isinstance(profile, str) else 'custom'})", flush=True)

    if verbose:
        print("Publishing simulated OpenSignals LSL stream:")
        print(f"  name = OpenSignals")
        print(f"  type = {mac}   <- device MAC in the type field (same as real OpenSignals)")
        print(f"  channels = {n_channels} x {mock.channel_label} [{mock.channel_unit}], srate = {srate:g} Hz")
        print("  In another terminal run: python -m lsl_apdf gui  or  python -m lsl_apdf run")
        print("  Press Ctrl+C to stop.\n")
    return mock.run(duration=duration, on_tick=report)
