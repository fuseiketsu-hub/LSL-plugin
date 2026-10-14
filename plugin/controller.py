# -*- coding: utf-8 -*-
"""
controller.py — 统一控制器
===========================
把 调度引擎 / Raw Accel 驱动桥 / 感度日志 / EMG 策略 串成一个整体，
GUI 与 CLI 共用。职责：

- start(mode)：time=时间驱动（会话A）| fixed=固定对照（会话B）
              | manual=手动两档 | emg=EMG 策略驱动 | demo=模拟EMG演示
- 所有灵敏度写入都经 _apply()：写驱动 + 写 sens_log + 更新状态历史
- emergency_reset()：立即恢复 800 DPI（sens=1.0）
- mark(note)：往当前日志追加 MARK 事件（与 EMG/任务时间戳对齐）

时间约定：会话内 elapsed 使用“模拟时间”= 真实时间 × time_scale，
渐变、hold/dwell 等时长均以模拟时间计，方便加速演示（time_scale>1）。
"""

import json
import os
import threading
import time
from collections import deque

from rawaccel_bridge import make_bridge
from scheduler import ScheduleEngine
from session_log import SensLog
from emg_server import (EMGMonitor, EMGTCPServer, MockEMGSource,
                        ThresholdPolicy, CrossoverPolicy, BalancePolicy)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------
# 用户可见消息目录（多语言：en / ja）。GUI 通过 config ui.lang 或
# 构造 overrides={"ui": {"lang": "ja"}} 选择语言。
# ----------------------------------------------------------------------
_MSGS = {
    "en": {
        "driver_fail": "[Driver write failed] {err} (logged; continuing)",
        "session_start": "[Session started] mode={mode} log={path} backend={bk} time_scale={ts}",
        "session_error": "[Session error] {e}",
        "session_stopped": "[Session stopped]",
        "emergency": "[Emergency reset] restored 800 DPI (sens=1.0)",
        "session_reset": "[Session reset] timer zeroed, schedule restarted from the beginning.",
        "reset_idle": "[Reset] No running session; display cleared.",
        "mark": "[Mark] {note}",
        "mark_log": "[Mark log] {path}",
        "cap_time": "[Time schedule] reached session cap {min} min",
        "cap_fixed": "[Control session] reached session cap {min} min",
        "cap_emg": "[EMG policy session] reached session cap {min} min",
        "emg_policy": ("[EMG policy] {name} up-triggers={up} down-triggers={down} "
                       "hold={h}s dwell={d}s"),
        "emg_input": ("[EMG input] TCP server listening on {host}:{port} "
                      "(protocol={proto}), waiting for %MVC data…"),
        "demo": ("[Demo] built-in simulated EMG source started "
                 "(low DPI→ECU↑, high DPI→FDS↑; policy switches automatically)"),
        "emg_input_error": "[EMG input error] {e}",
        "emg_switch": "[EMG switch] {reason}",
        "manual_reject": "[Rejected] Manual switch is disabled in EMG policy-driven mode.",
        "manual_standalone": "[Manual switch] → {level} ({dpi} DPI), written directly without session",
        "manual_ramp": "[Manual switch] → {level} ({dpi} DPI), ramp {sec}s",
    },
    "ja": {
        "driver_fail": "[ドライバー書込失敗] {err}（ログ記録済み、継続）",
        "session_start": "[セッション開始] mode={mode} ログ={path} バックエンド={bk} time_scale={ts}",
        "session_error": "[セッションエラー] {e}",
        "session_stopped": "[セッション停止]",
        "emergency": "[緊急リセット] 800 DPI に復元（sens=1.0）",
        "session_reset": "[セッションリセット] タイマーをゼロにし、スケジュールを最初から再開",
        "reset_idle": "[リセット] セッション未実行：表示のみクリア",
        "mark": "[マーク] {note}",
        "mark_log": "[マークログ] {path}",
        "cap_time": "[時間スケジュール] セッション上限 {min} 分に到達",
        "cap_fixed": "[対照セッション] セッション上限 {min} 分に到達",
        "cap_emg": "[EMGポリシー] セッション上限 {min} 分に到達",
        "emg_policy": ("[EMGポリシー] {name} 上昇トリガー={up} 下降トリガー={down} "
                       "hold={h}s dwell={d}s"),
        "emg_input": ("[EMG入力] TCPサーバー {host}:{port} で待機（protocol={proto}）、"
                      "%MVC データ待ち…"),
        "demo": "[デモ] 内蔵シミュレーションEMG開始（低DPI→ECU↑、高DPI→FDS↑、ポリシーが自動切替）",
        "emg_input_error": "[EMG入力エラー] {e}",
        "emg_switch": "[EMG切替] {reason}",
        "manual_reject": "[拒否] EMGポリシー駆動モードでは手動切替は無効です",
        "manual_standalone": "[手動切替] → {level}（{dpi} DPI）、セッションなしで直接書込み",
        "manual_ramp": "[手動切替] → {level}（{dpi} DPI）、ランプ {sec}秒",
    },
}


