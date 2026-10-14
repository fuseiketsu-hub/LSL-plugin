"""真实 LSL 往返测试与 GUI 冒烟测试。

LSL 部分**用子进程**发布一个 OpenSignals 风格的流（``python -m lsl_apdf mock``），
再从测试进程接收。为什么不用同进程 outlet→inlet：实测本机（Windows 多网卡）
liblsl 同进程自环收不到任何数据（解析得到流、但 pull_chunk 永远为空），而
跨进程完全正常 —— 这也正是 OpenSignals 的真实用法（它一定是另一个程序）。
所以这里用子进程，测的就是真实链路。

* 没有 pylsl → 整类跳过。
* 解析不到流（机器的多播/回环被禁用等环境问题）→ 跳过并给出说明。
* 解析到流但收不到数据 → **判定失败**（说明链路代码有问题）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.apdf import compute_apdf
from lsl_apdf.config import AppConfig, StreamConfig
from lsl_apdf.sources import PYLSL_AVAILABLE, LSLSource, SyntheticProfile, SyntheticSource

MAC = "00:07:80:99:88:77"
ROOT = Path(__file__).resolve().parent.parent


def _start_publisher(duration: float = 40.0, profile: str = "work") -> subprocess.Popen:
    """在子进程里启动模拟 OpenSignals 发布端。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "lsl_apdf", "mock",
            "--duration", str(duration),
            "--profile", profile,
            "--mac", MAC,
            "--srate", "500",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def _wait_for_stream(timeout: float = 15.0) -> int:
    """等待流出现；返回发现的流数量。"""
    from lsl_apdf.sources import discover_streams

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            found = discover_streams(wait_time=0.5)
        except Exception:
            found = []
        if any(m.type == MAC for m in found):
            return len(found)
        time.sleep(0.5)
    return 0


@unittest.skipUnless(PYLSL_AVAILABLE, "未安装 pylsl")
class TestLslRoundTrip(unittest.TestCase):
    proc: subprocess.Popen | None = None

    @classmethod
    def setUpClass(cls):
        cls.proc = _start_publisher(duration=60.0)
        cls.discovered = _wait_for_stream(timeout=20.0)
        if not cls.discovered:
            cls.proc.terminate()
            cls.proc = None
            raise unittest.SkipTest(
                "本机未能发现模拟 LSL 流（可能是多播/回环网络被禁用）；"
                "跨进程 LSL 链路已由 `python -m lsl_apdf mock` + `run` 手工验证过。"
            )

    @classmethod
    def tearDownClass(cls):
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()

    def _connect(self) -> LSLSource:
        src = LSLSource(StreamConfig(type=MAC, resolve_timeout=10.0, max_buflen=120))
        src.connect()
        self.addCleanup(src.close)
        return src

    def _read_some(self, src: LSLSource, count: int = 400, timeout: float = 8.0):
        got = []
        deadline = time.time() + timeout
        while sum(len(c) for c in got) < count and time.time() < deadline:
            chunk = src.read_chunk(timeout=0.25)
            if chunk is not None:
                got.append(chunk)
        return got

    def test_connect_by_mac_and_receive(self):
        src = self._connect()
        meta = src.meta
        self.assertEqual(meta.name, "OpenSignals")
        self.assertEqual(meta.type, MAC)
        self.assertEqual(meta.n_channels, 1)
        self.assertAlmostEqual(meta.nominal_srate, 500.0, places=3)
        # 通道 label/unit 只能从 inlet.info() 拿到（resolve 的 desc 是空的）
        self.assertEqual(meta.channel_labels, ["EMG"])
        self.assertEqual(meta.channel_units, ["mV"])
        self.assertIn("EMG", meta.describe())

        chunks = self._read_some(src, count=400)
        received = sum(len(c) for c in chunks)
        self.assertGreaterEqual(
            received, 400, "解析到流但收不到样本 —— 接收链路有问题"
        )
        self.assertGreater(src.n_samples, 0)
        self.assertGreater(src.n_chunks, 0)

    def test_received_signal_matches_synthetic_envelope(self):
        """收到的幅值应与模拟端的包络一致（端到端幅值不失真）。"""
        src = self._connect()
        chunks = self._read_some(src, count=2000, timeout=10.0)
        self.assertTrue(chunks, "没有收到数据")
        x = np.concatenate([c.channel(1) for c in chunks])
        t = np.concatenate([c.timestamps for c in chunks])
        # 采样率实测
        srate = (t.size - 1) / (t[-1] - t[0])
        self.assertAlmostEqual(srate, 500.0, delta=20.0)
        # 去 DC（模拟端有 1650 的偏置）后应在合理量级（work 轮廓 7.5%~56.5% × 1000）
        centered = x - float(np.mean(x))
        measured = float(np.sqrt(np.mean(centered ** 2)))
        self.assertGreater(measured, 30.0)
        self.assertLess(measured, 700.0)

    def test_apdf_from_lsl_is_wellformed(self):
        """通过 LSL 收到的数据要能算出结构正确的 APDF（与真实链路一致）。"""
        from lsl_apdf.dsp import RmsExtractor, design_filter_chain, filter_offline

        src = self._connect()
        chunks = self._read_some(src, count=6000, timeout=15.0)
        self.assertTrue(chunks, "没有收到数据")
        x = np.concatenate([c.channel(1) for c in chunks])
        t = np.concatenate([c.timestamps for c in chunks])
        self.assertGreater(x.size, 4000)

        srate = src.srate
        design = design_filter_chain(srate, 20.0, 450.0, None, 30.0, 4)
        y = filter_offline(x, srate, design, zero_phase=False)
        extractor = RmsExtractor(srate, 0.125, 0.0625)
        rms_v, rms_t = extractor.push(y, t)
        self.assertGreater(rms_v.size, 20)

        # 以 RMS 中位数为 MVE 参考值 → 中位负荷水平必须正好是 100 %MVE
        mve = float(np.median(rms_v))
        result = compute_apdf(rms_v, mve_ref=mve, window_sec=0.125, hop_sec=0.0625)
        self.assertGreater(result.n_windows, 20)
        self.assertAlmostEqual(result.median_level, 100.0, delta=1.0)
        self.assertLess(result.static_level, 100.0)
        self.assertGreater(result.peak_level, 100.0)
        # 方向性：静态(低) < 中位 < 峰值(高)
        self.assertLess(result.static_level, result.median_level)
        self.assertLess(result.median_level, result.peak_level)
        self.assertEqual(
            result.probability_pct.size, result.level_at_probability_pct.size
        )
        # 曲线随超概率单调不增
        self.assertTrue(np.all(np.diff(result.level_at_probability_pct) <= 1e-9))

    def test_stream_not_found_raises_with_hint(self):
        cfg = StreamConfig(type="NO:SU:CH:MA:C0", resolve_timeout=0.5)
        with self.assertRaises(Exception) as ctx:
            LSLSource(cfg).connect()
        self.assertIn("INTEGRATIONS", str(ctx.exception))

    def test_lsl_source_close_is_idempotent(self):
        src = self._connect()
        src.close()
        src.close()
        self.assertFalse(src.connected)


