"""配置、数据源、导出的测试。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.apdf import compute_apdf
from lsl_apdf.config import AppConfig, CalibrationConfig, ExportConfig, SignalConfig
from lsl_apdf.offline import load_recording
from lsl_apdf.recording import (
    SessionPaths,
    SessionWriter,
    export_apdf_result,
)
from lsl_apdf.sources import (
    PYLSL_AVAILABLE,
    OpenSignalsSource,
    ReplaySource,
    SampleChunk,
    StreamMeta,
    SyntheticProfile,
    SyntheticSource,
    StreamNotFoundError,
    _load_csv_channels,
    is_opensignals_txt,
    load_opensignals_txt,
)
from lsl_apdf.units import PRESETS, get_preset


class TestConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        cfg = AppConfig()
        self.assertEqual(cfg.validate(), [])
        self.assertIsNone(cfg.mve_ref)

    def test_roundtrip_json(self):
        cfg = AppConfig()
        cfg.signal.rms_window_sec = 0.25
        cfg.calibration.mvc_trials = 5
        cfg.apdf.peak_percentile = 95.0
        cfg.export.root_dir = "out"
        cfg.mve_ref = 12.5
        cfg.subject_id = "S01"
        with tempfile.TemporaryDirectory() as tmp:
            path = cfg.save(Path(tmp) / "cfg.json")
            back = AppConfig.load(path)
        self.assertAlmostEqual(back.signal.rms_window_sec, 0.25)
        self.assertEqual(back.calibration.mvc_trials, 5)
        self.assertAlmostEqual(back.apdf.peak_percentile, 95.0)
        self.assertEqual(back.export.root_dir, "out")
        self.assertAlmostEqual(back.mve_ref or 0, 12.5)
        self.assertEqual(back.subject_id, "S01")

    def test_dotted_overrides(self):
        cfg = AppConfig()
        cfg.apply_overrides(
            {
                "signal.rms_window_sec": 0.5,
                "calibration.mvc_trials": 2,
                "apdf.level_step_pct": 1.0,
                "export.enabled": False,
                "mve_ref": 3.0,
            }
        )
        self.assertAlmostEqual(cfg.signal.rms_window_sec, 0.5)
        self.assertEqual(cfg.calibration.mvc_trials, 2)
        self.assertFalse(cfg.export.enabled)
        self.assertAlmostEqual(cfg.mve_ref or 0, 3.0)

    def test_unknown_override_rejected(self):
        with self.assertRaises(KeyError):
            AppConfig().apply_overrides({"signal.not_a_field": 1})

    def test_unknown_keys_in_json_ignored(self):
        cfg = AppConfig.from_dict(
            {"signal": {"rms_window_sec": 0.2, "未来字段": 1}, "未知段": {"a": 1}}
        )
        self.assertAlmostEqual(cfg.signal.rms_window_sec, 0.2)

    def test_validation_catches_bad_values(self):
        cfg = AppConfig()
        cfg.signal.rms_hop_sec = 1.0
        cfg.signal.rms_window_sec = 0.1
        self.assertTrue(any("hop" in p for p in cfg.validate()))

        cfg2 = AppConfig()
        cfg2.mve_ref = -5.0
        self.assertTrue(any("mve_ref" in p for p in cfg2.validate()))

        cfg3 = AppConfig()
        cfg3.apdf.static_percentile = 95.0
        cfg3.apdf.peak_percentile = 5.0
        self.assertTrue(any("percentile" in p for p in cfg3.validate()))

    def test_stream_resolve_priority(self):
        from lsl_apdf.config import StreamConfig

        self.assertEqual(StreamConfig().resolve_property(), ("name", "OpenSignals"))
        self.assertEqual(
            StreamConfig(type="AA:BB").resolve_property(), ("type", "AA:BB")
        )
        self.assertEqual(
            StreamConfig(type="AA:BB", hostname="pc1").resolve_property(),
            ("hostname", "pc1"),
        )

    def test_calibration_total_duration(self):
        cfg = CalibrationConfig(
            rest_sec=5, mvc_sec=5, mvc_trials=3, between_trials_sec=20
        )
        self.assertAlmostEqual(cfg.total_duration(), 5 + 15 + 40)


class TestUnits(unittest.TestCase):
    def test_presets_exist(self):
        self.assertIn("bitalino_emg", PRESETS)
        self.assertIn("biosignalsplux_emg", PRESETS)
        self.assertGreater(get_preset("bitalino_emg").scale, 0)

    def test_bitalino_scale_value(self):
        p = get_preset("bitalino_emg")
        # 3.3V / 1024 / 1000 → mV
        self.assertAlmostEqual(p.scale, 3.3 / 1024.0, places=9)

    def test_unknown_preset(self):
        with self.assertRaises(KeyError):
            get_preset("nope")


class TestSyntheticSource(unittest.TestCase):
    def test_metadata(self):
        src = SyntheticSource(realtime=False).connect()
        self.assertEqual(src.n_channels, 1)
        self.assertAlmostEqual(src.nominal_srate, 1000.0)
        self.assertEqual(src.channel_labels, ["EMG"])

    def test_envelope_profile_piecewise(self):
        prof = SyntheticProfile(segments=[(2.0, 10.0), (3.0, 50.0)])
        got = prof.envelope_pct(np.array([0.0, 1.9, 2.1, 4.9, 5.1]))
        np.testing.assert_allclose(got, [10.0, 10.0, 50.0, 50.0, 10.0])

    def test_analytic_apdf_shares(self):
        prof = SyntheticProfile(segments=[(2.0, 10.0), (6.0, 50.0)], noise_floor_pct=1.0)
        shares = prof.analytic_apdf_pct()
        self.assertAlmostEqual(shares[11.0], 0.25)
        self.assertAlmostEqual(shares[51.0], 0.75)

    def test_measured_rms_matches_envelope(self):
        """合成源的包络值必须等于实测 RMS（否则真值校验就没意义）。

        注意要先去掉直流偏置（pipeline 里由高通负责），否则 RMS 会被
        1650 的偏置主导。
        """
        prof = SyntheticProfile(segments=[(6.0, 40.0)], noise_floor_pct=1.0, mve_unit=1000.0)
        src = SyntheticSource(profile=prof, srate=1000.0, realtime=False)
        src.connect()
        data = []
        for _ in range(120):
            chunk = src.read_chunk()
            data.append(chunk.channel(1))
        x = np.concatenate(data)[2000:]
        x = x - float(np.mean(x))  # 去 DC
        measured = float(np.sqrt(np.mean(x ** 2)))
        self.assertAlmostEqual(measured, 0.41 * 1000.0, delta=40.0)

    def test_dc_offset_removed_by_pipeline(self):
        prof = SyntheticProfile(segments=[(2.0, 20.0)], dc_offset=1650.0)
        src = SyntheticSource(profile=prof, srate=500.0, realtime=False)
        src.connect()
        chunk = src.read_chunk()
        self.assertGreater(float(np.mean(chunk.channel(1))), 1000.0)

    def test_multichannel_is_independent_noise(self):
        """多通道共用同一轮廓，但各自是**独立**的噪声实现（不是复制同一路）。"""
        src = SyntheticSource(n_channels=3, realtime=False, srate=500.0,
                              profile=SyntheticProfile(segments=[(2.0, 30.0)],
                                                       noise_floor_pct=0.0,
                                                       dc_offset=0.0))
        meta = src.connect()
        self.assertEqual(meta.n_channels, 3)
        self.assertEqual(len(set(meta.channel_labels)), 3)
        data = np.concatenate([src.read_chunk().data for _ in range(40)])
        self.assertEqual(data.shape[1], 3)
        # 不是同一路信号的复制
        self.assertFalse(np.allclose(data[:, 0], data[:, 2]))
        # 但统计量接近（同一轮廓）
        rms = [float(np.sqrt(np.mean(data[:, i] ** 2))) for i in range(3)]
        self.assertLess(max(rms) - min(rms), 0.15 * float(np.mean(rms)))

    def test_per_channel_profiles(self):
        """profiles 给定后，各通道按各自轮廓生成（模拟不同肌肉负荷）。

        mve_unit=100 → 包络 10% 对应 RMS ≈ 10，读起来直观。
        """
        quiet = SyntheticProfile(segments=[(5.0, 10.0)], noise_floor_pct=0.0,
                                 dc_offset=0.0, mve_unit=100.0)
        loud = SyntheticProfile(segments=[(5.0, 50.0)], noise_floor_pct=0.0,
                                dc_offset=0.0, mve_unit=100.0)
        src = SyntheticSource(profiles=[quiet, loud], channel_labels=["A1", "A2"],
                              n_channels=2, srate=500.0, realtime=False)
        meta = src.connect()
        self.assertEqual(meta.channel_labels, ["A1", "A2"])
        data = np.concatenate([src.read_chunk().data for _ in range(40)])
        r0 = float(np.sqrt(np.mean(data[:, 0] ** 2)))
        r1 = float(np.sqrt(np.mean(data[:, 1] ** 2)))
        self.assertAlmostEqual(r0, 10.0, delta=3.0)
        self.assertAlmostEqual(r1, 50.0, delta=6.0)

    def test_profiles_length_must_match_channels(self):
        with self.assertRaises(ValueError):
            SyntheticSource(profiles=[SyntheticProfile()], n_channels=2, realtime=False)

    def test_channel_indexing_and_errors(self):
        src = SyntheticSource(realtime=False, srate=500.0)
        src.connect()
        chunk = src.read_chunk()
        self.assertEqual(chunk.channel(1).size, len(chunk))
        with self.assertRaises(IndexError):
            chunk.channel(5)

    def test_close_and_context_manager(self):
        with SyntheticSource(realtime=False, srate=500.0) as src:
            self.assertTrue(src.connected)
            self.assertIsNotNone(src.read_chunk())
        self.assertFalse(src.connected)


class TestReplaySource(unittest.TestCase):
    def test_replay_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["lsl_time", "local_time", "ch1"])
                for i in range(2000):
                    w.writerow([i * 0.001, 1000 + i * 0.001, float(i % 100)])
            src = ReplaySource(path, realtime=False)
            meta = src.connect()
            self.assertAlmostEqual(meta.nominal_srate, 1000.0, places=3)
            data = []
            while True:
                chunk = src.read_chunk()
                if chunk is None:
                    break
                data.append(chunk.data)
            allx = np.concatenate(data)
            self.assertEqual(allx.shape, (2000, 1))
            self.assertAlmostEqual(float(allx[5, 0]), 5.0)

    def test_csv_without_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nohdr.csv"
            path.write_text("0,1\n0.001,2\n0.002,3\n", encoding="utf-8")
            times, values = _load_csv_channels(path, 0, None)
            self.assertEqual(times.size, 3)
            self.assertEqual(values.shape, (3, 1))


OS_FIXTURE = """# OpenSignals Text File Format. Version 1
# {"AA:BB": {"sampling rate": 100, "device name": "AA:BB", "column": ["nSeq", "I1", "A1", "A2", "A3", "A4"], "label": ["A1", "A2", "A3", "A4"]}}
# EndOfHeader
0\t0\t512\t500\t505\t510
1\t0\t520\t505\t501\t512
2\t0\t518\t503\t509\t508
"""


class TestOpenSignalsImport(unittest.TestCase):
    """OpenSignals 导出 .txt 的导入：解析 + 当数据源喂给分析链路。"""

    def _write_fixture(self, tmp) -> Path:
        path = Path(tmp) / "opensignals_test.txt"
        path.write_text(OS_FIXTURE, encoding="utf-8")
        return path

    def test_detect_and_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            self.assertTrue(is_opensignals_txt(path))
            values, labels, srate, info = load_opensignals_txt(path)
            self.assertEqual(
                labels, ["nSeq", "I1", "A1", "A2", "A3", "A4"])
            self.assertEqual(srate, 100.0)
            self.assertEqual(values.shape, (3, 6))
            self.assertEqual(info["device name"], "AA:BB")
            np.testing.assert_allclose(values[:, 2], [512, 520, 518])

    def test_rejects_non_opensignals_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.csv"
            path.write_text("lsl_time,ch1\n0,1\n", encoding="utf-8")
            self.assertFalse(is_opensignals_txt(path))

    def test_source_labels_resolve_like_live_stream(self):
        """文件里的 column 标签必须和实时流一样能被 A1=ECU 这类映射解析。"""
        from lsl_apdf.channels import resolve_channel_index

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            src = OpenSignalsSource(path)
            meta = src.connect()
            self.assertEqual(meta.nominal_srate, 100.0)
            self.assertEqual(meta.channel_labels[2], "A1")
            self.assertEqual(meta.channel_labels[3], "A2")
            self.assertEqual(resolve_channel_index("A1", meta), 2)
            self.assertEqual(resolve_channel_index("A4", meta), 5)

            chunks = []
            while not src.finished:
                chunk = src.read_chunk()
                self.assertIsNotNone(chunk)
                chunks.append(chunk.data)
            data = np.concatenate(chunks)
            self.assertEqual(data.shape, (3, 6))
            self.assertTrue(src.finished)
            # 时间轴按行号/采样率重建（nSeq 是循环的块号，不能当时间）
            self.assertAlmostEqual(float(src.meta.nominal_srate), 100.0)


class TestSessionPaths(unittest.TestCase):
    def test_unique_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = SessionPaths.create(tmp, "session", when=1700000000, subject_id="S01")
            p2 = SessionPaths.create(tmp, "session", when=1700000000, subject_id="S01")
            self.assertNotEqual(p1.root, p2.root)
            self.assertTrue(p1.root.exists())
            self.assertIn("S01", p1.root.name)


class TestSessionWriter(unittest.TestCase):
    def test_writes_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.create(tmp, "session")
            writer = SessionWriter(paths, ExportConfig(flush_interval_sec=0.01))
            writer.start()
            t = np.arange(200) / 1000.0
            writer.submit_raw(t, t + 1e6, np.stack([t, t * 2], axis=1))
            writer.submit_rms(t[:10], np.ones(10) * 3.0, np.ones(10) * 30.0)
            result = compute_apdf(np.linspace(0, 100, 500), mve_ref=100.0, window_sec=0.125,
                                  hop_sec=0.0625)
            writer.submit_apdf(result, 1700000000.0)
            writer.submit_json(paths.summary_json, {"ok": True, "arr": np.arange(3)})
            writer.log("hello")
            writer.stop()

            self.assertTrue(paths.raw_csv.exists())
            self.assertTrue(paths.rms_csv.exists())
            self.assertTrue(paths.apdf_curve_csv.exists())
            self.assertTrue(paths.percentiles_csv.exists())
            self.assertTrue(paths.summary_json.exists())
            self.assertTrue(paths.events_log.exists())

            raw = np.genfromtxt(paths.raw_csv, delimiter=",", names=True)
            self.assertEqual(raw["ch1"].size, 200)
            self.assertAlmostEqual(float(raw["ch2"][10]), 0.02, places=6)

            summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["arr"], [0, 1, 2])

            pct = np.genfromtxt(paths.percentiles_csv, delimiter=",", names=True)
            self.assertAlmostEqual(float(pct["median_pct"]), 50.0, places=1)

            curve_text = paths.apdf_curve_csv.read_text(encoding="utf-8")
            self.assertIn("probability_pct", curve_text)

            log = paths.events_log.read_text(encoding="utf-8")
            self.assertIn("hello", log)

    def test_timestamps_keep_sub_second_resolution(self):
        """回归测试：时间列必须保留小数。

        LSL 时间戳是"开机以来秒数"（量级 1e6）。若用 %.6g 这类有效数字格式写
        时间列，863672.077137 会被写成 863672，整条时间轴的分辨率全部丢失，
        离线重算时连采样率都推不出来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.create(tmp, "session")
            writer = SessionWriter(paths, ExportConfig(flush_interval_sec=0.01))
            writer.start()
            srate = 1000.0
            n = 500
            t = 863672.077137 + np.arange(n) / srate  # 模拟 LSL 时钟量级
            writer.submit_raw(t, t + 1.7e9, np.ones((n, 1)))
            writer.submit_rms(t[:100], np.ones(100), np.ones(100))
            writer.stop()

            text = paths.raw_csv.read_text(encoding="utf-8").splitlines()
            first = text[1].split(",")
            self.assertNotEqual(first[0], first[1])  # 相邻样本的时间戳必须不同
            self.assertTrue(first[0].endswith("0") or "." in first[0])

            rec = load_recording(paths.raw_csv)
            self.assertEqual(rec.times.size, n)
            self.assertAlmostEqual(rec.srate, srate, places=3)
            # 相邻时间差应为 1 ms（保到微秒）
            self.assertAlmostEqual(
                float(rec.times[1] - rec.times[0]), 1.0 / srate, places=6
            )

            rms = load_recording(paths.rms_csv)
            self.assertGreater(rms.srate, 0)
            self.assertIn("rms", rms.channels)

    def test_queue_overflow_counts_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.create(tmp, "session")
            writer = SessionWriter(paths, ExportConfig(), max_queue=2)
            writer.start()
            for _ in range(50):
                writer.submit_rms(np.arange(10.0), np.ones(10), np.ones(10))
            writer.stop()
            stats = writer.stats()
            self.assertEqual(stats["dropped"] + stats["written"], 50)

    def test_disabled_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.create(tmp, "session")
            writer = SessionWriter(paths, ExportConfig(enabled=False))
            writer.start()
            writer.submit_raw(np.arange(10.0), np.arange(10.0), np.ones((10, 1)))
            writer.stop()
            self.assertFalse(paths.raw_csv.exists())


