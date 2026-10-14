# -*- coding: utf-8 -*-
"""
test_balance_policy.py — 负荷交替策略（BalancePolicy）自检
==========================================================
无需 pytest，直接运行：python tests/test_balance_policy.py
或  python main.py --check（会连同 test_scheduler.py 一起跑）

覆盖：滚动高暴露区域 / 基线段 / 双模式归一化 / 交叉记录 / 死区 / 确认 /
死时间 / 饱和 / 安全阀（含"只记一条"回归）/ 切换次数由交替决定 / 方向可对调 /
参数校验 / 缺数据安全 / 事件日志按会话分文件 / 重复命令抑制 / 绝对模式兜底。
"""

import csv
import os
import shutil
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emg_server import BalancePolicy, _ExcessWindow

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def approx(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


class FakeMonitor:
    """只提供 BalancePolicy 需要的 .last 字典。"""

    def __init__(self, **kw):
        self.last = dict(kw)

    def set(self, **kw):
        self.last.update(kw)


class Sim:
    """模拟 controller：喂 %MVC → 调策略 → 动作后切换档位。

    真实 controller 收到 up/down 后会改 current_level，测试也要跟着换档，
    否则策略会一直以为自己在旧档、反复要求同向切换。
    ``apply_action=False`` 用来模拟"上游没能真正换档"。
    """

    def __init__(self, policy, level="low", apply_action=True):
        self.p = policy
        self.level = level
        self.apply_action = apply_action
        self.monitor = FakeMonitor()
        self.actions = []
        self.last_reason = ""

    def run(self, t0, t1, step=1.0, **values):
        t = t0
        act, why = None, ""
        while t <= t1 + 1e-9:
            self.monitor.set(**values)
            act, why = self.p.evaluate(t, self.level, self.monitor)
            self.last_reason = why
            if act:
                self.actions.append((t, act, why))
                if self.apply_action:
                    self.level = "high" if act == "up" else "low"
            t += step
        return act, why

    @property
    def first_action(self):
        return self.actions[0][1] if self.actions else None

    @property
    def n_actions(self):
        return len(self.actions)


def base_cfg(**over):
    """默认：窗 60s／基线 60s／渐变 20s／无稳定期，便于快速构造场景。

    ``metric`` 显式写 ``excess_area``：那是**自造的积分量**，本文件多数用例
    是在验证这套遗留行为与各种保护机制（死区/确认/死时间/饱和/安全阀…）。
    新的、有文献依据的 ``apdf_level`` 模式见 [17][18]。
    """
    cfg = {
        "switch_ramp_sec": 20.0,
        "balance": {
            "pair": ["ECU", "FDS"],
            "direction": {"ECU": "up", "FDS": "down"},
            "metric": "excess_area",
            "high_thr": 20.0,
            "roll_window_sec": 60.0,
            "baseline_sec": 60.0,
            "floor_mvc": 1.0,
            "deadband": 0.15,
            "deadband_abs_mvc": 2.0,
            "confirm_sec": 5.0,
            "settle_sec": 0.0,
            "safety_mvc": 40.0,
            "event_log": "",
        },
    }
    cfg["balance"].update(over.pop("balance", {}))
    cfg.update(over)
    return cfg


def read_events(path, event):
    with open(path, encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("event") == event]


# ----------------------------------------------------------------------
def test_excess_window():
    print("\n[1] 高暴露区域滚动窗")
    w = _ExcessWindow(window_sec=100.0, threshold=20.0)
    for i in range(11):
        w.add(i * 1.0, 15.0)
    check("低于阈值时面积为 0", approx(w.area(), 0.0), f"area={w.area()}")
    check("速率也为 0", approx(w.rate(), 0.0))

    w2 = _ExcessWindow(window_sec=100.0, threshold=20.0)
    for i in range(11):
        w2.add(i * 1.0, 30.0)
    check("恒定超阈面积 = 10*10", approx(w2.area(), 100.0), f"area={w2.area()}")
    check("速率 = 面积/跨度 = 10", approx(w2.rate(), 10.0), f"rate={w2.rate()}")

    w3 = _ExcessWindow(window_sec=5.0, threshold=20.0)
    for i in range(50):
        w3.add(i * 1.0, 30.0)
    check("窗口外样本被裁掉", len(w3) <= 7, f"len={len(w3)}")

    w4 = _ExcessWindow(window_sec=100.0, threshold=10.0)
    for i in range(11):
        w4.add(i * 1.0, 30.0)
    check("阈值越低面积越大", w4.area() > w2.area(), f"{w4.area()} vs {w2.area()}")


def test_baseline_phase():
    print("\n[2] 基线段未完成前不动作")
    p = BalancePolicy(base_cfg())
    sim = Sim(p)
    sim.run(0.0, 30.0, 1.0, ECU=5.0, FDS=5.0)
    check("基线段内不动作", sim.n_actions == 0)
    check("基线未就绪", p.baseline_ready is False)
    check("状态为 baseline", sim.last_reason == "baseline", sim.last_reason)
    sim.run(31.0, 80.0, 1.0, ECU=5.0, FDS=5.0)
    check("基线完成后标记就绪", p.baseline_ready is True)


def test_ratio_mode_normalization():
    print("\n[3] 相对各自基线归一化（ratio 模式）")
    p = BalancePolicy(base_cfg())
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)      # 超阈 10 : 4
    check("ECU 基线速率 = 10", approx(p._base_rate["ECU"], 10.0, 0.2),
          f"{p._base_rate['ECU']}")
    check("FDS 基线速率 = 4", approx(p._base_rate["FDS"], 4.0, 0.2),
          f"{p._base_rate['FDS']}")
    check("两块基线都够大 → ratio", p.mode_used == "ratio", f"{p.mode_used}")

    sim.run(71.0, 170.0, 1.0, ECU=30.0, FDS=24.0)
    check("维持基线水平 → 均衡不动作", sim.n_actions == 0, f"{sim.actions}")
    check("均衡时 |Δ|<死区", abs(p._delta) < 0.15, f"delta={p._delta}")

    sim2 = Sim(p, level="high")
    sim2.run(171.0, 290.0, 1.0, ECU=30.0, FDS=28.0)  # FDS 相对翻倍
    check("FDS 相对更重 → down", sim2.first_action == "down", f"{sim2.actions[:1]}")
    check("此时 r_FDS > 1", p._r["FDS"] > 1.0, f"r_FDS={p._r['FDS']}")


