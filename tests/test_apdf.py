"""APDF 算法测试：分位点方向、曲线自洽性、尺度不变性、边界情况。"""

from __future__ import annotations

import unittest

import numpy as np

from lsl_apdf.apdf import (
    ApdfAccumulator,
    compute_apdf,
    exceedance_probability,
    level_percentiles,
    levels_at_probabilities,
)
from lsl_apdf.config import ApdfConfig


class TestApdfCore(unittest.TestCase):
    def test_uniform_distribution_percentiles(self):
        """均匀分布下 static/median/peak 必须等于 10/50/90 %MVE。"""
        amps = np.linspace(0.0, 100.0, 10001)
        r = compute_apdf(amps, mve_ref=100.0, window_sec=0.125, hop_sec=0.0625)
        self.assertAlmostEqual(r.static_level, 10.0, places=1)
        self.assertAlmostEqual(r.median_level, 50.0, places=1)
        self.assertAlmostEqual(r.peak_level, 90.0, places=1)

    def test_static_is_low_and_peak_is_high(self):
        """方向性回归测试。

        这是最容易搞反的地方：静态负荷是"几乎一直存在"的**低**水平，
        峰值负荷是偶尔出现的**高**水平。若有人把两者对调，本测试会失败。
        """
        # 连续递增分布：10 → 90 %MVE
        amps = np.linspace(10.0, 90.0, 4001)
        r = compute_apdf(amps, mve_ref=100.0, window_sec=1.0, hop_sec=1.0)
        self.assertLess(r.static_level, r.median_level)
        self.assertLess(r.median_level, r.peak_level)
        self.assertAlmostEqual(r.static_level, 18.0, places=1)
        self.assertAlmostEqual(r.median_level, 50.0, places=1)
        self.assertAlmostEqual(r.peak_level, 82.0, places=1)

    def test_static_is_lowest_peak_is_highest_on_two_level_signal(self):
        """两档信号（静息/用力）下，静态档必须对应低幅值档。"""
        amps = np.concatenate([np.full(800, 10.0), np.full(200, 90.0)])
        r = compute_apdf(amps, mve_ref=100.0, window_sec=1.0, hop_sec=1.0)
        self.assertAlmostEqual(r.static_level, 10.0, places=6)
        self.assertAlmostEqual(r.median_level, 10.0, places=6)
        self.assertAlmostEqual(r.peak_level, 90.0, places=6)

    def test_exceedance_curve_is_consistent_with_levels(self):
        """曲线自洽：static 被超过的时间≈90%，peak≈10%（连续分布下成立）。"""
        amps = np.linspace(0.0, 200.0, 20001)
        r = compute_apdf(amps, mve_ref=200.0, window_sec=0.125)
        levels_pct = amps / 200.0 * 100.0
        ex_static = exceedance_probability(levels_pct, np.array([r.static_level]))[0]
        ex_peak = exceedance_probability(levels_pct, np.array([r.peak_level]))[0]
        self.assertAlmostEqual(ex_static, 0.90, places=2)
        self.assertAlmostEqual(ex_peak, 0.10, places=2)

    def test_level_at_exceedance_matches_percentiles(self):
        amps = np.linspace(1.0, 100.0, 5000)
        r = compute_apdf(amps, mve_ref=100.0, window_sec=0.125)
        # 超概率 90% ↔ 幅值 P10
        self.assertAlmostEqual(r.level_at_exceedance(0.90), r.static_level, places=1)
        self.assertAlmostEqual(r.level_at_exceedance(0.50), r.median_level, places=1)
        self.assertAlmostEqual(r.level_at_exceedance(0.10), r.peak_level, places=1)
        # 单调递减：概率越大，读到的水平越低
        self.assertGreater(
            r.level_at_exceedance(0.05), r.level_at_exceedance(0.95)
        )

    def test_level_at_percentile_alias(self):
        amps = np.linspace(0.0, 100.0, 1001)
        r = compute_apdf(amps, mve_ref=100.0, window_sec=0.125)
        self.assertAlmostEqual(r.level_at_percentile(10.0), r.static_level, places=1)
        self.assertAlmostEqual(r.level_at_percentile(90.0), r.peak_level, places=1)

    def test_scale_invariance(self):
        """线性换算 / 常数缩放不改变 %MVE 结果（ADC → mV 也不影响）。"""
        rng = np.random.default_rng(42)
        amps = np.abs(rng.normal(50.0, 20.0, 3000))
        a = compute_apdf(amps, mve_ref=100.0, window_sec=0.125)
        b = compute_apdf(amps * 3.2258, mve_ref=100.0 * 3.2258, window_sec=0.125)
        self.assertAlmostEqual(a.static_level, b.static_level, places=9)
        self.assertAlmostEqual(a.peak_level, b.peak_level, places=9)
        self.assertAlmostEqual(a.median_level, b.median_level, places=9)

    def test_time_span_with_overlapping_windows(self):
        """重叠窗下覆盖时长按 hop 计，不应被高估为 n*window。"""
        amps = np.ones(100)
        r = compute_apdf(amps, mve_ref=1.0, window_sec=0.125, hop_sec=0.0625)
        self.assertAlmostEqual(r.total_sec, 99 * 0.0625 + 0.125, places=9)
        self.assertLess(r.total_sec, r.n_windows * r.window_sec)

    def test_mve_ref_is_mandatory(self):
        amps = np.ones(10)
        with self.assertRaises(ValueError):
            compute_apdf(amps, mve_ref=None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            compute_apdf(amps, mve_ref=0.0)
        with self.assertRaises(ValueError):
            compute_apdf(amps, mve_ref=-1.0)
        with self.assertRaises(ValueError):
            compute_apdf(amps, mve_ref=float("nan"))

    def test_empty_and_nonfinite_input(self):
        r = compute_apdf(np.array([]), mve_ref=10.0, window_sec=0.125)
        self.assertEqual(r.n_windows, 0)
        self.assertEqual(r.total_sec, 0.0)
        self.assertTrue(np.isnan(r.static_level))

        r2 = compute_apdf(np.array([1.0, np.nan, 2.0, np.inf]), mve_ref=1.0)
        self.assertEqual(r2.n_windows, 2)
        self.assertTrue(any("non-finite" in n for n in r2.notes))

    def test_axis_auto_expand_above_100_percent(self):
        """幅值超过 100 %MVE 时网格应自动扩展，且不丢数据。"""
        amps = np.array([60.0, 120.0, 180.0])
        r = compute_apdf(amps, mve_ref=100.0, window_sec=1.0)
        self.assertGreater(r.max_level_pct, 100.0)
        self.assertGreaterEqual(r.levels_pct[-1], 180.0)
        self.assertTrue(any("auto-expanded" in n for n in r.notes))

    def test_probability_grid_rows_shape(self):
        r = compute_apdf(np.linspace(0, 100, 500), mve_ref=100.0, window_sec=0.125)
        prob_rows = list(r.probability_grid_rows())
        level_rows = list(r.level_grid_rows())
        self.assertGreater(len(prob_rows), 100)
        self.assertGreater(len(level_rows), 100)
        self.assertAlmostEqual(prob_rows[0][0], 0.0, places=9)
        self.assertAlmostEqual(prob_rows[-1][0], 100.0, places=6)
        # 概率从 0→100，对应幅值应从大到小
        self.assertGreater(prob_rows[0][1], prob_rows[-1][1])

    def test_summary_contains_both_conventions(self):
        r = compute_apdf(np.linspace(0, 50, 500), mve_ref=50.0, window_sec=0.125)
        s = r.summary()
        for key in (
            "static_level_pct",
            "median_level_pct",
            "peak_level_pct",
            "static_exceedance_pct",
            "peak_exceedance_pct",
        ):
            self.assertIn(key, s)
        self.assertAlmostEqual(s["static_exceedance_pct"], 90.0, places=6)
        self.assertAlmostEqual(s["peak_exceedance_pct"], 10.0, places=6)

    def test_custom_percentiles(self):
        cfg = ApdfConfig(static_percentile=25.0, median_percentile=50.0, peak_percentile=75.0)
        amps = np.linspace(0.0, 100.0, 10001)
        r = compute_apdf(amps, mve_ref=100.0, cfg=cfg, window_sec=1.0)
        self.assertAlmostEqual(r.static_level, 25.0, places=1)
        self.assertAlmostEqual(r.peak_level, 75.0, places=1)

    def test_invalid_percentile_config_rejected(self):
        cfg = ApdfConfig(static_percentile=90.0, peak_percentile=10.0)
        self.assertTrue(cfg.validate())


class TestHelpers(unittest.TestCase):
    def test_exceedance_probability_known_values(self):
        levels = np.array([0.0, 1.0, 2.0, 3.0])
        got = exceedance_probability(levels, np.array([-1.0, 0.0, 1.0, 3.0, 4.0]))
        np.testing.assert_allclose(got, [1.0, 0.75, 0.5, 0.0, 0.0])

    def test_levels_at_probabilities_endpoints(self):
        levels = np.array([1.0, 2.0, 3.0, 4.0])
        got = levels_at_probabilities(levels, np.array([0.0, 0.5, 1.0]))
        self.assertAlmostEqual(got[0], 4.0, places=6)   # P=0 → 最大值
        self.assertAlmostEqual(got[2], 1.0, places=6)   # P=1 → 最小值

    def test_level_percentiles_helper(self):
        got = level_percentiles(np.arange(101, dtype=float))
        self.assertAlmostEqual(got["P10"], 10.0, places=6)
        self.assertAlmostEqual(got["P90"], 90.0, places=6)
        empty = level_percentiles(np.array([]))
        self.assertTrue(np.isnan(empty["P50"]))


class TestAccumulator(unittest.TestCase):
    def test_accumulates_and_caches(self):
        acc = ApdfAccumulator(ApdfConfig())
        acc.add(np.array([10.0, 20.0, 30.0]))
        acc.add(40.0)
        acc.add_one(50.0)
        self.assertEqual(acc.n_samples, 5)

        r1 = acc.compute(mve_ref=100.0, window_sec=0.125)
        r2 = acc.compute(mve_ref=100.0, window_sec=0.125)
        self.assertIs(r1, r2)  # 命中缓存

        r3 = acc.compute(mve_ref=50.0, window_sec=0.125)
        self.assertIsNot(r1, r3)
        self.assertAlmostEqual(r3.median_level, r1.median_level * 2, places=9)

    def test_cache_key_tracks_new_data(self):
        acc = ApdfAccumulator(ApdfConfig())
        acc.add([1.0, 2.0])
        r1 = acc.compute(mve_ref=1.0, window_sec=0.125)
        acc.add([3.0])
        r2 = acc.compute(mve_ref=1.0, window_sec=0.125)
        self.assertIsNot(r1, r2)
        self.assertEqual(r2.n_windows, 3)

    def test_clear_and_growth(self):
        acc = ApdfAccumulator(ApdfConfig())
        acc.add(np.arange(50000, dtype=float))  # 触发多次扩容
        self.assertEqual(acc.n_samples, 50000)
        self.assertEqual(acc.amplitudes()[-1], 49999.0)
        acc.clear()
        self.assertEqual(acc.n_samples, 0)
        self.assertTrue(np.isnan(acc.compute(mve_ref=1.0).static_level))


if __name__ == "__main__":
    unittest.main()
