# -*- coding: utf-8 -*-
"""
emg_server.py — EMG 实时输入接口 + 切换策略（文档 §4.2、§5.3）
==============================================================
1) EMGTCPServer：自建 TCP 服务器，接收已处理好的 %MVC 数据（p50 由本模块
   滚动窗口再平滑一次），协议：
     - text（默认）：每行 JSON，如 {"EDC":11.2,"ECU":12.5,"FDS":13.1,"UT":6.0}
                     或键值对： EDC 11.2 ECU 12.5 FDS 13.1 UT 6.0
                     或逗号键值对： EDC=11.2,ECU=12.5
     - opensignals（实验性）：OpenSignals TCP 输出帧（4 字节序号 + 每通道
       2 字节大端 ADC），用 config 中 adc_to_mvc 线性变换为 %MVC。
       帧格式随版本有差异，正式实验前请用本机版本核对（见 README）。

2) 切换策略（两者皆带 hold_sec 持续确认 + min_dwell_sec 最小切换间隔 +
   迟滞 hysteresis_mvc）：
     - ThresholdPolicy（阈值法）：ECU≥16%MVC（或 UT 超过阈值）→ 升到 1200；
       FDS≥20%MVC → 降到 800。阈值、持续时长可配置。
     - CrossoverPolicy（交叉点法，模拟版）：把当前实测 %MVC 与“另一个 DPI
       档位的参考 %MVC”（需基准实验校准）比较，若对方档位在监控肌肉上低
       margin_mvc 以上 → 切换。默认 reference 为占位值，正式实验前必须
       用基准实验数据替换（文档 §5.3-1）。
     - BalancePolicy（负荷交替法，v2 新增）：比较**两块肌肉各自的高暴露区域**
       （> high_thr %MVC 的面积，即强度×持续时间），谁相对自己的基线更严重，
       就把 DPI 往“卸掉它”的方向切。**切换次数不预设**，完全由两块肌肉的
       高暴露区域互相交替（Δ 变号）的结果决定 —— 没有“每 N 分钟切一次”的
       定时器。方向映射保证是负反馈，于是负荷在两块肌肉间轮流承担。

3) MockEMGSource：演示用的模拟 EMG 发生器——低 DPI 时臂部肌肉（ECU/UT）
   负荷上升、高 DPI 时手指肌肉（FDS）负荷上升，形成“负荷转移”闭环，
   用于无硬件录屏演示。
"""

import csv
import json
import math
import os
import random
import socket
import struct
import threading
import time
from collections import deque


# ======================================================================
# 滚动窗口（p50 平滑）
# ======================================================================
class RollingWindow:
    def __init__(self, window_sec=10.0):
        self.window_sec = window_sec
        self.samples = deque()  # (t, v)

    def add(self, t, v):
        self.samples.append((t, float(v)))
        cutoff = t - self.window_sec
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def p50(self):
        if not self.samples:
            return None
        vals = sorted(v for _, v in self.samples)
        n = len(vals)
        mid = n // 2
        if n % 2:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2.0

    def latest(self):
        return self.samples[-1][1] if self.samples else None


class EMGMonitor:
    """多肌肉 %MVC 滚动窗口。"""

    def __init__(self, window_sec=10.0, muscles=("EDC", "ECU", "FDS", "UT")):
        self.muscles = list(muscles)
        self.windows = {m: RollingWindow(window_sec) for m in self.muscles}
        self.last = {}

    def update(self, t, values):
        for m in self.muscles:
            v = values.get(m)
            if v is not None:
                self.windows[m].add(t, float(v))
                self.last[m] = float(v)

    def p50(self, muscle):
        w = self.windows.get(muscle)
        return w.p50() if w else None

    def snapshot(self):
        return {m: self.p50(m) for m in self.muscles}


# ======================================================================
# 切换策略基类
# ======================================================================
class _Tracker:
    """持续超过阈值计时器 + 迟滞再触发（re-arm）。"""

    def __init__(self):
        self.over_since = None   # 首次超过阈值的时刻
        self.armed = True        # 是否需要先回落（低于 阈值-迟滞）才能再次触发

    def step(self, now, value, thr, hold_sec, hyst):
        """返回 True 表示本次应触发切换。"""
        if value is None or thr is None:
            return False
        if not self.armed:
            if value < thr - hyst:          # 回落 → 重新武装
                self.armed = True
                self.over_since = None
            return False
        if value >= thr:
            if self.over_since is None:
                self.over_since = now
            if now - self.over_since >= hold_sec:
                self.armed = False          # 触发后需回落再武装（迟滞）
                self.over_since = None
                return True
        else:
            self.over_since = None
        return False


