"""标定状态机测试：静息基线、多试次 MVC、平台选取、警告与手动覆盖。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.calibration import (
    CalibrationOutcome,
    CalibrationPhase,
    MvcCalibrator,
    TrialRecord,
    calibrate_from_signal,
)
from lsl_apdf.config import CalibrationConfig


def make_times(n: int, dt: float = 0.0625) -> np.ndarray:
    return np.arange(n, dtype=float) * dt


def build_calibration_series(
    rest_sec=2.0, mvc_sec=2.0, trials=3, between_sec=1.0, mvc=1000.0, noise=10.0,
    dt=0.0625, seed=0, tail_sec=1.5, trial_levels: list[float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """构造"静息 + 若干 MVC + 间隔"的幅值序列（含随机波动）。

    ``tail_sec``：末尾补一段静息数据。真实采集不会在最后一次用力结束的瞬间
    停止，补一小段尾巴才能让最后一个 MVC 阶段正常判定结束。
    ``trial_levels``：可为每个试次指定不同的用力水平（用于测试一致性警告）。
    """
    rng = np.random.default_rng(seed)
    levels = list(trial_levels) if trial_levels else [mvc] * trials
    values: list[float] = []
    for _ in range(int(rest_sec / dt)):
        values.append(abs(rng.normal(noise, noise * 0.2)))
    for i in range(trials):
        level = levels[i] if i < len(levels) else mvc
        for _ in range(int(mvc_sec / dt)):
            values.append(abs(rng.normal(level, level * 0.03)))
        if i != trials - 1:
            for _ in range(int(between_sec / dt)):
                values.append(abs(rng.normal(noise, noise * 0.2)))
    for _ in range(int(tail_sec / dt)):
        values.append(abs(rng.normal(noise, noise * 0.2)))
    arr = np.asarray(values)
    return arr, make_times(arr.size, dt)


class TestMvcCalibrator(unittest.TestCase):
    def setUp(self):
        self.cfg = CalibrationConfig(
            rest_sec=2.0, mvc_sec=2.0, mvc_trials=3, between_trials_sec=1.0,
            plateau_sec=1.0,
        )

    def test_full_flow_recovers_mve(self):
        values, times = build_calibration_series(noise=10.0, mvc=1000.0)
        cal, outcome = calibrate_from_signal(values, times, self.cfg)
        self.assertIs(cal.phase, CalibrationPhase.DONE)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertAlmostEqual(outcome.mve_ref, 1000.0, delta=60.0)
        self.assertEqual(len(outcome.trials), 3)
        self.assertAlmostEqual(outcome.baseline_rms, 10.0, delta=3.0)
        self.assertGreater(outcome.snr_db, 30.0)
        self.assertLess(outcome.trial_cv, 0.15)

    def test_phase_sequence(self):
        cal = MvcCalibrator(self.cfg)
        cal.start()
        self.assertIs(cal.phase, CalibrationPhase.REST)
        seen = [cal.phase]
        values, times = build_calibration_series()
        for v, t in zip(values, times):
            cal.push_amplitude(float(v), float(t))
            if cal.phase is not seen[-1]:
                seen.append(cal.phase)
        self.assertEqual(
            seen,
            [
                CalibrationPhase.REST,
                CalibrationPhase.MVC,
                CalibrationPhase.BETWEEN,
                CalibrationPhase.MVC,
                CalibrationPhase.BETWEEN,
                CalibrationPhase.MVC,
                CalibrationPhase.DONE,
            ],
        )

    def test_plateau_selection_prefers_strongest_window(self):
        """试次内应取最强平台，而不是整体均值（避免起落拖低参考值）。"""
        cfg = CalibrationConfig(rest_sec=1.0, mvc_sec=4.0, mvc_trials=1,
                                plateau_sec=1.0, between_trials_sec=0.5)
        values, times = build_calibration_series(
            rest_sec=1.0, mvc_sec=4.0, trials=1, mvc=1000.0, noise=10.0
        )
        # 让试次后半段明显更强
        half = int(times.size * 0.6)
        values[half:] *= 1.5
        cal, outcome = calibrate_from_signal(values, times, cfg)
        self.assertIs(cal.phase, CalibrationPhase.DONE)
        assert outcome is not None
        trial = outcome.trials[0]
        self.assertGreater(trial.plateau_rms, trial.mean_rms)
        self.assertGreater(trial.peak_rms, trial.plateau_rms)

    def test_average_best_trials_option(self):
        cfg = CalibrationConfig(rest_sec=1.0, mvc_sec=1.0, mvc_trials=3,
                                between_trials_sec=0.5, plateau_sec=0.5,
                                average_best_trials=True, best_trial_ratio=0.5)
        dt = 0.0625
        values, times = build_calibration_series(
            rest_sec=1.0, mvc_sec=1.0, trials=3, between_sec=0.5, mvc=1000.0,
            noise=10.0, dt=dt,
        )
        cal, outcome = calibrate_from_signal(values, times, cfg)
        assert outcome is not None
        self.assertTrue(any("averaged" in w for w in outcome.warnings))

    def test_low_snr_triggers_warning(self):
        values, times = build_calibration_series(noise=900.0, mvc=1000.0)
        _, outcome = calibrate_from_signal(values, times, self.cfg)
        assert outcome is not None
        self.assertTrue(any("信噪比" in w or "dB" in w for w in outcome.warnings))

    def test_unstable_trials_trigger_cv_warning(self):
        """三次用力差异过大时应提示重新标定（默认 cv_warn=15%）。"""
        cfg = CalibrationConfig(rest_sec=1.0, mvc_sec=1.0, mvc_trials=3,
                                between_trials_sec=0.5, plateau_sec=0.5)
        values, times = build_calibration_series(
            rest_sec=1.0, mvc_sec=1.0, trials=3, between_sec=0.5, mvc=1000.0,
            noise=10.0, trial_levels=[1000.0, 550.0, 1000.0],
        )
        cal, outcome = calibrate_from_signal(values, times, cfg)
        self.assertIs(cal.phase, CalibrationPhase.DONE)
        assert outcome is not None
        self.assertIsNotNone(outcome.trial_cv)
        assert outcome.trial_cv is not None
        self.assertGreater(outcome.trial_cv, cfg.cv_warn)
        self.assertTrue(any("CV" in w for w in outcome.warnings))

    def test_skip_phase_advances_immediately(self):
        cal = MvcCalibrator(self.cfg)
        cal.start()
        cal.push_amplitude(10.0, 0.0)
        cal.skip_phase()  # 结束静息
        self.assertIs(cal.phase, CalibrationPhase.MVC)
        cal.skip_phase()  # 结束试次 1
        self.assertIs(cal.phase, CalibrationPhase.BETWEEN)
        cal.skip_phase()  # 立刻开始试次 2
        self.assertIs(cal.phase, CalibrationPhase.MVC)

    def test_abort(self):
        cal = MvcCalibrator(self.cfg)
        cal.start()
        cal.push_amplitude(10.0, 0.0)
        cal.abort()
        self.assertIs(cal.phase, CalibrationPhase.ABORTED)
        cal.push_amplitude(1000.0, 1.0)  # 中止后不应再接收
        self.assertIsNone(cal.mve_ref)

    def test_state_snapshot_fields(self):
        cal = MvcCalibrator(self.cfg)
        cal.start()
        for i in range(20):  # 1.25 s，仍在静息段内
            cal.push_amplitude(10.0, i * 0.0625)
        st = cal.state()
        self.assertIs(st.phase, CalibrationPhase.REST)
        self.assertGreater(st.elapsed_in_phase, 0.0)
        self.assertAlmostEqual(st.duration_in_phase, 2.0)
        self.assertGreater(st.overall_progress, 0.0)
        self.assertLessEqual(st.overall_progress, 1.0)
        self.assertIn("relaxed", st.message)

    def test_no_valid_trial(self):
        cfg = CalibrationConfig(rest_sec=0.5, mvc_sec=0.5, mvc_trials=1)
        cal = MvcCalibrator(cfg)
        cal.start()
        cal.push_amplitude(1.0, 0.0)
        cal.skip_phase()
        cal.skip_phase()
        assert cal.outcome is not None
        self.assertTrue(np.isnan(cal.outcome.mve_ref))
        self.assertTrue(any("No valid MVC" in w for w in cal.outcome.warnings))

    def test_non_finite_values_ignored(self):
        cal = MvcCalibrator(self.cfg)
        cal.start()
        cal.push_amplitude(float("nan"), 0.0)
        cal.push_amplitude(float("inf"), 0.1)
        st = cal.state()
        self.assertIs(st.phase, CalibrationPhase.REST)


class TestOutcomeIO(unittest.TestCase):
    def test_save_and_load(self):
        outcome = CalibrationOutcome(
            mve_ref=123.45,
            unit_label="mV",
            baseline_rms=1.2,
            snr_db=40.0,
            trial_cv=0.03,
            trials=[TrialRecord(1, 123.0, 130.0, 120.0, 80, 5.0)],
            warnings=["测试警告"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = outcome.save(Path(tmp) / "cal.json")
            self.assertTrue(path.exists())
            back = CalibrationOutcome.load(path)
        self.assertAlmostEqual(back.mve_ref, 123.45)
        self.assertEqual(back.unit_label, "mV")
        self.assertEqual(len(back.trials), 1)
        self.assertEqual(back.trials[0].plateau_rms, 123.0)
        self.assertIn("123", back.summary_line())

    def test_json_is_serializable(self):
        outcome = CalibrationOutcome(mve_ref=1.0, trials=[TrialRecord(1, 1, 1, 1, 1, 1.0)])
        text = json.dumps(outcome.to_dict())
        self.assertIn("mve_ref", text)


class TestPlateauWithDifferentRates(unittest.TestCase):
    """平台长度按实际 RMS 速率推算，而不是写死采样率。"""

    def test_plateau_count_scales_with_rate(self):
        for dt in (0.0625, 0.125, 0.25):
            cfg = CalibrationConfig(rest_sec=1.0, mvc_sec=2.0, mvc_trials=1,
                                    plateau_sec=1.0)
            values, times = build_calibration_series(
                rest_sec=1.0, mvc_sec=2.0, trials=1, mvc=500.0, noise=5.0, dt=dt
            )
            cal, outcome = calibrate_from_signal(values, times, cfg)
            assert outcome is not None
            self.assertAlmostEqual(outcome.mve_ref, 500.0, delta=50.0)


if __name__ == "__main__":
    unittest.main()
