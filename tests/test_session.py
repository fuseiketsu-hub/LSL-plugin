"""端到端会话测试：标定 → %MVE → APDF → 落盘，并对照解析真值。

这些测试是"插件的验收测试"：合成源的包络是已知的，所以 APDF 的
static/median/peak 有可以对照的真值，而不是只看"跑通了没有"。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.apdf import compute_apdf
from lsl_apdf.calibration import CalibrationPhase
from lsl_apdf.config import AppConfig
from lsl_apdf.offline import analyze_csv, load_recording
from lsl_apdf.session import AcquisitionSession
from lsl_apdf.sources import SyntheticProfile, SyntheticSource


def build_cfg(tmpdir: str, *, srate: float = 500.0) -> AppConfig:
    cfg = AppConfig()
    cfg.export.enabled = True
    cfg.export.root_dir = tmpdir
    cfg.export.flush_interval_sec = 0.01
    cfg.signal.startup_skip_sec = 0.4
    cfg.signal.rms_window_sec = 0.125
    cfg.signal.rms_hop_sec = 0.0625
    cfg.calibration.rest_sec = 2.0
    cfg.calibration.mvc_sec = 2.0
    cfg.calibration.mvc_trials = 2
    cfg.calibration.between_trials_sec = 1.0
    cfg.calibration.plateau_sec = 1.0
    return cfg


#: 轮廓：静息 2s → MVC 2s → 休息 1s → MVC 2s → 任务段（14s 低负荷 + 6s 高负荷）
TASK_PROFILE = SyntheticProfile(
    segments=[
        (2.0, 0.0),
        (2.0, 100.0),
        (1.0, 0.0),
        (2.0, 100.0),
        (14.0, 6.0),
        (6.0, 51.0),
    ],
    noise_floor_pct=1.0,
    mve_unit=1000.0,
    dc_offset=1650.0,
)


class TestSessionEndToEnd(unittest.TestCase):
    def _run(self, tmp: str) -> tuple[AcquisitionSession, dict]:
        cfg = build_cfg(tmp)
        src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
        session = AcquisitionSession(cfg, src)
        session.open()
        session.start_calibration()
        total = int(27.0 * 500)
        while src.n_samples < total:
            session.step(timeout=0.0)
        summary = session.close("unit-test")
        return session, summary

    def test_calibration_then_apdf_matches_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, summary = self._run(tmp)

            # --- 标定 ---
            outcome = session.calibration_outcome
            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertIs(session.calibration_state.phase, CalibrationPhase.DONE)
            self.assertAlmostEqual(outcome.mve_ref, 1000.0, delta=100.0)
            self.assertEqual(len(outcome.trials), 2)
            self.assertGreater(outcome.snr_db, 20.0)

            # --- APDF 方向与量级 ---
            apdf = summary["apdf"]
            self.assertIsNotNone(apdf)
            assert apdf is not None
            static = apdf["static_level_pct"]
            median = apdf["median_level_pct"]
            peak = apdf["peak_level_pct"]

            # 真值：任务段 70% 时间在 ~7 %MVE，30% 时间在 ~52 %MVE
            self.assertGreater(static, 4.0)
            self.assertLess(static, 10.0)
            self.assertGreater(median, 4.0)
            self.assertLess(median, 10.0)
            self.assertGreater(peak, 40.0)
            self.assertLess(peak, 60.0)
            # 方向性：峰值必须远大于静态（防止 static/peak 对调）
            self.assertGreater(peak, static * 3.0)
            self.assertLessEqual(static, median)
            self.assertLessEqual(median, peak)
            self.assertAlmostEqual(apdf["mve_ref"], outcome.mve_ref, places=6)

    def test_exported_files_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, summary = self._run(tmp)
            files = summary["files"]
            raw = Path(files["raw_csv"])
            rms_csv = Path(files["rms_csv"])
            curve = Path(files["apdf_curve_csv"])
            pct = Path(files["percentiles_csv"])
            config_json = Path(files["config_json"])
            cal_json = Path(files["calibration_json"])
            for path in (raw, rms_csv, curve, pct, config_json, cal_json):
                self.assertTrue(path.exists(), f"{path} 应存在")
                self.assertGreater(path.stat().st_size, 0)

            # raw.csv：27 s @ 500 Hz
            recording = load_recording(raw)
            self.assertEqual(recording.times.size, int(27.0 * 500))
            self.assertIn("ch1", recording.channels)
            self.assertEqual(recording.local_times.size, recording.times.size)

            # rms.csv 的负荷列应与 summary 里的 mve_ref 自洽：
            # level_pct = rms / mve * 100  →  rms / level_pct = mve / 100
            data = np.genfromtxt(rms_csv, delimiter=",", names=True)
            ratio = data["rms"] / data["level_pct_mve"]
            finite = np.isfinite(ratio) & (data["level_pct_mve"] != 0)
            self.assertTrue(finite.any())
            # CSV 只写 6 位有效数字，比值会有 ~1e-6 的量化误差
            np.testing.assert_allclose(
                ratio[finite] * 100.0, summary["mve_ref"], rtol=1e-4
            )

            # config.json 记录了本次运行的全部参数
            cfg_json = json.loads(config_json.read_text(encoding="utf-8"))
            self.assertIn("_runtime", cfg_json)
            self.assertAlmostEqual(
                cfg_json["signal"]["rms_window_sec"], 0.125, places=9
            )

            # calibration.json
            cal = json.loads(cal_json.read_text(encoding="utf-8"))
            self.assertAlmostEqual(cal["mve_ref"], summary["mve_ref"], places=6)

    def test_offline_recompute_matches_online(self):
        """离线重算（零相位）应与在线结果接近（统计量层面）。"""
        with tempfile.TemporaryDirectory() as tmp:
            session, summary = self._run(tmp)
            mve = summary["mve_ref"]
            online = summary["apdf"]

            cfg = build_cfg(tmp)
            result, _, _, _ = analyze_csv(
                summary["files"]["raw_csv"], mve_ref=mve, cfg=cfg, zero_phase=True
            )
            # 离线段包含标定段（MVC 用力），所以峰值会更高；
            # 这里只校验量级与前 60 s 之外的一致性由另有测试覆盖。
            self.assertGreater(result.n_windows, 0)
            self.assertGreater(result.peak_level, online["peak_level_pct"] * 0.8)

    def test_causal_offline_matches_online_closely(self):
        """因果滤波的离线段（排除标定段）应与在线结果高度一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            session, summary = self._run(tmp)
            mve = summary["mve_ref"]
            cfg = build_cfg(tmp)
            result, _, _, recording = analyze_csv(
                summary["files"]["raw_csv"], mve_ref=mve, cfg=cfg, zero_phase=False
            )
            # 只取任务段（标定在第 7 秒结束）
            task = recording.times >= recording.times[0] + 7.5
            from lsl_apdf.dsp import RmsExtractor, design_filter_chain, filter_offline

            cfg.signal.adc_scale = 1.0
            x = recording.channels["ch1"][task]
            fs = recording.srate
            d = design_filter_chain(fs, 20.0, 450.0, 50.0, 30.0, 4)
            y = filter_offline(x, fs, d, zero_phase=False)
            ex = RmsExtractor(fs, 0.125, 0.0625)
            rms_v, _ = ex.push(y, recording.times[task])
            off = compute_apdf(rms_v, mve, cfg.apdf, window_sec=0.125, hop_sec=0.0625)
            self.assertAlmostEqual(
                off.peak_level, summary["apdf"]["peak_level_pct"], delta=3.0
            )
            self.assertAlmostEqual(
                off.static_level, summary["apdf"]["static_level_pct"], delta=2.0
            )