def test_crossing_recorded():
    print("\n[4] 交叉（Δ 变号）事件被记录")
    p = BalancePolicy(base_cfg(balance={"confirm_sec": 1e9}))   # 只看交叉
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 120.0, 1.0, ECU=40.0, FDS=24.0)
    n1 = p.stats["crossings"]
    sim.run(121.0, 260.0, 1.0, ECU=20.0, FDS=40.0)
    check("发生交叉事件", p.stats["crossings"] > n1, f"{n1} → {p.stats['crossings']}")
    check("CROSSING 已记录", p.last_crossing is not None)
    check("交叉本身不等于动作", p.stats["switches"] == 0, f"{p.stats['switches']}")


def test_deadband():
    print("\n[5] 死区：差值不足不动作")
    p = BalancePolicy(base_cfg(balance={"deadband": 0.5}))
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 220.0, 1.0, ECU=31.0, FDS=24.0)
    check("差值小于死区 → 不动作", sim.n_actions == 0, f"{sim.actions}")
    check("被判定为 balanced", sim.last_reason == "balanced", sim.last_reason)
    check("分离计数为 0", p.stats["separated"] == 0, f"{p.stats['separated']}")


def test_confirm():
    print("\n[6] 持续确认 confirm_sec")
    p = BalancePolicy(base_cfg(balance={"confirm_sec": 20.0}))
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 85.0, 1.0, ECU=45.0, FDS=24.0)
    check("确认期内不动作", sim.n_actions == 0, f"{sim.actions}")
    check("状态为 confirming", sim.last_reason.startswith("confirming"), sim.last_reason)
    sim.run(86.0, 110.0, 1.0, ECU=45.0, FDS=24.0)
    check("确认满后动作 up", sim.first_action == "up", f"{sim.actions[:1]}")
    check("reason 含 balance_up", any("balance_up" in a[2] for a in sim.actions))