class ThresholdPolicy:
    """阈值法：p50%MVC 达注意值 → 切换（文档 §5.3-1）。"""

    name = "threshold"

    def __init__(self, cfg):
        self.up = {m: v for m, v in (cfg["thresholds"].get("up") or {}).items()
                   if v is not None}
        self.down = {m: v for m, v in (cfg["thresholds"].get("down") or {}).items()
                     if v is not None}
        self.hold_sec = float(cfg.get("hold_sec", 5.0))
        self.min_dwell = float(cfg.get("min_dwell_sec", 60.0))
        self.hyst = float(cfg.get("hysteresis_mvc", 2.0))
        self.last_switch = None
        self.trackers = {("up", m): _Tracker() for m in self.up}
        self.trackers.update({("down", m): _Tracker() for m in self.down})

    def evaluate(self, now, level, monitor):
        """返回 (action|None, reason)，action ∈ {"up","down"}。"""
        if self.last_switch is not None and now - self.last_switch < self.min_dwell:
            return None, "dwell"
        if level == "low":
            for m, thr in self.up.items():
                v = monitor.p50(m)
                if self.trackers[("up", m)].step(now, v, thr, self.hold_sec, self.hyst):
                    self.last_switch = now
                    return "up", f"emg_threshold_up({m}={v:.1f}%MVC>={thr}%)"
        elif level == "high":
            for m, thr in self.down.items():
                v = monitor.p50(m)
                if self.trackers[("down", m)].step(now, v, thr, self.hold_sec, self.hyst):
                    self.last_switch = now
                    return "down", f"emg_threshold_down({m}={v:.1f}%MVC>={thr}%)"
        return None, "ok"


class CrossoverPolicy:
    """交叉点法（模拟版）：当前实测 vs 另一档参考 %MVC 曲线。

    reference 必须来自基准实验校准（800/1200 各肌肉 p50%MVC 参考值）。
    判定：监控肌肉中，若当前实测值比“另一档参考值”高出 margin_mvc 以上
    （即换档后该肌肉负荷更低），且持续 hold_sec → 切换。
    """

    name = "crossover"

    def __init__(self, cfg):
        c = cfg.get("crossover", {})
        self.muscles = list(c.get("muscles", ["EDC", "ECU", "FDS", "UT"]))
        self.reference = {str(k): v for k, v in (c.get("reference") or {}).items()}
        self.margin = float(c.get("margin_mvc", 1.5))
        self.hold_sec = float(cfg.get("hold_sec", 5.0))
        self.min_dwell = float(cfg.get("min_dwell_sec", 60.0))
        self.last_switch = None
        # 与阈值法一致：升档看 ECU/UT，降档看 FDS（可扩展为全部肌肉）
        self.up_muscles = [m for m in ("ECU", "UT") if m in self.muscles]
        self.down_muscles = [m for m in ("FDS",) if m in self.muscles]
        self.trackers = {}
        for d, ms in (("up", self.up_muscles), ("down", self.down_muscles)):
            for m in ms:
                self.trackers[(d, m)] = _Tracker()

    def _ref(self, level_key, muscle):
        ref = self.reference.get(level_key) or {}
        return ref.get(muscle)

    def evaluate(self, now, level, monitor):
        if self.last_switch is not None and now - self.last_switch < self.min_dwell:
            return None, "dwell"
        other = "1200" if level == "low" else "800"
        if level == "low":
            for m in self.up_muscles:
                v = monitor.p50(m)
                ref_other = self._ref(other, m)
                # 对方档位参考值比当前实测低 margin → 值得升档
                thr = (ref_other + self.margin) if ref_other is not None else None
                if thr is not None and self.trackers[("up", m)].step(
                        now, v, thr, self.hold_sec, 0.0):
                    self.last_switch = now
                    return "up", (f"emg_crossover_up({m}={v:.1f}%MVC vs "
                                  f"ref1200={ref_other:.1f}%)")
        elif level == "high":
            for m in self.down_muscles:
                v = monitor.p50(m)
                ref_other = self._ref(other, m)
                thr = (ref_other + self.margin) if ref_other is not None else None
                if thr is not None and self.trackers[("down", m)].step(
                        now, v, thr, self.hold_sec, 0.0):
                    self.last_switch = now
                    return "down", (f"emg_crossover_down({m}={v:.1f}%MVC vs "
                                    f"ref800={ref_other:.1f}%)")
        return None, "ok"