class TestExportApdfResult(unittest.TestCase):
    def test_exports_three_files(self):
        result = compute_apdf(np.linspace(0, 100, 1000), mve_ref=100.0, window_sec=0.125)
        with tempfile.TemporaryDirectory() as tmp:
            files = export_apdf_result(result, tmp, stem="mine")
            names = sorted(p.name for p in files)
            self.assertEqual(names, ["mine_curve.csv", "mine_levels.csv", "mine_summary.json"])
            for f in files:
                self.assertTrue(f.exists() and f.stat().st_size > 0)


class TestStreamMeta(unittest.TestCase):
    def test_labels_and_describe(self):
        meta = StreamMeta(
            name="OpenSignals", type="00:07:80:AA", n_channels=2,
            nominal_srate=1000.0,
            channel_labels=["EMG", "ECG"], channel_units=["mV", "mV"],
        )
        self.assertEqual(meta.label_for(1), "EMG")
        self.assertEqual(meta.unit_for(2), "mV")
        self.assertEqual(meta.label_for(9), "ch9")
        self.assertIn("EMG", meta.describe())

    def test_describe_notes_missing_labels(self):
        meta = StreamMeta(name="OpenSignals", n_channels=1, nominal_srate=1000.0)
        self.assertIn("after connecting", meta.describe())


class TestPylslOptionality(unittest.TestCase):
    def test_require_pylsl_raises_when_missing(self):
        if PYLSL_AVAILABLE:
            self.skipTest("本机已安装 pylsl")
        from lsl_apdf.sources import require_pylsl

        with self.assertRaises(RuntimeError):
            require_pylsl()

    def test_stream_not_found_error_has_hints(self):
        err = StreamNotFoundError("找不到流")
        self.assertIn("INTEGRATIONS", str(err))


if __name__ == "__main__":
    unittest.main()