class TestSessionBehaviour(unittest.TestCase):
    def test_mve_ref_in_config_skips_calibration(self):
        """提供 mve_ref 时应直接进入 APDF 统计（不做标定，全部数据都算）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.mve_ref = 1000.0
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            while src.n_samples < int(10.0 * 500):
                session.step(timeout=0.0)
            snap = session.snapshot()
            self.assertIsNotNone(snap.apdf)
            assert snap.apdf is not None
            self.assertGreater(snap.apdf.n_windows, 0)
            self.assertAlmostEqual(snap.mve_ref or 0, 1000.0)
            # 没有任何标定阶段被启动
            self.assertIs(session.calibration_state.phase, CalibrationPhase.IDLE)
            level_before = snap.latest_level_pct

            # 手动把 MVE 参考值改成 2 倍 → 负荷百分比应减半，且重算曲线
            session.set_mve_ref(2000.0)
            self.assertAlmostEqual(session.cfg.mve_ref or 0, 2000.0)
            snap2 = session.snapshot()
            self.assertAlmostEqual(snap2.latest_level_pct, level_before / 2, delta=1e-6)
            session.close("test")

    def test_apdf_callback_is_invoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.mve_ref = 1000.0
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            seen: list[float] = []
            session.add_apdf_callback(lambda r: seen.append(r.median_level))
            while src.n_samples < int(12.0 * 500):
                session.step(timeout=0.0)
            self.assertTrue(seen)
            self.assertTrue(all(np.isfinite(v) for v in seen))
            session.close("test")

    def test_calibration_excluded_from_apdf_by_default(self):
        """默认标定段（MVC 用力）不得进入任务 APDF。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            session.start_calibration()
            # 只跑到标定结束（7 s），此时不应有任务数据进入累积
            while src.n_samples < int(7.5 * 500):
                session.step(timeout=0.0)
            self.assertEqual(session.snapshot().stats.n_apdf_samples, 0)
            session.close("test")

    def test_include_calibration_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.include_calibration_in_apdf = True
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            session.start_calibration()
            while src.n_samples < int(7.5 * 500):
                session.step(timeout=0.0)
            self.assertGreater(session.snapshot().stats.n_apdf_samples, 0)
            session.close("test")

    def test_reset_accumulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.mve_ref = 1000.0
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            while src.n_samples < int(12.0 * 500):
                session.step(timeout=0.0)
            self.assertGreater(session.snapshot().stats.n_apdf_samples, 0)
            session.reset_accumulator()
            self.assertEqual(session.snapshot().stats.n_apdf_samples, 0)
            self.assertIsNone(session.apdf)
            session.close("test")

    def test_manual_mve_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src)
            session.open()
            for bad in (0.0, -1.0, float("nan")):
                with self.assertRaises(ValueError):
                    session.set_mve_ref(bad)
            session.close("test")

    def test_no_writer_mode(self):
        cfg = build_cfg(".")
        cfg.export.enabled = False
        src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
        session = AcquisitionSession(cfg, src)
        session.open()
        self.assertIsNone(session.paths)
        session.step(timeout=0.0)
        summary = session.close("test")
        self.assertIsNone(summary["files"]["raw_csv"])

    def test_stream_ends_gracefully(self):
        """数据源耗尽（返回 None）时 step 应返回 False 而不是抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.export.enabled = False
            prof = SyntheticProfile(segments=[(0.5, 20.0)], noise_floor_pct=1.0)
            src = SyntheticSource(profile=prof, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src, create_writer=False)
            session.open()
            session.step(timeout=0.0)

            class _Empty(SyntheticSource):
                def read_chunk(self, timeout: float = 0.05):
                    return None

            session.source = _Empty(profile=prof, srate=500.0, realtime=False)
            self.assertFalse(session.step(timeout=0.0))
            session.close("test")

    def test_channel_out_of_range_rejected(self):
        cfg = build_cfg(".")
        cfg.export.enabled = False
        cfg.signal.channel = 4
        src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False,
                              n_channels=1)
        session = AcquisitionSession(cfg, src, create_writer=False)
        with self.assertRaises(ValueError):
            session.open()

    def test_snapshot_arrays_are_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_cfg(tmp)
            cfg.export.enabled = False
            cfg.mve_ref = 1000.0
            src = SyntheticSource(profile=TASK_PROFILE, srate=500.0, realtime=False)
            session = AcquisitionSession(cfg, src, create_writer=False)
            session.open()
            for _ in range(20):
                session.step(timeout=0.0)
            snap = session.snapshot()
            n_raw = snap.raw_y.size
            self.assertGreater(n_raw, 0)
            snap.raw_y[:] = 0.0  # 修改快照不应影响会话内部状态
            snap2 = session.snapshot()
            self.assertNotAlmostEqual(float(np.max(np.abs(snap2.raw_y))), 0.0)
            session.close("test")


if __name__ == "__main__":
    unittest.main()
