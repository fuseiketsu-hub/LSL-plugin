"""统一插件（单进程）测试：执行器档位/渐变/驱动写入 + %MVC 口径的 APDF。"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.dsp import GrowableF64
from lsl_apdf.session_runner import IntegratedConfig

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
HAS_PLUGIN = (PLUGIN_DIR / "rawaccel_bridge.py").exists()


@unittest.skipUnless(HAS_PLUGIN, "未找到 DPI 插件目录（rawaccel_bridge）")
class TestDpiActuator(unittest.TestCase):
    def setUp(self):
        # 注意清理顺序：cleanup 是 LIFO。先注册删目录（最后执行），
        # 各测试再注册 actuator.close()（先执行）——否则 Windows 上
        # 日志文件还开着就去删目录，会 PermissionError。
        import shutil

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _make(self, **over):
        from lsl_apdf.unified import DpiActuator

        cfg = IntegratedConfig(plugin_dir=str(PLUGIN_DIR), dry_run=True,
                               out_dir=self.tmp, switch_ramp_sec=10.0, **over)
        d = Path(self.tmp) / "s"
        d.mkdir(parents=True, exist_ok=True)
        act = DpiActuator(cfg, d, ["ECU", "FDS"])
        self.addCleanup(act.close)          # 先于 rmtree 执行
        return act, d

    def test_initial_state_is_base_level(self):
        act, _ = self._make()
        self.assertEqual(act.level, "low")
        self.assertAlmostEqual(act.effective_dpi, 800.0)
        self.assertFalse(act.ramping)

    def test_request_takes_level_not_action(self):
        """回归测试：必须传档位（low/high）。传动作（up/down）要被拒绝 ——
        之前就是把策略返回的 up/down 直接当档位传进来，导致 DPI 永远不动。"""
        act, _ = self._make()
        self.assertFalse(act.request("up", "wrong", 0.0))
        self.assertFalse(act.request("down", "wrong", 0.0))
        self.assertEqual(act.level, "low")
        self.assertFalse(act.ramping)
        self.assertTrue(act.request("high", "ok", 0.0))

    def test_ramp_advances_and_completes(self):
        act, _ = self._make()
        act.request("high", "test", 0.0)
        act.tick(0.0)                       # 刚起步
        self.assertAlmostEqual(act.sens, 1.0, places=3)
        self.assertEqual(act.level, "low")  # 渐变未完成，档位还是旧档
        act.tick(5.0)                       # 一半
        self.assertAlmostEqual(act.sens, 1.25, places=3)
        self.assertTrue(act.ramping)
        act.tick(10.0)                      # 完成
        self.assertAlmostEqual(act.sens, 1.5, places=6)
        self.assertEqual(act.level, "high")
        self.assertAlmostEqual(act.effective_dpi, 1200.0)
        self.assertFalse(act.ramping)

    def test_duplicate_request_ignored(self):
        act, _ = self._make()
        self.assertTrue(act.request("high", "first", 0.0))
        self.assertFalse(act.request("high", "again", 0.1))

    def test_reset_returns_to_base(self):
        act, _ = self._make()
        act.request("high", "x", 0.0)
        act.tick(10.0)
        act.reset(12.0)
        self.assertEqual(act.level, "low")
        self.assertAlmostEqual(act.effective_dpi, 800.0)
        self.assertFalse(act.ramping)

    def test_sens_log_columns_match_plugin_format(self):
        """列必须和插件原版 sens_log 一致，否则 tools/plot_log.py 画不了。"""
        act, _ = self._make()
        act.request("high", "reason-here", 0.0)
        act.tick(10.0, {"ECU": 28.5, "FDS": 9.1})
        act.close()
        with act.path.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], [
            "unix_ms", "elapsed_s", "iso", "mode", "event", "block_idx",
            "target_dpi", "target_sens", "applied_sens", "effective_dpi",
            "emg_EDC", "emg_ECU", "emg_FDS", "emg_UT", "reason"])
        self.assertIn("EMG_SWITCH", [r[4] for r in rows[1:]])
        ecu_col = rows[0].index("emg_ECU")
        last = rows[-1]
        self.assertAlmostEqual(float(last[ecu_col]), 28.5, places=2)
        self.assertIn("reason-here", last[-1])

    def test_no_bom_in_sens_log(self):
        """插件原版写 utf-8-sig（带 BOM），统一插件不写 BOM 便于 numpy/pandas 直读。"""
        act, _ = self._make()
        act.close()
        raw = act.path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))


class TestUnifiedApdf(unittest.TestCase):
    """%MVC 序列 → APDF：mve_ref=100 时结果直接落在 %MVC 上。"""

    def _rows(self, values_by_muscle: dict[str, np.ndarray], cfg: IntegratedConfig):
        from lsl_apdf.unified import UnifiedSession

        with tempfile.TemporaryDirectory() as tmp:
            cfg.out_dir = tmp
            session = UnifiedSession(cfg, source=None, printer=lambda *a, **k: None)
            accum = {}
            for m, v in values_by_muscle.items():
                g = GrowableF64()
                g.append(v)
                accum[m] = g
            return session._compute_apdf(accum, list(values_by_muscle))

    def test_half_high_half_low(self):
        cfg = IntegratedConfig(dry_run=True)
        vals = np.concatenate([np.full(500, 30.0), np.full(500, 10.0)])
        rows = self._rows({"ECU": vals}, cfg)
        r = rows[0]
        self.assertEqual(r.muscle, "ECU")
        self.assertEqual(r.n_windows, 1000)
        # 30% 与 10% 各占一半 → 中位数落在两者之间；阈值 20 以上占 50%
        self.assertGreater(r.median_pct, 10.0)
        self.assertLess(r.median_pct, 30.0)
        self.assertAlmostEqual(r.high_ratio, 50.0, places=1)
        # 面积 = (30-20) * 500 窗 * hop(0.0625)
        self.assertAlmostEqual(r.high_area, 10.0 * 500 * 0.0625, delta=1.0)

    def test_levels_are_in_percent_mvc(self):
        cfg = IntegratedConfig(dry_run=True)
        # 恒定 40 %MVC → APDF 三个水平都应是 40（单位就是 %MVC）
        rows = self._rows({"FDS": np.full(400, 40.0)}, cfg)
        r = rows[0]
        self.assertAlmostEqual(r.static_pct, 40.0, places=6)
        self.assertAlmostEqual(r.median_pct, 40.0, places=6)
        self.assertAlmostEqual(r.peak_pct, 40.0, places=6)
        self.assertAlmostEqual(r.high_ratio, 100.0, places=6)

    def test_empty_muscle_is_nan_not_crash(self):
        cfg = IntegratedConfig(dry_run=True)
        rows = self._rows({"ECU": np.array([])}, cfg)
        self.assertEqual(rows[0].n_windows, 0)
        self.assertTrue(np.isnan(rows[0].median_pct))


if __name__ == "__main__":
    unittest.main()