def test_settle_after_switch():
    print("\n[7] 切换后死时间 = 渐变 + 稳定期")
    p = BalancePolicy(base_cfg(switch_ramp_sec=100.0,
                               balance={"confirm_sec": 5.0, "settle_sec": 50.0}))
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 90.0, 1.0, ECU=45.0, FDS=24.0)
    check("首次动作 up", sim.first_action == "up", f"{sim.actions[:1]}")
    sim.run(p.last_switch + 1, p.last_switch + 149, 1.0, ECU=45.0, FDS=24.0)
    check("死时间内只有 1 次动作", sim.n_actions == 1, f"{sim.actions}")
    check("状态为 settle", sim.last_reason.startswith("settle"), sim.last_reason)


def test_saturated_episode_count():
    print("\n[8] 已在目标档 → 饱和（按次数计，不按评估计）")
    p = BalancePolicy(base_cfg())
    sim = Sim(p, level="high")
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 200.0, 1.0, ECU=45.0, FDS=24.0)
    check("已在高档时不返回 up", sim.n_actions == 0, f"{sim.actions}")
    check("记录为 saturated", "saturated" in sim.last_reason, sim.last_reason)
    check("撞限幅计为 1 次", p.stats["saturated"] == 1, f"{p.stats['saturated']}")
    check("未误计切换", p.stats["switches"] == 0)


def test_safety_valve():
    print("\n[9] 安全阀：两块同时过高 → 强制降档")
    p = BalancePolicy(base_cfg(balance={"safety_mvc": 40.0}))
    sim = Sim(p, level="high")
    sim.run(0.0, 70.0, 1.0, ECU=45.0, FDS=45.0)
    sim.run(71.0, 90.0, 1.0, ECU=45.0, FDS=45.0)
    check("强制 down", sim.first_action == "down", f"{sim.actions[:1]}")
    check("原因为 safety_valve", any("safety_valve" in a[2] for a in sim.actions))
    check("安全阀已计数", p.stats["safety_valve"] > 0)


def test_safety_valve_logs_once_per_episode():
    """回归：已在低档且两块都过高时只记**一条**日志。

    之前是每次评估都写一行（2 Hz），整个过载期把事件日志刷成几千行，事后没法读。
    """
    tmpdir = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmpdir, "balance_events.csv")
        p = BalancePolicy(base_cfg(balance={"safety_mvc": 40.0,
                                            "event_log": log_path}))
        sim = Sim(p, level="low")                     # 已在低档，无处可降
        sim.run(0.0, 70.0, 1.0, ECU=45.0, FDS=45.0)
        sim.run(71.0, 200.0, 1.0, ECU=45.0, FDS=45.0)
        # 不 close（close 会关掉日志文件，之后就不再写行）；每行都 flush，可直接读
        rows = read_events(p.log_path, "SAFETY_VALVE")
        check("安全阀只记一条", len(rows) == 1, f"行数={len(rows)}")
        check("计数也只加一次", p.stats["safety_valve"] == 1,
              f"{p.stats['safety_valve']}")

        sim.run(201.0, 230.0, 1.0, ECU=10.0, FDS=10.0)   # 负荷降下来
        sim.run(231.0, 260.0, 1.0, ECU=45.0, FDS=45.0)   # 再过载 → 新的一段
        rows = read_events(p.log_path, "SAFETY_VALVE")
        check("恢复后再过载 → 记为第二段", len(rows) == 2, f"行数={len(rows)}")
        p.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _alternating_session(policy, rounds, seg_sec, ecu_hi=45.0, fds_hi=45.0):
    """构造负荷在两块肌肉间反复交替的任务（窗口须短于单段时长）。"""
    sim = Sim(policy)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    t = 71.0
    for _ in range(rounds):
        sim.run(t, t + seg_sec, 1.0, ECU=ecu_hi, FDS=24.0)
        t += seg_sec + 1
        sim.run(t, t + seg_sec, 1.0, ECU=30.0, FDS=fds_hi)
        t += seg_sec + 1
    return sim