# ======================================================================
# 负荷交替策略（v2）：由"高暴露区域"的交替结果决定切换次数
# ======================================================================
class _ExcessWindow:
    """高暴露区域滚动窗。

    保存最近 ``window_sec`` 内的 ``(t, %MVC)`` 样本，按左黎曼和积分：

        A = Σ max(%MVC_i − thr, 0) · (t_{i+1} − t_i)        [%MVC·s]

    即"超过阈值的强度 × 持续时间"（APDF 曲线上 >thr 那段的面积）。
    再给出**暴露速率** ``rate = A / 时间跨度``（单位 %MVC），
    用速率而不是面积做跨肌肉比较，避免窗口长度不同的量纲混淆。
    """

    def __init__(self, window_sec=300.0, threshold=20.0):
        self.window_sec = float(window_sec)
        self.threshold = float(threshold)
        self.samples = deque()          # (t, %MVC)

    def add(self, t, mvc):
        self.samples.append((float(t), float(mvc)))
        cutoff = float(t) - self.window_sec
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def __len__(self):
        return len(self.samples)

    def quantile(self, q):
        """窗口内幅值的第 q 百分位（%MVC）。

        ``q=50`` 即 Jonsson(1982) APDF 的 **median load level**，
        ``q=10/90`` 对应 static / peak load level。这是**已发表量**，
        可直接在两块肌肉之间比较（同一量、量纲相同），不需要任何加权合成。

        纯标准库实现（本插件不依赖 numpy）；插值方式与 numpy.percentile
        的默认 "linear" 一致，便于与 Python 侧算出的 APDF 对齐。
        """
        if not self.samples:
            return 0.0
        vals = sorted(v for _, v in self.samples)
        n = len(vals)
        if n == 1:
            return float(vals[0])
        pos = (float(q) / 100.0) * (n - 1)
        lo = int(pos)
        if lo >= n - 1:
            return float(vals[-1])
        frac = pos - lo
        return float(vals[lo] + (vals[lo + 1] - vals[lo]) * frac)

    def area(self):
        if len(self.samples) < 2:
            return 0.0
        it = iter(self.samples)
        prev_t, prev_v = next(it)
        total = 0.0
        for t, v in it:
            total += max(prev_v - self.threshold, 0.0) * (t - prev_t)
            prev_t, prev_v = t, v
        return total

    def span(self):
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1][0] - self.samples[0][0]

    def rate(self):
        span = self.span()
        return self.area() / span if span > 0 else 0.0


