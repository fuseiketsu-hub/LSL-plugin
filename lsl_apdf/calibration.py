"""%MVE 归一化所需的 MVC 标定状态机。

为什么必须标定
--------------
APDF 的 Y 轴是 %MVE（相对最大随意收缩时的肌电幅值）。没有参考值，
不同人、不同电极位置、不同增益下的曲线无法互相比较 —— 因此本插件按需求
**强制**先走标定流程（或显式提供一个已知的 ``mve_ref``）。

流程
----
::

    REST(静息基线)  →  MVC 试次 1  →  BETWEEN(休息)  →  MVC 试次 2  →  ...  →  DONE

* **REST**：静息 ``rest_sec`` 秒 → 基线噪声（中位数，稳健）+ SNR 检查。
* **MVC**：尽力收缩保持 ``mvc_sec`` 秒 → 取该试次内长度为 ``plateau_sec`` 的
  **连续最强平台均值**（比取瞬时峰值稳健，不易被一个电极抖动尖峰污染）。
* **BETWEEN**：休息 ``between_trials_sec`` 秒，避免叠加疲劳污染参考值。
* **DONE**：取最强试次（或最强的若干试次均值）作为 ``mve_ref``，
  并给出 SNR / 试次一致性（CV）警告。

时间推进用的是**数据的 LSL 时间戳**而不是墙钟：流一停，标定进度就停，
不会出现"人还没使劲、阶段已经跑完"的错位（说明文档 §4 的时间同步要求）。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from .config import CalibrationConfig

__all__ = [
    "CalibrationPhase",
    "TrialRecord",
    "CalibrationState",
    "CalibrationOutcome",
    "MvcCalibrator",
]


class CalibrationPhase(str, Enum):
    IDLE = "idle"
    REST = "rest"
    MVC = "mvc"
    BETWEEN = "between"
    DONE = "done"
    ABORTED = "aborted"

    @property
    def label(self) -> str:
        return {
            "idle": "Not started",
            "rest": "Rest baseline",
            "mvc": "Maximal voluntary contraction (MVC)",
            "between": "Rest between trials",
            "done": "Calibration complete",
            "aborted": "Aborted",
        }[self.value]

    @property
    def active(self) -> bool:
        return self in (CalibrationPhase.REST, CalibrationPhase.MVC, CalibrationPhase.BETWEEN)


@dataclass
class TrialRecord:
    """单次 MVC 试次的结果。"""

    index: int
    plateau_rms: float
    peak_rms: float
    mean_rms: float
    n_samples: int
    duration_sec: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalibrationState:
    """给 GUI / CLI 展示的标定状态快照。"""

    phase: CalibrationPhase = CalibrationPhase.IDLE
    elapsed_in_phase: float = 0.0
    duration_in_phase: float = 0.0
    overall_progress: float = 0.0
    trial_index: int = 0
    n_trials: int = 0
    baseline_rms: float | None = None
    mve_ref: float | None = None
    trials: list[TrialRecord] = field(default_factory=list)
    snr_db: float | None = None
    trial_cv: float | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def phase_label(self) -> str:
        return self.phase.label

    @property
    def remaining_in_phase(self) -> float:
        return max(0.0, self.duration_in_phase - self.elapsed_in_phase)

    @property
    def is_done(self) -> bool:
        return self.phase is CalibrationPhase.DONE


@dataclass
class CalibrationOutcome:
    """标定产物：可写盘、可加载、可复用到后续会话。"""

    mve_ref: float
    unit_label: str = "a.u."
    baseline_rms: float | None = None
    rest_std: float | None = None
    snr_db: float | None = None
    trial_cv: float | None = None
    trials: list[TrialRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    finished_at: str = ""
    source: str = "calibration"  # calibration | manual

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trials"] = [t.to_dict() if isinstance(t, TrialRecord) else t for t in self.trials]
        return data

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationOutcome":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        trials = [TrialRecord(**t) for t in data.get("trials", [])]
        data["trials"] = trials
        return cls(**data)

    def summary_line(self) -> str:
        unit = self.unit_label
        bits = [f"MVE = {self.mve_ref:.4g} {unit}"]
        if self.baseline_rms is not None:
            bits.append(f"rest baseline = {self.baseline_rms:.4g} {unit}")
        if self.snr_db is not None:
            bits.append(f"SNR = {self.snr_db:.1f} dB")
        if self.trial_cv is not None:
            bits.append(f"trial CV = {self.trial_cv * 100:.1f}%")
        return " | ".join(bits)


class MvcCalibrator:
    """消费 RMS 幅值序列、按 :class:`CalibrationConfig` 推进标定阶段。"""

    def __init__(self, cfg: CalibrationConfig | None = None, unit_label: str = "a.u."):
        self.cfg = cfg or CalibrationConfig()
        self.unit_label = unit_label
        self._phase = CalibrationPhase.IDLE
        self._phase_start_t: float | None = None
        self._last_t: float | None = None
        self._rest_values: list[float] = []
        self._rest_times: list[float] = []
        self._trial_values: list[float] = []
        self._trial_times: list[float] = []
        self._trials: list[TrialRecord] = []
        self._trial_index = 0
        self._warnings: list[str] = []
        self._outcome: CalibrationOutcome | None = None
        self._baseline: float | None = None
        self._rest_std: float | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """重置并进入静息阶段。"""
        self._phase = CalibrationPhase.REST
        self._phase_start_t = None
        self._last_t = None
        self._rest_values = []
        self._rest_times = []
        self._trial_values = []
        self._trial_times = []
        self._trials = []
        self._trial_index = 0
        self._warnings = []
        self._outcome = None
        self._baseline = None
        self._rest_std = None

    def abort(self) -> None:
        self._phase = CalibrationPhase.ABORTED

    def reset(self) -> None:
        self._phase = CalibrationPhase.IDLE
        self._phase_start_t = None
        self._last_t = None
        self._rest_values = []
        self._rest_times = []
        self._trial_values = []
        self._trial_times = []
        self._trials = []
        self._trial_index = 0
        self._warnings = []
        self._outcome = None
        self._baseline = None
        self._rest_std = None

    # ------------------------------------------------------------------ #
    # 数据输入
    # ------------------------------------------------------------------ #
    def push_amplitude(self, value: float, t: float) -> None:
        """送入一个 RMS 幅值及其时间戳（LSL 时间）。"""
        if not self._phase.active:
            return
        if value is None or not math.isfinite(float(value)):
            return
        value = abs(float(value))
        t = float(t)

        if self._phase_start_t is None:
            self._phase_start_t = t
        self._last_t = t

        if self._phase is CalibrationPhase.REST:
            self._rest_values.append(value)
            self._rest_times.append(t)
            if t - self._phase_start_t >= self.cfg.rest_sec:
                self._finish_rest()
        elif self._phase is CalibrationPhase.MVC:
            self._trial_values.append(value)
            self._trial_times.append(t)
            if t - self._phase_start_t >= self.cfg.mvc_sec:
                self._finish_trial()
        elif self._phase is CalibrationPhase.BETWEEN:
            if t - self._phase_start_t >= self.cfg.between_trials_sec:
                self._begin_trial()

        # 阶段刚切换时用**当前样本时间**作为新阶段起点。否则新阶段要等到下一个
        # 样本才起算，每个阶段会白白吃掉一个样本的时间（在低 RMS 速率下会让
        # 整个标定比预期慢一拍，末尾试次还可能永远等不到结束）。
        if self._phase.active and self._phase_start_t is None:
            self._phase_start_t = t

    def skip_phase(self) -> None:
        """手动立即结束当前阶段（GUI 的"下一阶段"按钮 / 提前完成用力）。"""
        if self._phase is CalibrationPhase.REST:
            self._finish_rest()
        elif self._phase is CalibrationPhase.MVC:
            self._finish_trial()
        elif self._phase is CalibrationPhase.BETWEEN:
            self._begin_trial()

    # ------------------------------------------------------------------ #
    # 阶段切换
    # ------------------------------------------------------------------ #
    def _finish_rest(self) -> None:
        values = np.asarray(self._rest_values, dtype=float)
        if values.size == 0:
            self._warnings.append("No data received during rest; baseline not estimated")
        else:
            stat = self.cfg.baseline_statistic
            self._baseline = (
                float(np.median(values)) if stat == "median" else float(np.mean(values))
            )
            self._rest_std = float(np.std(values))
        if self._trial_index == 0:
            self._begin_trial()
        else:  # pragma: no cover - 正常流程不会走到
            self._finish_all()

    def _begin_trial(self) -> None:
        self._trial_index += 1
        self._phase = CalibrationPhase.MVC
        self._phase_start_t = None
        self._trial_values = []
        self._trial_times = []

    def _finish_trial(self) -> None:
        values = np.asarray(self._trial_values, dtype=float)
        times = np.asarray(self._trial_times, dtype=float)
        if values.size == 0:
            self._warnings.append(f"MVC trial {self._trial_index} received no data; skipped")
            record = TrialRecord(
                index=self._trial_index,
                plateau_rms=float("nan"),
                peak_rms=float("nan"),
                mean_rms=float("nan"),
                n_samples=0,
                duration_sec=0.0,
            )
        else:
            n_plateau = self._plateau_sample_count(times)
            plateau = _best_plateau_mean(values, n_plateau)
            duration = float(times[-1] - times[0]) if times.size > 1 else 0.0
            record = TrialRecord(
                index=self._trial_index,
                plateau_rms=float(plateau),
                peak_rms=float(np.max(values)),
                mean_rms=float(np.mean(values)),
                n_samples=int(values.size),
                duration_sec=duration,
            )
            if duration < self.cfg.mvc_sec * 0.8:
                self._warnings.append(
                    f"MVC trial {record.index} lasted only {duration:.2f} s"
                    f" (target {self.cfg.mvc_sec:.1f} s); result may be unstable"
                )
        self._trials.append(record)

        if self._trial_index >= self.cfg.mvc_trials:
            self._finish_all()
        else:
            self._phase = CalibrationPhase.BETWEEN
            self._phase_start_t = None

    def _finish_all(self) -> None:
        self._phase = CalibrationPhase.DONE
        self._phase_start_t = None
        self._outcome = self._build_outcome()

    # ------------------------------------------------------------------ #
    # 计算
    # ------------------------------------------------------------------ #
    def _plateau_sample_count(self, times: np.ndarray) -> int:
        """按本试次的实际采样间隔推算平台包含多少个 RMS 样本。

        不写死 RMS 速率：从时间戳的中位间隔反推（说明文档 §6 的
        "勿写死采样率"要求同样适用于派生速率）。
        """
        if times.size < 2:
            return 1
        dt = float(np.median(np.diff(times)))
        if dt <= 0:
            return 1
        n = int(round(self.cfg.plateau_sec / dt))
        return int(max(1, min(n, times.size)))

    def _build_outcome(self) -> CalibrationOutcome:
        valid = [t for t in self._trials if math.isfinite(t.plateau_rms)]
        warnings = list(self._warnings)
        baseline = self._baseline
        rest_std = self._rest_std

        if not valid:
            warnings.append("No valid MVC trials; cannot determine the MVE reference")
            return CalibrationOutcome(
                mve_ref=float("nan"),
                unit_label=self.unit_label,
                baseline_rms=baseline,
                rest_std=rest_std,
                trials=self._trials,
                warnings=warnings,
                config=asdict(self.cfg),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

        plateaus = np.asarray([t.plateau_rms for t in valid], dtype=float)
        best = float(np.max(plateaus))
        if self.cfg.average_best_trials:
            keep = plateaus >= best * self.cfg.best_trial_ratio
            if keep.sum() > 1:
                mve = float(np.mean(plateaus[keep]))
                warnings.append(
                    f"MVE averaged over {int(keep.sum())} trials within "
                    f"{self.cfg.best_trial_ratio * 100:.0f}% of the best"
                )
            else:
                mve = best
        else:
            mve = best

        cv = float(np.std(plateaus) / np.mean(plateaus)) if plateaus.size > 1 else 0.0
        snr_db: float | None = None
        if baseline is not None and baseline > 0 and mve > 0:
            snr_db = float(20.0 * np.log10(mve / baseline))
            if snr_db < self.cfg.snr_warn_db:
                warnings.append(
                    f"MVE/rest baseline is only {snr_db:.1f} dB (threshold "
                    f"{self.cfg.snr_warn_db:.1f} dB): check electrode contact or amplifier settings"
                )
        elif baseline == 0:
            warnings.append("Rest amplitude is 0; cannot evaluate SNR")
        if cv > self.cfg.cv_warn:
            warnings.append(
                f"Between-trial CV is {cv * 100:.1f}% (threshold {self.cfg.cv_warn * 100:.0f}%): "
                "effort is unstable; consider recalibrating"
            )
        if plateaus.size < self.cfg.mvc_trials:
            warnings.append(
                f"Only {plateaus.size}/{self.cfg.mvc_trials} valid trials completed"
            )

        return CalibrationOutcome(
            mve_ref=mve,
            unit_label=self.unit_label,
            baseline_rms=baseline,
            rest_std=rest_std,
            snr_db=snr_db,
            trial_cv=cv if plateaus.size > 1 else None,
            trials=self._trials,
            warnings=warnings,
            config=asdict(self.cfg),
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def phase(self) -> CalibrationPhase:
        return self._phase

    @property
    def outcome(self) -> CalibrationOutcome | None:
        return self._outcome

    @property
    def mve_ref(self) -> float | None:
        if self._outcome and math.isfinite(self._outcome.mve_ref):
            return self._outcome.mve_ref
        return None

    def state(self) -> CalibrationState:
        """构造当前状态快照（线程安全由调用方的锁保证）。"""
        st = CalibrationState(
            phase=self._phase,
            trial_index=self._trial_index,
            n_trials=self.cfg.mvc_trials,
            baseline_rms=self._baseline,
            mve_ref=self.mve_ref,
            trials=list(self._trials),
            warnings=list(self._warnings),
        )
        if self._outcome:
            st.snr_db = self._outcome.snr_db
            st.trial_cv = self._outcome.trial_cv
            st.warnings = list(self._outcome.warnings)

        elapsed = 0.0
        if self._phase_start_t is not None and self._last_t is not None:
            elapsed = max(0.0, self._last_t - self._phase_start_t)
        st.elapsed_in_phase = elapsed
        if self._phase is CalibrationPhase.REST:
            st.duration_in_phase = self.cfg.rest_sec
            st.message = f"Stay relaxed, do not contract ({self.cfg.rest_sec:g} s)"
        elif self._phase is CalibrationPhase.MVC:
            st.duration_in_phase = self.cfg.mvc_sec
            st.message = (
                f"MVC trial {self._trial_index}/{self.cfg.mvc_trials}: contract as hard "
                f"as you can for {self.cfg.mvc_sec:g} s"
            )
        elif self._phase is CalibrationPhase.BETWEEN:
            st.duration_in_phase = self.cfg.between_trials_sec
            st.message = (
                f"Rest {self.cfg.between_trials_sec:g} s; prepare for trial "
                f"{self._trial_index + 1}/{self.cfg.mvc_trials}"
            )
        elif self._phase is CalibrationPhase.DONE and self._outcome:
            st.duration_in_phase = 0.0
            st.message = self._outcome.summary_line()
        elif self._phase is CalibrationPhase.IDLE:
            st.message = "Calibration not started"
        elif self._phase is CalibrationPhase.ABORTED:
            st.message = "Calibration aborted"
        st.overall_progress = self._overall_progress(elapsed)
        return st

    def _overall_progress(self, elapsed: float) -> float:
        total = max(self.cfg.total_duration(), 1e-9)
        done = 0.0
        if self._phase is CalibrationPhase.REST:
            done = elapsed
        elif self._phase is CalibrationPhase.MVC:
            done = self.cfg.rest_sec + (self._trial_index - 1) * (
                self.cfg.mvc_sec + self.cfg.between_trials_sec
            ) + elapsed
        elif self._phase is CalibrationPhase.BETWEEN:
            done = (
                self.cfg.rest_sec
                + self._trial_index * self.cfg.mvc_sec
                + (self._trial_index - 1) * self.cfg.between_trials_sec
                + elapsed
            )
        elif self._phase in (CalibrationPhase.DONE, CalibrationPhase.ABORTED):
            done = total
        return float(min(1.0, max(0.0, done / total)))


def _best_plateau_mean(values: np.ndarray, n: int) -> float:
    """返回长度 n 的连续窗口均值中的最大值（n >= 样本数时退化为整体均值）。"""
    values = np.asarray(values, dtype=float).reshape(-1)
    if values.size == 0:
        return float("nan")
    n = int(max(1, min(n, values.size)))
    if n == values.size:
        return float(np.mean(values))
    kernel = np.ones(n, dtype=float) / n
    means = np.convolve(values, kernel, mode="valid")
    return float(np.max(means))


def calibrate_from_signal(
    signal_values: np.ndarray,
    times: np.ndarray,
    cfg: CalibrationConfig | None = None,
    unit_label: str = "a.u.",
) -> tuple[MvcCalibrator, CalibrationOutcome | None]:
    """离线标定：把一整段"静息 + 各次 MVC"的幅值序列灌进状态机。

    用于（a）测试；（b)事后对已录制的标定段重算 MVE。
    """
    cal = MvcCalibrator(cfg, unit_label=unit_label)
    cal.start()
    for value, t in zip(np.asarray(signal_values, dtype=float), np.asarray(times, dtype=float)):
        cal.push_amplitude(float(value), float(t))
    return cal, cal.outcome