def test_switch_count_is_data_driven():
    print("\n[10] 切换次数完全由交替结果决定（无定时器）")
    p = BalancePolicy(base_cfg())
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 2000.0, 1.0, ECU=30.0, FDS=24.0)
    check("负荷恒定时 0 次切换", p.stats["switches"] == 0, f"{p.stats}")
    check("也没有交叉", p.stats["crossings"] == 0, f"{p.stats['crossings']}")

    p2 = BalancePolicy(base_cfg())
    sim2 = _alternating_session(p2, rounds=6, seg_sec=80.0)
    check("交替越多切得越多", p2.stats["switches"] >= 4, f"{p2.stats}")
    # 首次确立优势不算"交替"，但控制器会合理地动作一次 → 动作数 ≤ 交叉数 + 1
    check("切换次数 ≤ 交叉次数 + 1",
          p2.stats["switches"] <= p2.stats["crossings"] + 1, f"{p2.stats}")
    check("确实在两档之间来回", len({a[1] for a in sim2.actions}) == 2,
          f"{[a[1] for a in sim2.actions]}")

    p3 = BalancePolicy(base_cfg(balance={"deadband": 5.0}))
    _alternating_session(p3, rounds=6, seg_sec=80.0)
    check("死区大 → 切换被抑制", p3.stats["switches"] < p2.stats["switches"],
          f"{p3.stats['switches']} < {p2.stats['switches']}")

    p4 = BalancePolicy(base_cfg())
    _alternating_session(p4, rounds=12, seg_sec=80.0)
    check("交替 12 轮比 6 轮切得多", p4.stats["switches"] > p2.stats["switches"],
          f"{p4.stats['switches']} > {p2.stats['switches']}")


def test_direction_swappable():
    print("\n[11] 方向映射可由配置对调")
    p = BalancePolicy(base_cfg(balance={"direction": {"ECU": "down", "FDS": "up"}}))
    sim = Sim(p, level="high")
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 200.0, 1.0, ECU=45.0, FDS=24.0)
    check("对调后 ECU 更重 → down", sim.first_action == "down", f"{sim.actions[:1]}")


def test_pair_configurable_and_validated():
    print("\n[12] 肌肉对可配置 + 参数校验 + 汇总")
    p = BalancePolicy(base_cfg(balance={"pair": ["FDS", "UT"],
                                        "direction": {"FDS": "down", "UT": "up"}}))
    check("任意两块肌肉都能用", p.A == "FDS" and p.B == "UT")
    check("展示用 up/down 已生成", bool(p.up) and bool(p.down))
    try:
        BalancePolicy(base_cfg(balance={"pair": ["ECU"]}))
        check("肌肉数不为 2 时应报错", False)
    except ValueError:
        check("肌肉数不为 2 时报错", True)

    p2 = BalancePolicy(base_cfg())
    sim = Sim(p2)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 160.0, 1.0, ECU=45.0, FDS=24.0)
    rep = p2.report()
    check("report 含 switches/crossings", "switches=" in rep and "crossings=" in rep, rep)
    check("report 含 separation", "separation=" in rep, rep)
    check("report 含 mode", "mode=" in rep, rep)


