"""DSP 测试：滤波设计、流式滤波、增量 RMS、可增长数组。"""

from __future__ import annotations

import unittest

import numpy as np
from scipy import signal

from lsl_apdf.dsp import (
    GrowableF64,
    RmsExtractor,
    StreamingFilter,
    design_filter_chain,
    filter_offline,
    rms,
)


class TestFilterDesign(unittest.TestCase):
    def test_bandpass_defaults(self):
        d = design_filter_chain(1000.0, 20.0, 450.0, 50.0, 30.0, 4)
        self.assertIsNotNone(d.sos)
        self.assertAlmostEqual(d.highpass_hz, 20.0)
        self.assertAlmostEqual(d.lowpass_hz, 450.0)
        self.assertAlmostEqual(d.notch_hz, 50.0)
        self.assertIn("HP 20 Hz", d.describe())
        self.assertIn("NOTCH 50 Hz", d.describe())

    def test_lowpass_clamped_to_nyquist(self):
        """低采样率设备上，450 Hz 低通应被夹紧而不是抛异常。"""
        d = design_filter_chain(500.0, 20.0, 450.0, 0, 30.0, 4)
        self.assertLess(d.lowpass_hz, 250.0)
        self.assertTrue(any("clamped" in n for n in d.notes))

    def test_highpass_above_nyquist_disabled(self):
        d = design_filter_chain(100.0, 80.0, 200.0, 0, 30.0, 4)
        self.assertIsNone(d.highpass_hz)
        self.assertTrue(any("high-pass disabled" in n for n in d.notes))

    def test_notch_above_nyquist_ignored(self):
        d = design_filter_chain(80.0, 5.0, 30.0, 50.0, 30.0, 4)
        self.assertIsNone(d.notch_hz)
        self.assertTrue(any("Notch" in n for n in d.notes))

    def test_no_filter_requested(self):
        d = design_filter_chain(1000.0, 0, 0, 0)
        self.assertIsNone(d.sos)
        self.assertIn("no filter", d.describe())

    def test_invalid_srate(self):
        with self.assertRaises(ValueError):
            design_filter_chain(0.0)


class TestFiltering(unittest.TestCase):
    def test_highpass_removes_dc_offset(self):
        fs = 1000.0
        t = np.arange(int(fs * 5)) / fs
        x = 1650.0 + 10.0 * np.sin(2 * np.pi * 100 * t)
        d = design_filter_chain(fs, 20.0, 450.0, None, 30.0, 4)
        y = filter_offline(x, fs, d)
        self.assertLess(abs(float(np.mean(y))), 0.5)
        self.assertAlmostEqual(float(np.std(y)), 10.0 / np.sqrt(2), delta=0.3)

    def test_notch_attenuates_line_noise(self):
        fs = 1000.0
        t = np.arange(int(fs * 6)) / fs
        emg = np.random.default_rng(0).standard_normal(t.size) * 5.0
        x = 50.0 * np.sin(2 * np.pi * 50 * t) + emg
        with_notch = filter_offline(x, fs, design_filter_chain(fs, 20.0, 450.0, 50.0, 30.0, 4))
        without = filter_offline(x, fs, design_filter_chain(fs, 20.0, 450.0, None, 30.0, 4))
        # 陷波后总功率应明显更小（50 Hz 分量被去掉）
        self.assertLess(float(np.std(with_notch)), float(np.std(without)))

    def test_streaming_matches_batch(self):
        """分块流式滤波应与一次性滤波结果几乎一致（状态必须被正确保持）。

        实时路径会先"预热"滤波器（用首个数据块建立状态），所以对照的批量结果
        也要在同样的初始条件下算：先喂一遍前 500 个样本再算全段，然后丢掉
        预热段。
        """
        fs = 1000.0
        t = np.arange(int(fs * 4)) / fs
        x = 100.0 + 20.0 * np.sin(2 * np.pi * 60 * t) + 5 * np.sin(2 * np.pi * 200 * t)
        d = design_filter_chain(fs, 20.0, 450.0, None, 30.0, 4)
        pre = 500
        expected = signal.sosfilt(d.sos, np.concatenate([x[:pre], x]))[pre:]

        sf = StreamingFilter(d)
        sf.warmup(x[:pre])
        chunks = [sf.process(x[i:i + 137]) for i in range(0, x.size, 137)]
        streamed = np.concatenate(chunks)
        np.testing.assert_allclose(streamed, expected, atol=1e-9)

    def test_streaming_is_independent_of_chunk_size(self):
        """块大小不应影响流式结果（状态保持正确、块间无跳变）。"""
        fs = 1000.0
        rng = np.random.default_rng(3)
        x = rng.standard_normal(4000)
        d = design_filter_chain(fs, 20.0, 450.0, None, 30.0, 4)

        def run(step: int) -> np.ndarray:
            sf = StreamingFilter(d)
            sf.warmup(x[:100])
            return np.concatenate([sf.process(x[i:i + step]) for i in range(0, x.size, step)])

        a = run(100)
        b = run(333)
        c = run(4000)
        np.testing.assert_allclose(a, b, atol=1e-12)
        np.testing.assert_allclose(a, c, atol=1e-12)

    def test_streaming_output_lengths_preserved(self):
        d = design_filter_chain(1000.0, 20.0, 450.0)
        sf = StreamingFilter(d)
        for n in (1, 10, 999):
            self.assertEqual(sf.process(np.zeros(n)).size, n)

    def test_no_filter_passthrough(self):
        d = design_filter_chain(1000.0, 0, 0, 0)
        sf = StreamingFilter(d)
        x = np.array([1.0, 2.0, 3.0])
        np.testing.assert_allclose(sf.process(x), x)