class BalancePolicy:
    """负荷交替法：按"哪块肌肉的高暴露区域更严重"反复切换 DPI（文档 v2）。

    判定链
    ------
    ::

        rate_m(t) = 最近 roll_window 内该肌肉的高暴露速率（%MVC）
        base_m    = 基线段（会话最初 baseline_sec，固定低档）测到的同一量
        r_m(t)    = rate_m / max(base_m, floor_mvc)         ← 相对自己基线的倍数
        Δ(t)      = r_A − r_B                               （A、B = pair）

    * **交叉**：Δ 变号 → 记一次“交替”事件（无论幅度大小）
    * **执行**：|Δ| ≥ deadband 且持续 confirm_sec → 切换
        - Δ > 0 → A 相对更重 → direction[A]（默认 up）
        - Δ < 0 → B 相对更重 → direction[B]（默认 down）
    * **死时间**：切换后待渐变（switch_ramp_sec）走完 + settle_sec 稳定，
      期间只记录不动作。这不是“每 N 分钟切一次”的时间表，而是
      “上一次干预还没生效之前不许再动”的物理下限。

    **切换次数完全由两块肌肉的交替结果决定**：时钟流逝本身永远不会引起切换。
    三者关系（可用于论文表述）：

    * ``crossings`` = Δ 变号次数 = 负荷在两块肌肉之间**交替**的次数
      （首次确立优势不算交替）
    * ``switches`` = 实际写入驱动的次数 ≤ ``crossings`` + 1
      （+1 是基线段结束后面对一个明显失衡时的那一次合理动作）
    * 负荷全程均衡 → 两者都是 0，这是**"负荷本来就分散"的正常结果**，
      不是策略失效

    为什么先各自归一化：每块肌肉的 MVC 分母不同，直接比绝对面积会让系统
    长期偏向一侧、DPI 一路饱和。归一化后比较的是“相对自己基线被推高了多少”，
    交叉点在构造上必然存在（不会出现“两条线永不交叉所以无解”）。

    比较模式（``balance.compare``）
    ------------------------------
    * ``"auto"``（默认）：两块基线都够大 → ``ratio``（相对自己基线的倍数）；
      否则两块一起退回 ``absolute``（绝对速率）。
    * ``"ratio"``：只要两块基线都 > 0 就比相对倍数（基线≈0 时仍有风险，慎用）。
    * ``"absolute"``：**直接比较原始高暴露速率/面积**——同一滚动窗下速率与
      面积只差一个公共跨度，等价；死区改用 ``deadband_abs_mvc``（单位 %MVC）。
      论文口径就是"比较危险暴露面积"时用这个模式。

    比较节奏（``balance.compare_interval_sec``）
    --------------------------------------------
    默认 ``0`` = 连续判定（旧行为：过死区后需 ``confirm_sec`` 持续确认）。
    设为正数（如 ``900``）时改为**定刻比较**：每隔这么久才做一次比较/判定，
    到点直接按当前窗口的 Δ 决定切或不切（窗口本身就是持续性证据，不再需要
    confirm）。期间只累计、不动作；安全阀不受此限制。

    事件记录（“记录下这个点”）：每次交叉与每次动作追加一行 CSV，字段见
    :meth:`_log_row`。默认写到 ``logs/balance_events_<时间戳>.csv``。
    """

    name = "balance"

    def __init__(self, cfg):
        b = cfg.get("balance", {}) or {}
        pair = list(b.get("pair", ["ECU", "FDS"]))
        if len(pair) != 2:
            raise ValueError(f"balance.pair 必须是两块肌肉，收到 {pair!r}")
        self.pair = pair
        self.A, self.B = pair

        self.threshold = float(b.get("high_thr", cfg.get("high_thr", 20.0)))
        self.window = float(b.get("roll_window_sec", 300.0))
        self.baseline_sec = float(b.get("baseline_sec", self.window))
        self.floor_mvc = float(b.get("floor_mvc", 1.0))
        self.deadband = float(b.get("deadband", 0.15))
        self.deadband_abs = float(b.get("deadband_abs_mvc", 2.0))
        self.confirm = float(b.get("confirm_sec", 30.0))
        self.settle = float(b.get("settle_sec", 120.0))
        self.ramp = float(cfg.get("switch_ramp_sec", 300.0))
        self.safety_mvc = float(b.get("safety_mvc", 40.0))
        self.compare = str(b.get("compare", "auto")).strip().lower()
        if self.compare not in ("auto", "ratio", "absolute"):
            self.compare = "auto"
        # 比较量：apdf_level（默认，已发表量：APDF 百分位水平，Jonsson 1982）
        #         excess_area（向后兼容的自造积分量，论文不应使用）
        self.metric = str(b.get("metric", "apdf_level")).strip().lower()
        if self.metric not in ("apdf_level", "excess_area"):
            self.metric = "apdf_level"
        # 用哪个百分位做比较：50 = median load level（默认），90 = peak
        self.level_pct = float(b.get("level_percentile", 50.0))
        if not 1.0 <= self.level_pct <= 99.0:
            self.level_pct = 50.0
        self.compare_interval = max(
            0.0, float(b.get("compare_interval_sec", 0.0)))
        """定刻比较间隔（秒）：> 0 时每隔这么久才做一次比较/判定（如 900=每 15 分钟）；
        0 = 连续判定（旧行为，需 confirm_sec 持续确认）。"""
        direction = b.get("direction") or {}
        self.direction = {
            self.A: direction.get(self.A, "up"),
            self.B: direction.get(self.B, "down"),
        }
        self.min_dwell = self.ramp + self.settle     # 供 controller 展示与内部死时间
        self.hold_sec = self.confirm

        self.excess = {m: _ExcessWindow(self.window, self.threshold) for m in self.pair}
        self._prev_t = {}
        self._base_rate = {}
        self._base_t0 = None
        self.baseline_ready = False
        self.mode_used = None
        """比较模式：``ratio``（两块肌肉的基线都够大 → 比相对倍数）
        或 ``absolute``（有一块基线≈0 → 两块都退回比绝对速率，保证同量纲）。"""

        self._last_sign = 0
        self._pending_dir = None
        self._pending_since = None
        self._was_saturated = False
        self._in_safety = False        # 是否已处于安全阀状态（只在进入时记一条）
        self._last_cmd = None          # 上一次发出的动作方向
        self._last_cmd_level = None    # 发出时的档位（用于抑制重复刷同一条命令）
        self.last_switch = None
        self._last_compare = None      # 上一次定刻比较的时刻（compare_interval > 0 时）

        # 汇报用计数
        self.stats = {
            "crossings": 0,        # Δ 变号次数（含未过死区的）
            "switches": 0,         # 实际动作次数
            "saturated": 0,        # 撞限幅的"次数"（进入饱和算一次）
            "suppressed": 0,       # 同方向重复命令被抑制的次数
            "evaluations": 0,      # 有效评估次数
            "separated": 0,        # |Δ| 越过死区的评估次数
            "safety_valve": 0,
        }
        self.last_crossing = None
        self._delta = None
        self._deadband_used = None     # 当前实际生效的死区（随模式切换）
        self._r = {}
        self._rates = {}

        # 事件日志（“记录下这个点”）
        self._log_writer = None
        self._log_fh = None
        self.log_path = None
        log_path = b.get("event_log") or ""
        if log_path:
            try:
                if not os.path.isabs(log_path):
                    log_path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), log_path)
                # 按会话分文件（与 SensLog 的 sens_log_<时间戳>.csv 一致）：
                # 追加到同一文件会让多次会话的 t_sim 互相重叠、事后无法区分。
                folder, name = os.path.split(log_path)
                stem, ext = os.path.splitext(name)
                ext = ext or ".csv"
                if not stem.endswith(time.strftime("%Y%m%d")):
                    stem = f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}"
                log_path = os.path.join(folder, stem + ext)
                os.makedirs(folder, exist_ok=True)
                self._log_fh = open(log_path, "w", newline="", encoding="utf-8")
                self._log_writer = csv.writer(self._log_fh)
                self._log_writer.writerow(self._log_header())
                self._log_fh.flush()
                self.log_path = log_path
            except OSError:
                self._log_writer = None
                self._log_fh = None
                self.log_path = None

    # ---------------------------------------------------------------- 展示
    #: controller 的 emg_policy 消息会读取这两项
    @property
    def up(self):
        return {self.A: f"r>{1 + self.deadband:g}x"}

    @property
    def down(self):
        return {self.B: f"r<{1 - self.deadband:g}x"}

    # ---------------------------------------------------------------- 日志
    @staticmethod
    def _log_header():
        return ["t_sim", "event", "A_muscle", "B_muscle",
                "rate_A", "rate_B", "base_A", "base_B", "r_A", "r_B",
                "delta", "deadband", "high_thr", "level", "action", "reason"]

    def _log_row(self, now, event, level, action, reason):
        if self._log_writer is None:
            return
        try:
            self._log_writer.writerow([
                f"{now:.3f}", event, self.A, self.B,
                self._fmt(self._rates.get(self.A)), self._fmt(self._rates.get(self.B)),
                self._fmt(self._base_rate.get(self.A)), self._fmt(self._base_rate.get(self.B)),
                self._fmt(self._r.get(self.A)), self._fmt(self._r.get(self.B)),
                self._fmt(self._delta),
                self._fmt(self._deadband_used
                          if self._deadband_used is not None else self.deadband),
                self.threshold,
                level or "?", action or "hold", reason,
            ])
            self._log_fh.flush()
        except Exception:
            pass

    @staticmethod
    def _fmt(v):
        return "" if v is None else f"{float(v):.4f}"

    def close(self):
        # 结束时把汇总写进事件日志（"切换了几次/交替了几次"），再关文件
        try:
            self._log_row(self._prev_t.get(self.A) or 0.0, "SESSION_END",
                          None, None, self.report())
        except Exception:
            pass
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
            self._log_writer = None
    # ---------------------------------------------------------------- 汇总
    def report(self):
        """结束时的一行汇总（供 controller/日志展示）。"""
        s = self.stats
        ratio = (s["separated"] / s["evaluations"] * 100.0) if s["evaluations"] else 0.0
        return (f"balance: switches={s['switches']} crossings={s['crossings']} "
                f"separation={ratio:.1f}% saturated={s['saturated']} "
                f"suppressed={s['suppressed']} safety={s['safety_valve']} "
                f"mode={self.mode_used} pair={self.A}/{self.B}")

    # ---------------------------------------------------------------- 判定
    def evaluate(self, now, level, monitor):
        """返回 ``(action|None, reason)``；action ∈ {"up","down"}。"""
        pair = self.pair

        # 1) 喂入最新 %MVC（面积靠相邻样本间隔积分）
        fresh = False
        for m in pair:
            v = monitor.last.get(m)
            if v is None:
                continue
            pt = self._prev_t.get(m)
            if pt is not None and now > pt:
                self.excess[m].add(now, v)
                fresh = True
            self._prev_t[m] = now
        if not fresh:
            return None, "no_data"

        if level not in ("low", "high"):
            return None, "no_level"

        # 2) 基线段（会话最初 baseline_sec，固定低档）
        if not self.baseline_ready:
            if self._base_t0 is None:
                self._base_t0 = now
                return None, "baseline"
            if now - self._base_t0 < self.baseline_sec:
                return None, "baseline"
            if self.metric == "apdf_level":
                for m in pair:
                    self._base_rate[m] = self.excess[m].quantile(self.level_pct)
                self.mode_used = "apdf_level"
                self._deadband_used = self.deadband_abs
            else:
                for m in pair:
                    self._base_rate[m] = max(self.excess[m].rate(), 0.0)
            # 两块肌肉的基线都够大 → 比"相对自己基线的倍数"；
            # 有一块≈0（例如低档下该肌肉根本不参与）→ 两块都退回比绝对速率，
            # 否则会把"倍数"和"原始 %MVC"混在一起比，量纲不一致、结论无效。
            # compare="absolute" 时直接比原始速率（面积），不做任何归一化。
            if self.compare == "absolute":
                use_ratio = False
            elif self.compare == "ratio":
                use_ratio = all(self._base_rate[m] > 0.0 for m in pair)
            else:                                   # auto
                use_ratio = all(
                    self._base_rate[m] >= self.floor_mvc for m in pair)
            if self.metric != "apdf_level":
                self.mode_used = "ratio" if use_ratio else "absolute"
                # 死区随模式走：日志里必须写"实际生效"的那个（ratio=0.15 / absolute=2.0）
                self._deadband_used = (self.deadband if use_ratio
                                       else self.deadband_abs)
            self.baseline_ready = True
            # 定刻比较：基线结束后的第一次评估立即算一个比较点
            self._last_compare = now - self.compare_interval
            self._log_row(now, "BASELINE_READY", level, None,
                          f"mode={self.mode_used} "
                          f"base_{self.A}={self._base_rate[self.A]:.4f} "
                          f"base_{self.B}={self._base_rate[self.B]:.4f}")

        # 3) 计算可比严重度 → delta
        #    两种 metric：
        #      excess_area —— 高暴露速率（"超阈值强度×时间"的积分，自造量；
        #                     保留只为向后兼容与对照，论文不应使用）
        #      apdf_level  —— **直接比较两块肌肉的 APDF 百分位水平**（Jonsson 1982）。
        #                     水平本身已是各自归一化过的 %MVC，量纲相同 → 直接取差。
        #                     不做任何加权合成（自造合成量没有文献依据）。
        self._rates = {}
        if self.metric == "apdf_level":
            for m in pair:
                self._rates[m] = self.excess[m].quantile(self.level_pct)
            self._r = {m: self._rates[m] for m in pair}
            self.mode_used = "apdf_level"
            deadband = self.deadband_abs           # 单位 %MVC
        else:
            for m in pair:
                self._rates[m] = self.excess[m].rate()
            if self.mode_used == "ratio":
                self._r = {m: self._rates[m] / self._base_rate[m] for m in pair}
                deadband = self.deadband
            else:
                self._r = {m: self._rates[m] for m in pair}
                deadband = self.deadband_abs
        self._deadband_used = deadband
        delta = self._r[self.A] - self._r[self.B]
        self._delta = delta
        self.stats["evaluations"] += 1

        # 4) 安全阀：两块都过高 → 强制降档（切换已无意义，先卸载）
        latest = [(monitor.last.get(m) or 0.0) for m in pair]
        if self.safety_mvc > 0 and min(latest) > self.safety_mvc:
            if level == "high":
                self.stats["safety_valve"] += 1
                self._in_safety = True
                self.last_switch = now
                self._pending_dir = None
                self._pending_since = None
                reason = f"safety_valve(both>{self.safety_mvc:g}%MVC)"
                self._log_row(now, "SAFETY_VALVE", level, "down", reason)
                return "down", reason
            # 已经是低档：只在**刚进入**安全状态时记一条，否则每次评估都写一行
            # 会把事件日志刷爆（2 Hz × 整个过载期），事后完全无法阅读。
            if not self._in_safety:
                self.stats["safety_valve"] += 1
                self._in_safety = True
                self._log_row(now, "SAFETY_VALVE", level, None,
                              f"safety_valve(both>{self.safety_mvc:g}%MVC,"
                              f"already low)")
            return None, "safety_valve_low"
        self._in_safety = False

        # 5) 交叉判定：Δ 变号 → 记一次交替（无论幅度）
        sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if sign != 0 and self._last_sign != 0 and sign != self._last_sign:
            self.stats["crossings"] += 1
            self.last_crossing = {
                "t": now, "delta": delta,
                "r_A": self._r[self.A], "r_B": self._r[self.B],
            }
            self._log_row(now, "CROSSING", level, None,
                          f"cross({'A>' if sign > 0 else 'B>'}) delta={delta:+.4f}")
        if sign != 0:
            self._last_sign = sign

        # 6) 死时间：上次切换的渐变 + 稳定期未走完 → 只记录不动作
        if self.last_switch is not None and (now - self.last_switch) < self.min_dwell:
            left = self.min_dwell - (now - self.last_switch)
            return None, f"settle({left:.0f}s left)"

        # 6b) 定刻比较：compare_interval > 0 时，每隔这么久才进入一次判定。
        #     到了比较点就直接按当前窗口的 Δ 决定切/不切（窗口本身就是持续性证据，
        #     不再需要 confirm_sec 的连续确认）。
        if self.compare_interval > 0:
            left = self.compare_interval - (now - self._last_compare)
            if left > 1e-9:
                return None, f"next_compare({left:.0f}s)"
            self._last_compare = now
            self._log_row(
                now, "COMPARE", level, None,
                f"compare({self.mode_used}: r_{self.A}={self._r[self.A]:.3f} "
                f"r_{self.B}={self._r[self.B]:.3f} delta={delta:+.3f})")

        # 7) 死区 → 目标方向
        if delta > deadband:
            want = self.direction[self.A]
        elif delta < -deadband:
            want = self.direction[self.B]
        else:
            self._pending_dir = None
            self._pending_since = None
            self._was_saturated = False
            return None, "balanced"

        self.stats["separated"] += 1

        # 8) 已在目标档 → 饱和，不动作（避免无意义写驱动）
        if (want == "up" and level == "high") or (want == "down" and level == "low"):
            if not self._was_saturated:      # 只把"进入饱和"算一次
                self.stats["saturated"] += 1
                self._was_saturated = True
            self._pending_dir = None
            self._pending_since = None
            return None, f"saturated(delta={delta:+.4f})"
        self._was_saturated = False

        # 8b) 同方向命令已经发过、而档位至今没变 → 说明上游没生效（例如渐变
        #     还没走完/被取消），此时重复刷同一条命令只会反复取消渐变、永远
        #     切不过去。抑制它，等档位真的变了或方向翻转再说。
        if want == self._last_cmd and level == self._last_cmd_level:
            self.stats["suppressed"] += 1
            self._pending_dir = None
            self._pending_since = None
            return None, f"repeat_suppressed({want}@{level})"

        # 9) 持续确认（仅连续模式；定刻比较模式在比较点直接动作）
        if self.compare_interval <= 0:
            if self._pending_dir != want:
                self._pending_dir = want
                self._pending_since = now
                return None, "confirming"
            held = now - self._pending_since
            if held < self.confirm:
                return None, f"confirming({held:.0f}/{self.confirm:.0f}s)"

        # 10) 执行
        self.last_switch = now
        self.stats["switches"] += 1
        self._pending_dir = None
        self._pending_since = None
        self._last_cmd = want
        self._last_cmd_level = level
        gate = (f"interval={self.compare_interval:.0f}s"
                if self.compare_interval > 0
                else f"confirm={self.confirm:.0f}s")
        reason = (f"balance_{want}({self.mode_used}: r_{self.A}={self._r[self.A]:.3f} "
                  f"r_{self.B}={self._r[self.B]:.3f} delta={delta:+.3f} "
                  f">={deadband:g}, {gate})")
        self._log_row(now, "SWITCH", level, want, reason)
        return want, reason