def test_missing_data_is_safe():
    print("\n[13] 缺数据/缺档位时不崩溃")
    p = BalancePolicy(base_cfg())
    m = FakeMonitor()
    act, reason = p.evaluate(0.0, "low", m)
    check("无数据返回 no_data", reason == "no_data", reason)
    check("无数据不动作", act is None)
    m.last["ECU"] = 30.0
    act, reason = p.evaluate(1.0, "low", m)
    check("只有一块肌肉也不崩溃", act is None, reason)
    m.last["FDS"] = 25.0
    act, reason = p.evaluate(2.0, None, m)
    check("档位未知返回 no_level", reason == "no_level", reason)
    act, reason = p.evaluate(3.0, "low", FakeMonitor(ECU=float("nan"), FDS=1.0))
    check("NaN 不崩溃", act is None, reason)


def test_event_log_per_session():
    print("\n[14] 事件日志按会话分文件")
    tmpdir = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmpdir, "balance_events.csv")
        p = BalancePolicy(base_cfg(balance={"event_log": log_path}))
        sim = Sim(p)
        sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
        sim.run(71.0, 160.0, 1.0, ECU=45.0, FDS=24.0)
        p.close()
        check("日志路径已记录", bool(p.log_path), f"{p.log_path}")
        check("日志文件已生成", bool(p.log_path) and os.path.exists(p.log_path))
        base = os.path.basename(p.log_path or "")
        check("按会话分文件（带时间戳）",
              base.startswith("balance_events_") and base.endswith(".csv"), base)
        with open(p.log_path, encoding="utf-8") as fh:
            text = fh.read()
        check("含表头 delta/deadband/high_thr",
              all(k in text for k in ("delta", "deadband", "high_thr")))
        check("含 BASELINE_READY", "BASELINE_READY" in text)
        check("含 SWITCH 记录", "SWITCH" in text)
        check("含结束时汇总", "SESSION_END" in text)
        check("记录了 mode", "mode=" in text)

        paths = [p.log_path]
        time.sleep(1.1)
        p2 = BalancePolicy(base_cfg(balance={"event_log": log_path}))
        Sim(p2).run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
        p2.close()
        paths.append(p2.log_path)
        check("两次会话两个文件", paths[0] != paths[1], f"{paths}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_duplicate_command_suppressed():
    print("\n[15] 同方向重复命令被抑制（上游没生效时不再反复刷）")
    p = BalancePolicy(base_cfg())
    sim = Sim(p, apply_action=False)      # 模拟上游没换档
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim.run(71.0, 160.0, 1.0, ECU=45.0, FDS=24.0)
    first = sim.n_actions
    check("先发出一次 up", first == 1, f"n={first} {sim.actions[:2]}")
    sim.run(161.0, 900.0, 1.0, ECU=45.0, FDS=24.0)
    check("档位未变时不再重复发同向命令", sim.n_actions == first, f"{sim.actions}")
    check("记录了 suppressed 计数", p.stats["suppressed"] > 0,
          f"{p.stats['suppressed']}")
    check("状态为 repeat_suppressed", "repeat_suppressed" in sim.last_reason,
          sim.last_reason)

    p2 = BalancePolicy(base_cfg())
    sim2 = Sim(p2)
    sim2.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    sim2.run(71.0, 160.0, 1.0, ECU=45.0, FDS=24.0)
    check("换档后第一次 up 正常", sim2.n_actions == 1, f"{sim2.actions[:2]}")


def test_absolute_mode_when_baseline_zero():
    print("\n[16] 基线≈0 时退回绝对模式（避免量纲混用）")
    p = BalancePolicy(base_cfg())
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=10.0, FDS=8.0)
    check("进入 absolute 模式", p.mode_used == "absolute", f"{p.mode_used}")
    check("两块基线都为 0",
          p._base_rate["ECU"] == 0.0 and p._base_rate["FDS"] == 0.0,
          f"{p._base_rate}")
    sim.run(71.0, 200.0, 1.0, ECU=30.0, FDS=22.0)
    check("absolute 模式下 r 就是实测速率", p._r["ECU"] > 5.0, f"r={p._r}")
    check("绝对模式也能动作", sim.n_actions >= 1, f"{sim.actions[:2]}")

    p2 = BalancePolicy(base_cfg())
    Sim(p2).run(0.0, 70.0, 1.0, ECU=30.0, FDS=26.0)
    check("基线足够大时用 ratio 模式", p2.mode_used == "ratio", f"{p2.mode_used}")