class TestRmsExtractor(unittest.TestCase):
    def test_sine_rms_matches_theory(self):
        fs = 1000.0
        amp = 7.0
        t = np.arange(int(fs * 2)) / fs
        x = amp * np.sin(2 * np.pi * 100 * t)
        ex = RmsExtractor(fs, window_sec=0.1, hop_sec=0.05)
        values, times = ex.push(x, t)
        self.assertAlmostEqual(float(np.median(values)), amp / np.sqrt(2), delta=0.05)
        self.assertEqual(values.size, times.size)

    def test_window_and_hop_counts(self):
        fs = 100.0
        ex = RmsExtractor(fs, window_sec=0.5, hop_sec=0.25)
        values, _ = ex.push(np.ones(1000))
        # 首窗在 50 样本处产出，之后每 25 样本一个 → 1 + (1000-50)//25 = 39
        self.assertEqual(values.size, 1 + (1000 - 50) // 25)
        self.assertAlmostEqual(float(values[0]), 1.0, places=9)

    def test_cross_chunk_continuity(self):
        """跨块边界的窗口不能漏算或重复：分块与整块结果必须一致。"""
        fs = 1000.0
        rng = np.random.default_rng(7)
        x = rng.standard_normal(5000)
        whole = RmsExtractor(fs, 0.125, 0.0625)
        v_whole, t_whole = whole.push(x, np.arange(5000) / fs)

        piecewise = RmsExtractor(fs, 0.125, 0.0625)
        vs, ts = [], []
        for i in range(0, 5000, 333):
            v, tt = piecewise.push(x[i:i + 333], np.arange(i, min(i + 333, 5000)) / fs)
            vs.append(v)
            ts.append(tt)
        v_pw = np.concatenate(vs)
        t_pw = np.concatenate(ts)
        np.testing.assert_allclose(v_pw, v_whole, atol=1e-12)
        np.testing.assert_allclose(t_pw, t_whole, atol=1e-12)

    def test_timestamps_are_window_end(self):
        fs = 10.0
        ex = RmsExtractor(fs, window_sec=0.5, hop_sec=0.5)
        t = np.arange(10) / fs
        _, times = ex.push(np.ones(10), t)
        np.testing.assert_allclose(times, [0.4, 0.9])

    def test_large_push_is_batched(self):
        """一次喂入很长的数据也不应爆内存（内部分批）。"""
        ex = RmsExtractor(1000.0, 0.125, 0.0625)
        values, _ = ex.push(np.ones(500_000))
        self.assertEqual(values.size, 1 + (500_000 - 125) // 62)

    def test_invalid_params(self):
        with self.assertRaises(ValueError):
            RmsExtractor(1000.0, 0.0, 0.1)
        with self.assertRaises(ValueError):
            RmsExtractor(1000.0, 0.1, 0.2)  # hop > window
        with self.assertRaises(ValueError):
            RmsExtractor(0.0, 0.1, 0.05)

    def test_timestamp_length_mismatch(self):
        ex = RmsExtractor(1000.0, 0.1, 0.05)
        with self.assertRaises(ValueError):
            ex.push(np.ones(10), np.ones(5))

    def test_reset(self):
        ex = RmsExtractor(1000.0, 0.1, 0.05)
        ex.push(np.ones(1000))
        self.assertGreater(ex.emitted, 0)
        ex.reset()
        self.assertEqual(ex.emitted, 0)
        self.assertEqual(ex.pending, 0)

    def test_empty_push(self):
        ex = RmsExtractor(1000.0, 0.1, 0.05)
        v, t = ex.push(np.array([]))
        self.assertEqual(v.size, 0)
        self.assertEqual(t.size, 0)


class TestGrowableF64(unittest.TestCase):
    def test_growth_preserves_data(self):
        g = GrowableF64(initial_capacity=4)
        for i in range(1000):
            g.append([float(i)])
        self.assertEqual(len(g), 1000)
        arr = g.to_array()
        np.testing.assert_allclose(arr, np.arange(1000, dtype=float))

    def test_append_array_and_empty(self):
        g = GrowableF64()
        g.append(np.arange(100.0))
        g.append(np.array([]))
        self.assertEqual(len(g), 100)
        g.clear()
        self.assertEqual(len(g), 0)
        self.assertEqual(g.to_array().size, 0)

    def test_view_is_readonly(self):
        g = GrowableF64()
        g.append([1.0, 2.0])
        v = g.view()
        with self.assertRaises(ValueError):
            v[0] = 5.0

    def test_to_array_is_copy(self):
        g = GrowableF64()
        g.append([1.0, 2.0])
        arr = g.to_array()
        arr[0] = 99.0
        self.assertEqual(g.to_array()[0], 1.0)


class TestRmsHelper(unittest.TestCase):
    def test_rms_values(self):
        self.assertAlmostEqual(rms(np.array([3.0, 4.0])), np.sqrt(12.5), places=9)
        self.assertEqual(rms(np.array([])), 0.0)


if __name__ == "__main__":
    unittest.main()