def _deep_merge(base, over):
    if not isinstance(over, dict):
        return over
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class DPIController:
    def __init__(self, config_path=None, overrides=None):
        self.config_path = config_path or os.path.join(BASE_DIR, "config.json")
        self.cfg = load_config(self.config_path)
        if overrides:
            self.cfg = _deep_merge(self.cfg, overrides)

        m = self.cfg["mouse"]
        self.base_dpi = float(m["base_dpi"])
        self.levels = m["levels"]  # {"low": {dpi, sens}, "high": {dpi, sens}}
        self.time_scale = float(self.cfg.get("time_scale", 1.0))
        self.tick_sec = float(self.cfg.get("tick_sec", 1.0))
        self.cap_sec = float(self.cfg.get("session_cap_sec", 7200.0))
        self.log_dir = self.cfg.get("log_dir", "logs")
        if not os.path.isabs(self.log_dir):
            self.log_dir = os.path.join(BASE_DIR, self.log_dir)

        self.bridge = make_bridge(self.cfg)
        self.bridge_kind = getattr(self.bridge, "kind", "rawaccel_cli")

        self._lock = threading.RLock()
        self._running = threading.Event()
        self.mode = None
        self.policy = None
        self.policy_name = None
        self.engine = None
        self.log = None
        self.elapsed = 0.0
        self.phase = "idle"
        self.block_idx = ""
        self.target_dpi = ""
        self.applied_sens = self._low_sens()
        self.effective_dpi = self.base_dpi
        self.current_level = "low"
        self.last_reason = ""
        self.error = None
        self.lang = self.cfg.get("ui", {}).get("lang", "en")
        self.history = deque(maxlen=40000)   # (elapsed, effective_dpi)
        self.messages = deque(maxlen=400)    # 界面/控制台提示
        self.monitor = EMGMonitor(window_sec=float(self.cfg["emg"].get("window_sec", 10.0)))
        self.emg_server = None
        self.emg_source = None
        self._threads = []
        self._switch_lock = threading.Lock()
        self._ramp_token = None   # {"stop": threading.Event()}，新渐变会取消旧渐变
        self._ramp_base = None    # 时间引擎渐变的实际起点 sens（支持手动覆盖后保持连续）
        self._t0 = time.monotonic()   # 会话计时基准（复位会话进度时重新设定）

    # ---------------------------------------------------------------- 工具
    def _low_sens(self):
        return float(self.levels["low"]["sens"])

    def _high_sens(self):
        return float(self.levels["high"]["sens"])

    def _level_of_sens(self, sens):
        low, high = self._low_sens(), self._high_sens()
        if abs(sens - low) <= abs(sens - high):
            return "low"
        return "high"

    def _msg(self, key, **kw):
        """按当前语言输出用户可见消息（日志面板与控制台）。"""
        tpl = _MSGS.get(self.lang, _MSGS["en"]).get(key)
        if tpl is None:
            tpl = _MSGS["en"].get(key, key)
        try:
            text = tpl.format(**kw) if kw else tpl
        except Exception:
            text = tpl
        self.messages.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        print(text)

    # ---------------------------------------------------------------- 应用
    def _apply(self, sens, event, mode=None, block_idx="", target_dpi="",
               target_sens="", reason="", emg=None):
        sens = float(sens)
        eff = round(self.base_dpi * sens, 1)
        err = None
        try:
            self.bridge.apply(sens, "off")
        except Exception as e:
            err = str(e)
            self.error = err
        with self._lock:
            self.applied_sens = sens
            self.effective_dpi = eff
            self.history.append((self.elapsed, eff))
            if self.log is not None:
                self.log.row(
                    elapsed=self.elapsed, mode=mode or self.mode, event=event,
                    block_idx=block_idx, target_dpi=target_dpi,
                    target_sens=target_sens, applied_sens=sens,
                    effective_dpi=eff, emg=emg, reason=reason)
        if err:
            self._msg("driver_fail", err=err)
        return eff

    # ---------------------------------------------------------------- 会话控制
    def reconfigure(self, overrides):
        with self._lock:
            if self._running.is_set():
                raise RuntimeError("Cannot change config while a session is running; stop it first.")
            self.cfg = _deep_merge(self.cfg, overrides)
            self.time_scale = float(self.cfg.get("time_scale", 1.0))
            self.tick_sec = float(self.cfg.get("tick_sec", 1.0))
            self.cap_sec = float(self.cfg.get("session_cap_sec", 7200.0))
            if "rawaccel" in overrides:
                self.bridge = make_bridge(self.cfg)
                self.bridge_kind = getattr(self.bridge, "kind", "rawaccel_cli")

    def start(self, mode, policy_name=None):
        with self._lock:
            if self._running.is_set():
                self.stop()
        self.mode = mode
        self.policy_name = policy_name or self.cfg["emg"].get("policy", "threshold")
        self.elapsed = 0.0
        self.error = None
        self.current_level = "low"
        self._running.set()
        self.log = SensLog(log_dir=self.log_dir, prefix="sens_log")
        self._msg("session_start", mode=mode, path=self.log.path,
                  bk=self.bridge_kind, ts=self.time_scale)

        t = threading.Thread(target=self._dispatch, daemon=True, name=f"session-{mode}")
        self._threads.append(t)
        t.start()
        return self.log.path

    def _dispatch(self):
        try:
            if self.mode == "time":
                # 首个块的 sens 在引擎内作为 SESSION_START 立即应用
                # （上升模式从 800 开始、下降模式从 1200 开始，避免先写 800 再跳变）
                self._run_time_engine()
            elif self.mode == "fixed":
                self._apply(self._low_sens(), "SESSION_START", reason="mode=fixed control 800 DPI")
                self._run_fixed_engine()
            elif self.mode == "manual":
                self._apply(self._low_sens(), "SESSION_START", reason="mode=manual initial 800 DPI")
                self._wait_until_stopped()
            elif self.mode in ("emg", "demo"):
                self._apply(self._low_sens(), "SESSION_START", reason=f"mode={self.mode} initial 800 DPI")
                self._run_emg_engine()
            else:
                raise ValueError(f"Unknown mode: {self.mode}")
        except Exception as e:
            self.error = str(e)
            self._msg("session_error", e=e)
        finally:
            self._finish_session()

    def _finish_session(self):
        if not self._running.is_set():
            # 正常停止路径：stop() 已负责复位与结束日志
            return
        self._apply(1.0, "SESSION_END", reason="session ended, restored 800 DPI")
        with self._lock:
            self.phase = "session_end"
            if self.log:
                self.log.close()
                self.log = None
        self._running.clear()

    def stop(self, restore=True):
        """停止会话；restore=True 时复位到基准 800 DPI。"""
        self._running.clear()
        self._stop_children()
        if restore and self.log is not None:
            self._apply(1.0, "SESSION_END", reason="stopped manually, restored 800 DPI")
        with self._lock:
            self.phase = "stopped"
            if self.log:
                self.log.close()
                self.log = None
        self._msg("session_stopped")

    def _stop_children(self):
        if self.emg_server:
            self.emg_server.stop()
            self.emg_server = None
        if self.emg_source:
            self.emg_source.stop()
            self.emg_source = None

    def emergency_reset(self):
        self._apply(1.0, "EMERGENCY_RESET", reason="emergency reset to 800 DPI")
        self._msg("emergency")

    def reset_session(self):
        """复位会话进度：计时归零、清空历史曲线、重新从调度起点开始。

        任何状态下都生效：运行中 → 调度从头重新计时；
        未运行/已结束 → 进度显示清零（下次开始会话不受影响）。
        """
        with self._lock:
            running = self._running.is_set()
            self._t0 = time.monotonic()
            self.elapsed = 0.0
            self.history.clear()
            self.phase = "block" if running else "idle"
            self.block_idx = ""
        if running:
            if self.log is not None:
                self.log.row(elapsed=0.0, mode=self.mode, event="SESSION_RESET",
                             reason="session progress reset, schedule restarted")
            self._msg("session_reset")
        else:
            self._msg("reset_idle")
        return True

    def mark(self, note):
        if self.log is not None:
            self.log.row(elapsed=self.elapsed, mode=self.mode, event="MARK",
                         reason=note or "")
            self._msg("mark", note=note)
            return True
        return False

    def open_log_for_mark(self):
        """仅为打标记打开日志（不启动会话、不写驱动）。"""
        with self._lock:
            if self.log is None:
                self.mode = self.mode or "mark"
                self.log = SensLog(log_dir=self.log_dir, prefix="sens_log")
                self._msg("mark_log", path=self.log.path)
            return self.log.path

    def wait(self, poll=0.2):
        while self._running.is_set():
            time.sleep(poll)

    def _wait_until_stopped(self):
        while self._running.is_set():
            time.sleep(0.2)

    # ---------------------------------------------------------------- 时间驱动（会话A）
    def _run_time_engine(self):
        tm = self.cfg["time_mode"]
        ramp = tm.get("ramp", {})
        engine = ScheduleEngine(
            pattern_dpi=tm["pattern_dpi"], block_sec=float(tm["block_sec"]),
            ramp_sec=float(ramp.get("sec", 300)),
            ramp_placement=ramp.get("placement", "lead_in"),
            repeats=int(tm.get("repeats", 1)), base_dpi=self.base_dpi,
            cap_sec=self.cap_sec, block_durs=tm.get("block_durs"))
        self.engine = engine
        self._msg(engine.plan_text(lang=self.lang).replace("\n", "\n  "))
        with self._lock:
            self._t0 = time.monotonic()
        # 立即应用首个块的 sens（SESSION_START），上升/下降模式各自从正确起点开始
        st0 = engine.state_at(0.0)
        self._apply(st0["applied_sens"], "SESSION_START", block_idx=0,
                    target_dpi=st0["target_dpi"], target_sens=st0["target_sens"],
                    reason="session start")
        last_sens, last_phase = st0["applied_sens"], "block"
        while self._running.is_set():
            with self._lock:
                base = self._t0
            el = (time.monotonic() - base) * self.time_scale
            st = engine.state_at(el)
            with self._lock:
                self.elapsed = el
                self.phase = st["phase"]
                self.block_idx = st["block_idx"]
                self.target_dpi = st["target_dpi"]
            if st["finished"]:
                self._msg("cap_time", min=self.cap_sec / 60)
                break
            sens = st["applied_sens"]
            if st["phase"] == "ramp":
                if last_phase != "ramp":
                    # 进入渐变：以当前实际 sens 为起点（手动覆盖过也能平滑衔接）
                    with self._lock:
                        self._ramp_base = self.applied_sens
                sens = self._ramp_base + (st["target_sens"] - self._ramp_base) * st["ramp_progress"]
            with self._lock:
                # 每秒记录一个时间线点（即使 DPI 未变），保证曲线连续不断线
                if not self.history or abs(self.history[-1][0] - el) > 0.05:
                    self.history.append((el, self.base_dpi * self.applied_sens))
            if (last_sens is None or abs(sens - last_sens) > 1e-9
                    or st["phase"] == "ramp"):
                event = "RAMP" if st["phase"] == "ramp" else "BLOCK"
                self._apply(sens, event, block_idx=st["block_idx"],
                            target_dpi=st["target_dpi"],
                            target_sens=st["target_sens"],
                            reason=self._time_reason(st))
                with self._lock:
                    self.current_level = self._level_of_sens(sens)
                last_sens, last_phase = sens, st["phase"]
            time.sleep(max(self.tick_sec / self.time_scale, 0.002))

    def _time_reason(self, st):
        if st["phase"] == "ramp":
            p = (st.get("ramp_progress") or 0) * 100
            return f"time-driven ramp {p:.0f}%"
        return f"time-driven block {st['block_idx']}"

    # ---------------------------------------------------------------- 固定对照（会话B）
    def _run_fixed_engine(self):
        dpi = float(self.cfg["fixed_mode"]["dpi"])
        engine = ScheduleEngine(
            pattern_dpi=[dpi], block_sec=self.cap_sec, ramp_sec=0.0,
            repeats=1, base_dpi=self.base_dpi, cap_sec=self.cap_sec)
        self.engine = engine
        with self._lock:
            self._t0 = time.monotonic()
        while self._running.is_set():
            with self._lock:
                base = self._t0
            el = (time.monotonic() - base) * self.time_scale
            st = engine.state_at(el)
            with self._lock:
                self.elapsed = el
                self.phase = st["phase"]
                self.block_idx = st["block_idx"]
                self.target_dpi = st["target_dpi"]
            if st["finished"]:
                self._msg("cap_fixed", min=self.cap_sec / 60)
                break
            self._apply(st["applied_sens"], "TICK", block_idx=st["block_idx"],
                        target_dpi=st["target_dpi"], target_sens=st["target_sens"],
                        reason=f"control fixed {dpi:.0f} DPI")
            with self._lock:
                # 每秒记录一个时间线点，保证曲线连续
                if not self.history or abs(self.history[-1][0] - el) > 0.05:
                    self.history.append((el, self.base_dpi * self.applied_sens))
                self.current_level = self._level_of_sens(st["target_sens"])
            time.sleep(max(self.tick_sec / self.time_scale, 0.002))

    # ---------------------------------------------------------------- EMG / 演示
    def _make_policy(self):
        if self.policy_name == "crossover":
            return CrossoverPolicy(self.cfg["emg"])
        if self.policy_name == "balance":
            return BalancePolicy(self.cfg["emg"])
        return ThresholdPolicy(self.cfg["emg"])

    def _run_emg_engine(self):
        emg = self.cfg["emg"]
        self.policy = self._make_policy()
        self._msg("emg_policy", name=self.policy.name,
                  up=getattr(self.policy, "up", {}),
                  down=getattr(self.policy, "down", {}),
                  h=getattr(self.policy, "hold_sec", "?"),
                  d=getattr(self.policy, "min_dwell", "?"))

        with self._lock:
            self._t0 = time.monotonic()

        def on_update(values):
            with self._lock:
                base = self._t0
            el = (time.monotonic() - base) * self.time_scale
            with self._lock:
                self.elapsed = el
                self.phase = "emg"
                self.monitor.update(el, values)
                level = self.current_level
                snap = self.monitor.snapshot()
            action, reason = self.policy.evaluate(el, level, self.monitor)
            if action:
                self._request_emg_switch(action, reason, snap)

        if self.mode == "emg":
            self.emg_server = EMGTCPServer(
                host=emg.get("host", "127.0.0.1"), port=int(emg.get("port", 9000)),
                protocol=emg.get("protocol", "text"), on_update=on_update,
                opensignals_cfg=emg.get("opensignals", {}))
            self.emg_server.start()
            self._msg("emg_input", host=emg.get("host", "127.0.0.1"),
                      port=emg.get("port", 9000),
                      proto=emg.get("protocol", "text"))
        else:  # demo：内置模拟 EMG 源（闭环负荷转移）
            demo_cfg = None
            if self.policy_name == "balance":
                demo_cfg = emg.get("demo_balance")   # balance 演示专用参数
            self.emg_source = MockEMGSource(
                level_getter=lambda: self.current_level, on_update=on_update,
                cfg=demo_cfg or emg.get("demo", {}), time_scale=self.time_scale)
            self.emg_source.start()
            self._msg("demo")

        while self._running.is_set():
            time.sleep(0.2)
            if self.mode == "emg" and self.emg_server and self.emg_server.last_error:
                self._msg("emg_input_error", e=self.emg_server.last_error)
                self.emg_server.last_error = None
            # 会话上限（默认 2 小时）：EMG/balance 驱动的会话同样要自动收尾并复位 800 DPI。
            # 注意用**自己的时钟**推算，不能用 self.elapsed —— 后者只在收到 EMG 数据时
            # 更新，一旦数据断流（OpenSignals 停了/线掉了）上限就永远不会触发，
            # 会话会一直挂着且不复位 DPI。
            with self._lock:
                base = self._t0
            elapsed = (time.monotonic() - base) * self.time_scale if base else 0.0
            if self.cap_sec and elapsed >= self.cap_sec:
                self._msg("cap_emg", min=f"{self.cap_sec / 60:.1f}")
                try:
                    if self.policy is not None and hasattr(self.policy, "close"):
                        self.policy.close()
                except Exception:
                    pass
                self._finish_session()
                return

        # 会话收尾：balance 策略会把汇总（切换次数/交替次数）写进事件日志
        try:
            if self.policy is not None and hasattr(self.policy, "close"):
                self.policy.close()
        except Exception:
            pass

    def _request_emg_switch(self, action, reason, emg_snapshot):
        with self._switch_lock:
            with self._lock:
                level = self.current_level
                start_sens = self.applied_sens
            if action == "up" and level == "low":
                target_sens = self._high_sens()
            elif action == "down" and level == "high":
                target_sens = self._low_sens()
            else:
                return
            self._msg("emg_switch", reason=reason)
            self._ramp_apply(start_sens, target_sens,
                             float(self.cfg["emg"].get("switch_ramp_sec", 300.0)),
                             event_prefix="EMG_SWITCH", reason=reason,
                             emg_snapshot=emg_snapshot)

    # ---------------------------------------------------------------- 渐变执行（分步写入，后台线程）
    def _ramp_apply(self, start_sens, end_sens, dur_sim, event_prefix, reason,
                    emg_snapshot=None, step_sim=0.25, min_real_step=0.02):
        """在 dur_sim 模拟秒内线性渐变，分步写入驱动。

        在独立后台线程执行（不阻塞 EMG 接收/界面）；新请求会取消进行中的渐变，
        从当前实际 sens 继续。时间加速下自动降采样，保证每步间隔 ≥ min_real_step。
        """
        token = {"stop": threading.Event()}
        with self._lock:
            old = self._ramp_token
            self._ramp_token = token
        if old is not None:
            old["stop"].set()

        def run():
            dur_real = dur_sim / self.time_scale
            steps = max(1, min(int(round(dur_sim / step_sim)),
                               max(1, int(dur_real / min_real_step))))
            sleep_real = dur_real / steps
            for i in range(1, steps + 1):
                if token["stop"].is_set() or not self._running.is_set():
                    return
                sens = start_sens + (end_sens - start_sens) * (i / steps)
                self._apply(sens, f"{event_prefix}_RAMP", target_dpi="",
                            target_sens=end_sens, reason=reason, emg=emg_snapshot)
                token["stop"].wait(sleep_real)
            if not token["stop"].is_set() and self._running.is_set():
                self._apply(end_sens, event_prefix, target_dpi="",
                            target_sens=end_sens, reason=reason, emg=emg_snapshot)
                with self._lock:
                    self.current_level = self._level_of_sens(end_sens)
                    if self._ramp_token is token:
                        self._ramp_token = None

        t = threading.Thread(target=run, daemon=True, name=f"ramp-{event_prefix}")
        with self._lock:
            self._threads.append(t)
        t.start()

    # ---------------------------------------------------------------- 手动切换
    def manual_switch(self, level, ramp_sec=None):
        """手动切到 low(800)/high(1200)，ramp_sec=0 立即生效。

        - 无会话（未运行）：直接写入 Raw Accel，不创建会话/日志；
        - time/fixed 会话运行中：作为手动覆盖，直到下一次调度切换；
        - EMG/demo 策略驱动模式下禁用，避免干扰闭环。
        """
        if self._running.is_set() and self.mode in ("emg", "demo"):
            self._msg("manual_reject")
            return False
        target_sens = self._low_sens() if level == "low" else self._high_sens()
        if not self._running.is_set():
            # outside a session: write the driver immediately (no session/log)
            self._apply(target_sens, "MANUAL", mode=self.mode or "standalone",
                        reason=f"manual_{level}_standalone")
            with self._lock:
                self.current_level = level
            self._msg("manual_standalone", level=level,
                      dpi=self.levels[level]["dpi"])
            return True
        if ramp_sec is None:
            ramp_sec = float(self.cfg.get("manual_mode", {}).get("ramp_sec", 300.0))
        self._msg("manual_ramp", level=level, dpi=self.levels[level]["dpi"],
                  sec=ramp_sec)
        self._ramp_apply(self.applied_sens, target_sens, ramp_sec,
                         event_prefix="MANUAL", reason=f"manual_{level}",
                         step_sim=0.25)
        return True

    # ---------------------------------------------------------------- 状态快照（GUI/CLI）
    def status_snapshot(self):
        with self._lock:
            emg_latest = dict(self.monitor.snapshot())
            next_change = None
            if self.engine is not None and self._running.is_set() \
                    and self.mode in ("time", "fixed"):
                next_change = self.engine.next_change(self.elapsed)
            return {
                "running": self._running.is_set(),
                "mode": self.mode,
                "policy": self.policy_name,
                "elapsed": self.elapsed,
                "cap_sec": self.cap_sec,
                "phase": self.phase,
                "block_idx": self.block_idx,
                "target_dpi": self.target_dpi,
                "applied_sens": self.applied_sens,
                "effective_dpi": self.effective_dpi,
                "current_level": self.current_level,
                "last_reason": self.last_reason,
                "error": self.error,
                "log_path": self.log.path if self.log else None,
                "bridge_kind": self.bridge_kind,
                "emg_p50": emg_latest,
                "emg_last": dict(self.monitor.last),
                "next_change": next_change,
                "history": list(self.history),
                "messages": list(self.messages)[-12:],
            }