def test_compare_absolute_vs_ratio():
    print("\n[17] compare=absolute：直接比较原始高暴露面积/速率")
    # 基线：ECU 超阈速率 10、FDS 超阈速率 4。
    # 之后：ECU=60（超阈 40）、FDS=48（超阈 28）——
    #   绝对量 ECU 更重（40 > 28）→ absolute 应切向 up；
    #   但相对自己基线 FDS 翻得更多（7x vs 4x）→ ratio 应切向 down。
    p_abs = BalancePolicy(base_cfg(balance={"compare": "absolute"}))
    sim_abs = Sim(p_abs)
    sim_abs.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    check("强制 absolute 模式", p_abs.mode_used == "absolute",
          f"{p_abs.mode_used}")
    sim_abs.run(71.0, 200.0, 1.0, ECU=60.0, FDS=48.0)
    check("绝对模式 r 就是原始速率", p_abs._r["ECU"] > p_abs._r["FDS"] > 0,
          f"r={p_abs._r}")
    check("更重的 ECU → up", sim_abs.first_action == "up",
          f"{sim_abs.actions[:2]}")

    p_ratio = BalancePolicy(base_cfg())          # 默认 auto：基线够大 → ratio
    # 初始档位取 high：ratio 判定 FDS 更重时，卸载方向是 down（低档才动手）
    sim_ratio = Sim(p_ratio, level="high")
    sim_ratio.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    check("auto 基线够大时用 ratio", p_ratio.mode_used == "ratio",
          f"{p_ratio.mode_used}")
    sim_ratio.run(71.0, 200.0, 1.0, ECU=60.0, FDS=48.0)
    check("ratio 下 FDS 翻倍更狠 → down", sim_ratio.first_action == "down",
          f"{sim_ratio.actions[:2]}")


def test_event_log_deadband_matches_mode():
    print("\n[18] 事件日志的 deadband 列必须写实际生效的死区")
    tmpdir = tempfile.mkdtemp()
    try:
        log_abs = os.path.join(tmpdir, "abs.csv")
        p_abs = BalancePolicy(base_cfg(balance={"compare": "absolute",
                                                "event_log": log_abs}))
        Sim(p_abs).run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
        p_abs.close()
        rows = read_events(p_abs.log_path, "BASELINE_READY")
        check("absolute：日志 deadband=2.0（不是 0.15）",
              bool(rows) and approx(float(rows[0]["deadband"]), 2.0, 1e-9),
              rows[0]["deadband"] if rows else "no BASELINE_READY row")

        log_ratio = os.path.join(tmpdir, "ratio.csv")
        p_ratio = BalancePolicy(base_cfg(balance={"event_log": log_ratio}))
        Sim(p_ratio).run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
        p_ratio.close()
        rows = read_events(p_ratio.log_path, "BASELINE_READY")
        check("ratio：日志 deadband=0.15",
              bool(rows) and approx(float(rows[0]["deadband"]), 0.15, 1e-9),
              rows[0]["deadband"] if rows else "no BASELINE_READY row")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_compare_interval():
    print("\n[19] compare_interval_sec：每 15 分钟才比较一次（定刻比较）")
    p = BalancePolicy(base_cfg(balance={"compare_interval_sec": 900.0}))
    sim = Sim(p)
    sim.run(0.0, 70.0, 1.0, ECU=30.0, FDS=24.0)
    check("基线结束即完成首次比较", approx(p._last_compare, 60.0, 1.5),
          f"_last_compare={p._last_compare}")
    sim.run(71.0, 959.0, 1.0, ECU=60.0, FDS=24.0)
    check("比较间隔内不动作", sim.n_actions == 0, f"{sim.actions[:2]}")
    check("状态为 next_compare", "next_compare" in sim.last_reason,
          sim.last_reason)
    sim.run(960.0, 1010.0, 1.0, ECU=60.0, FDS=24.0)
    check("到点直接动作（不再需要 30 s 确认）", sim.first_action == "up",
          f"{sim.actions[:2]}")
    check("动作发生在比较点 960 s 附近",
          bool(sim.actions) and approx(sim.actions[0][0], 960.0, 1.5),
          f"{sim.actions[:2]}")