@unittest.skipUnless(PYLSL_AVAILABLE, "未安装 pylsl")
class TestProcessedOutletPublish(unittest.TestCase):
    def test_publishes_processed_stream(self):
        from lsl_apdf.recording import ProcessedOutlet

        outlet = ProcessedOutlet("lsl_apdf_test_processed", srate=16.0, source_id="test_pub")
        outlet.open()
        outlet.push([12.0, 5.0, 12.0, 40.0], 1000.0)
        outlet.push([13.0, 5.1, 12.1, 41.0], 1000.0625)
        self.assertEqual(outlet.pushed, 2)
        outlet.close()
        self.assertEqual(outlet.pushed, 2)


class TestStreamMetaFallback(unittest.TestCase):
    """desc 为空（resolve 的短信息就是这种）时应退回 ch1，而不是崩溃。"""

    def test_label_fallback(self):
        from lsl_apdf.sources import StreamMeta

        meta = StreamMeta(name="x", n_channels=2)
        self.assertEqual(meta.label_for(1), "ch1")
        self.assertEqual(meta.unit_for(1), "a.u.")
        self.assertIn("after connecting", meta.describe())


class TestGuiSmoke(unittest.TestCase):
    """真正创建窗口跑几帧。没有显示环境时跳过。"""

    def setUp(self):
        try:
            import tkinter

            self.root = tkinter.Tk()
        except Exception as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"无法创建 Tk 窗口：{exc}")
        self.root.withdraw()  # 不弹窗，避免影响用户

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_window_builds_and_ticks(self):
        from lsl_apdf.gui import SYNTHETIC_ENTRY, ApdfPluginApp

        cfg = AppConfig()
        cfg.export.enabled = False
        cfg.display_window_sec = 2.0
        cfg.signal.startup_skip_sec = 0.1
        app = ApdfPluginApp(self.root, cfg)
        self.assertEqual(app.stream_var.get(), SYNTHETIC_ENTRY)

        # 用合成信号连接（不依赖硬件），跑几十帧
        app.synthetic_var.set(True)
        app._connect()
        self.assertIsNotNone(app.session)
        try:
            for _ in range(60):
                self.root.update()
                time.sleep(0.01)
            snap = app.session.snapshot()
            self.assertGreater(snap.stats.n_samples, 0)
            app._update_plots(snap)
            app._update_panels(snap)
            app.canvas.draw()
            # 标定按钮路径
            app._start_calibration()
            for _ in range(20):
                self.root.update()
                time.sleep(0.01)
            app._skip_phase()
            self.root.update()
        finally:
            app._disconnect()
        self.assertIsNone(app.session)

    def test_param_application_clamps_hop(self):
        from lsl_apdf.gui import ApdfPluginApp

        cfg = AppConfig()
        cfg.export.enabled = False
        app = ApdfPluginApp(self.root, cfg)
        app.param_vars["rms_window"].set("0.2")
        app.param_vars["rms_hop"].set("0.5")  # 大于窗长 → 应被夹到窗长
        app.param_vars["notch"].set("0")
        app._apply_params()
        self.assertAlmostEqual(app.cfg.signal.rms_window_sec, 0.2)
        self.assertLessEqual(app.cfg.signal.rms_hop_sec, app.cfg.signal.rms_window_sec)
        self.assertIsNone(app.cfg.signal.notch_hz)

    def test_param_application_tolerates_garbage(self):
        from lsl_apdf.gui import ApdfPluginApp

        cfg = AppConfig()
        cfg.export.enabled = False
        app = ApdfPluginApp(self.root, cfg)
        app.param_vars["rms_window"].set("abc")
        app.param_vars["hp"].set("")
        app._apply_params()  # 不应抛异常，应保留原值
        self.assertGreater(app.cfg.signal.rms_window_sec, 0)


if __name__ == "__main__":
    unittest.main()