# ======================================================================
# 文本协议解析
# ======================================================================
def parse_line(line):
    """解析一行 %MVC 数据，返回 {肌肉: 值}；失败返回 None。"""
    s = line.strip().strip("\x00")
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return {str(k): float(v) for k, v in obj.items()}
    except Exception:
        pass
    # 键值对形式： EDC=11.2,ECU=12.5 或 EDC 11.2 ECU 12.5
    s2 = s.replace("=", " ").replace(":", " ").replace(",", " ")
    toks = s2.split()
    out = {}
    try:
        for i in range(0, len(toks) - 1, 2):
            out[toks[i].upper()] = float(toks[i + 1])
        return out or None
    except ValueError:
        return None


# ======================================================================
# TCP 服务器
# ======================================================================
class EMGTCPServer(threading.Thread):
    def __init__(self, host="127.0.0.1", port=9000, protocol="text",
                 on_update=None, opensignals_cfg=None, name="EMGTCPServer"):
        super().__init__(daemon=True, name=name)
        self.host, self.port, self.protocol = host, port, protocol
        self.on_update = on_update
        self.os_cfg = opensignals_cfg or {}
        self._stop = threading.Event()
        self.sock = None
        self.conn = None
        self.last_error = None

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.host, self.port))
            self.sock.listen(1)
        except OSError as e:
            self.last_error = f"EMG 端口 {self.host}:{self.port} 绑定失败: {e}"
            return
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                self.conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(self.conn)
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        try:
            self.sock.close()
        except OSError:
            pass

    def _handle(self, conn):
        if self.protocol == "opensignals":
            self._handle_opensignals(conn)
        else:
            conn.settimeout(1.0)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        values = parse_line(line.decode("utf-8", errors="replace"))
                    except Exception:
                        values = None
                    if values and self.on_update:
                        self.on_update(values)

    def _handle_opensignals(self, conn):
        """OpenSignals TCP 帧（实验性）：4 字节序号 + n_channels×2 字节 ADC。"""
        n_ch = int(self.os_cfg.get("n_channels", 4))
        channels = self.os_cfg.get("channels", [])
        calib = self.os_cfg.get("adc_to_mvc", {})
        frame_size = 4 + n_ch * 2
        buf = b""
        conn.settimeout(1.0)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= frame_size:
                frame, buf = buf[:frame_size], buf[frame_size:]
                seq = struct.unpack(">I", frame[:4])[0]
                adcs = struct.unpack(">" + "H" * n_ch, frame[4:])
                values = {}
                for i, adc in enumerate(adcs):
                    name = channels[i] if i < len(channels) else f"CH{i+1}"
                    c = calib.get(name) or {}
                    off, scale = c.get("offset", 0.0), c.get("scale", 1.0)
                    values[name] = (float(adc) - float(off)) * float(scale)
                if self.on_update:
                    self.on_update(values)

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.conn:
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ======================================================================
# 模拟 EMG 源（演示）
# ======================================================================
class MockEMGSource(threading.Thread):
    """无硬件演示用的模拟 %MVC 发生器。

    模拟“负荷转移”假设（文档 §3）：低 DPI 时肩臂（ECU/UT）负荷上升、
    手指（FDS）下降；高 DPI 时反之。阈值/交叉点策略据此自动来回切换。
    """

    def __init__(self, level_getter, on_update, cfg=None, time_scale=1.0,
                 name="MockEMGSource"):
        super().__init__(daemon=True, name=name)
        self.level_getter = level_getter
        self.on_update = on_update
        c = cfg or {}
        self.up_rate = float(c.get("up_rate_mvc_per_s", 0.25))
        self.down_rate = float(c.get("down_rate_mvc_per_s", 0.15))
        self.noise = float(c.get("noise_mvc", 0.3))
        self.interval = float(c.get("interval_sim_sec", 0.5))
        # 各肌肉的上下限（可配置；默认值与原行为一致）
        self.ecu_max = float(c.get("ecu_max", 17.5))
        self.ecu_min = float(c.get("ecu_min", 8.0))
        self.fds_max = float(c.get("fds_max", 22.0))
        self.fds_min = float(c.get("fds_min", 8.0))
        self.ut_max = float(c.get("ut_max", 11.0))
        self.ut_min = float(c.get("ut_min", 5.0))
        self.time_scale = float(time_scale)
        self._stop = threading.Event()
        self.state = {"EDC": 11.0, "ECU": 10.0, "FDS": 11.0, "UT": 7.0}

    def stop(self):
        self._stop.set()

    def run(self):
        real_interval = max(self.interval / self.time_scale, 0.02)
        while not self._stop.is_set():
            level = self.level_getter() or "low"
            s = self.state
            if level == "low":
                s["ECU"] = min(s["ECU"] + self.up_rate * self.interval, self.ecu_max)
                s["UT"] = min(s["UT"] + self.up_rate * 0.5 * self.interval, self.ut_max)
                s["FDS"] = max(s["FDS"] - self.down_rate * self.interval, self.fds_min)
            else:
                s["FDS"] = min(s["FDS"] + self.up_rate * self.interval, self.fds_max)
                s["ECU"] = max(s["ECU"] - self.down_rate * self.interval, self.ecu_min)
                s["UT"] = max(s["UT"] - self.down_rate * 0.5 * self.interval, self.ut_min)
            s["EDC"] = max(8.0, min(15.0, s["EDC"] + random.uniform(-0.1, 0.1)))
            values = {k: max(0.0, v + random.uniform(-self.noise, self.noise))
                      for k, v in s.items()}
            if self.on_update:
                self.on_update(values)
            time.sleep(real_interval)