def test_quantile_is_published_load_level():
    print("\n[17] quantile = APDF 负荷水平（Jonsson 1982 的 static/median/peak）")
    w = _ExcessWindow(window_sec=1000.0, threshold=20.0)
    for i, v in enumerate([10.0, 20.0, 30.0, 40.0, 100.0]):
        w.add(float(i), v)
    # 手算校验（线性插值，与 numpy.percentile 默认一致）：
    #   P50: pos=0.5*4=2.0        → vals[2] = 30
    #   P10: pos=0.1*4=0.4        → 10 + 0.4*(20-10) = 14
    #   P90: pos=0.9*4=3.6        → 40 + 0.6*(100-40) = 76
    check("P50 = 30", approx(w.quantile(50), 30.0), w.quantile(50))
    check("P10 = 14", approx(w.quantile(10), 14.0), w.quantile(10))
    check("P90 = 76", approx(w.quantile(90), 76.0), w.quantile(90))
    check("空窗返回 0", approx(_ExcessWindow().quantile(50), 0.0))
    w2 = _ExcessWindow(window_sec=3.0, threshold=20.0)
    for i, v in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]):
        w2.add(float(i), v)
    check("窗口外的样本不参与分位数", w2.quantile(50) >= 4.0, w2.quantile(50))


def test_apdf_level_metric():
    print("\n[18] apdf_level：直接比较两块肌肉的 APDF 百分位水平（无合成）")
    p = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                        "confirm_sec": 5.0}))
    sim = Sim(p)
    # 基线：两块肌肉的负荷水平相同 → 不应该动作
    sim.run(0.0, 70.0, 1.0, ECU=20.0, FDS=20.0)
    check("模式为 apdf_level", p.mode_used == "apdf_level", f"{p.mode_used}")
    check("基线记录的是中位水平而非速率",
          approx(p._base_rate["ECU"], 20.0, 0.01), f"{p._base_rate['ECU']}")
    check("水平相同 → 不动作", sim.n_actions == 0, f"{sim.actions[:2]}")

    # ECU 水平明显更高 → up；Δ 应等于 P50 之差（单位 %MVC）
    sim.run(71.0, 260.0, 1.0, ECU=45.0, FDS=20.0)
    check("水平更高的一块触发 up", sim.first_action == "up", f"{sim.actions[:1]}")
    check("Δ = P50_ECU − P50_FDS = 25", approx(p._delta, 25.0, 0.6), f"{p._delta}")
    check("死区用绝对 %MVC（2.0）", approx(p._deadband_used, 2.0),
          f"{p._deadband_used}")
    check("日志里的 r 就是水平本身",
          approx(p._r["ECU"], 45.0, 0.6) and approx(p._r["FDS"], 20.0, 0.6),
          f"{p._r}")

    # 反过来：FDS 水平更高 → down
    p2 = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                         "confirm_sec": 5.0}))
    sim2 = Sim(p2, level="high")
    sim2.run(0.0, 70.0, 1.0, ECU=20.0, FDS=20.0)
    sim2.run(71.0, 260.0, 1.0, ECU=20.0, FDS=45.0)
    check("另一块更高触发 down", sim2.first_action == "down",
          f"{sim2.actions[:1]}")
    check("Δ 变号", p2._delta < 0, f"{p2._delta}")

    # 差值小于死区 → 不动作（22 vs 20，Δ=2 未超过 2.0 不含）
    p3 = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                         "deadband_abs_mvc": 5.0}))
    sim3 = Sim(p3)
    sim3.run(0.0, 70.0, 1.0, ECU=20.0, FDS=20.0)
    sim3.run(71.0, 300.0, 1.0, ECU=22.0, FDS=20.0)
    check("差值小于死区 → 不动作", sim3.n_actions == 0, f"{sim3.actions[:2]}")

    # 用 P90（peak load level）比较
    p4 = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                         "level_percentile": 90.0,
                                         "confirm_sec": 5.0}))
    sim4 = Sim(p4)
    sim4.run(0.0, 70.0, 1.0, ECU=20.0, FDS=20.0)
    sim4.run(71.0, 260.0, 1.0, ECU=45.0, FDS=20.0)
    check("可用 P90 作比较量", p4.level_pct == 90.0, f"{p4.level_pct}")
    check("P90 模式下 Δ 接近 25", approx(p4._delta, 25.0, 0.6), f"{p4._delta}")

    # 非法 metric / 百分位要退回安全值
    p5 = BalancePolicy(base_cfg(balance={"metric": "乱写",
                                         "level_percentile": 500.0}))
    check("非法 metric 退回 apdf_level", p5.metric == "apdf_level", p5.metric)
    check("非法百分位退回 50", p5.level_pct == 50.0, f"{p5.level_pct}")


def test_apdf_level_keeps_safety_and_logging():
    print("\n[19] apdf_level 模式下保护机制与日志仍然生效")
    tmpdir = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmpdir, "balance_events.csv")
        p = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                            "event_log": log_path}))
        sim = Sim(p)
        sim.run(0.0, 70.0, 1.0, ECU=20.0, FDS=20.0)
        sim.run(71.0, 260.0, 1.0, ECU=45.0, FDS=25.0)
        p.close()
        with open(p.log_path, encoding="utf-8") as fh:
            text = fh.read()
        check("日志记录 metric 名称", "apdf_level" in text, text[:200])
        check("日志含 BASELINE_READY", "BASELINE_READY" in text)
        check("日志含 SWITCH", "SWITCH" in text)
        check("切换被计数", p.stats["switches"] >= 1, f"{p.stats}")

        # 安全阀在 apdf_level 下独立于比较量，仍按原始 %MVC 判定
        p2 = BalancePolicy(base_cfg(balance={"metric": "apdf_level",
                                             "safety_mvc": 40.0}))
        sim2 = Sim(p2, level="high")
        sim2.run(0.0, 70.0, 1.0, ECU=45.0, FDS=45.0)
        sim2.run(71.0, 120.0, 1.0, ECU=45.0, FDS=45.0)
        check("安全阀仍强制降档", sim2.first_action == "down",
              f"{sim2.actions[:1]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ----------------------------------------------------------------------
def main():
    print("=" * 68)
    print("BalancePolicy 自检（负荷交替法）")
    print("=" * 68)
    for fn in (test_excess_window, test_baseline_phase,
               test_ratio_mode_normalization, test_crossing_recorded,
               test_deadband, test_confirm, test_settle_after_switch,
               test_saturated_episode_count, test_safety_valve,
               test_safety_valve_logs_once_per_episode,
               test_switch_count_is_data_driven, test_direction_swappable,
               test_pair_configurable_and_validated, test_missing_data_is_safe,
               test_event_log_per_session, test_duplicate_command_suppressed,
               test_absolute_mode_when_baseline_zero,
               test_quantile_is_published_load_level, test_apdf_level_metric,
               test_apdf_level_keeps_safety_and_logging,
               test_compare_absolute_vs_ratio,
               test_event_log_deadband_matches_mode,
               test_compare_interval):
        fn()
    print("\n" + "=" * 68)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
