# -*- coding: utf-8 -*-
"""
test_scheduler.py — 自检测试（无需 pytest，直接运行）
======================================================
覆盖：时间调度引擎（块/渐变/上限）、阈值策略、交叉点策略、日志、模板合并。
运行：python tests/test_scheduler.py  或  python main.py --check
"""

import json
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import ScheduleEngine
from session_log import SensLog
from rawaccel_bridge import (_deep_set, _find_value, build_v17_payload,
                             find_v17_install_dir, sens_to_output_dpi)
from emg_server import EMGMonitor, ThresholdPolicy, CrossoverPolicy, parse_line

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


class FakeMonitor:
    """策略测试用：直接给定各肌肉 p50 值。"""

    def __init__(self, values):
        self.values = values

    def p50(self, m):
        return self.values.get(m)


# ======================================================================
print("== 1. 时间调度引擎（会话A：800,800,1200,1200 × 30min + 5min 渐变）==")
eng = ScheduleEngine(pattern_dpi=[800, 800, 1200, 1200], block_sec=1800,
                     ramp_sec=300, ramp_placement="lead_in", repeats=1,
                     base_dpi=800, cap_sec=7200)
check("块数 = 4", len(eng.blocks) == 4)
check("总时长 = 7200s", abs(eng.total_sec - 7200) < 1e-6)

st = eng.state_at(0.5)
check("t=0.5 → 块0, 800DPI, sens=1.0",
      st["phase"] == "block" and st["effective_dpi"] == 800 and st["block_idx"] == 0)
st = eng.state_at(1800.5)
check("t=1800.5 → 块1, 仍 800DPI（无渐变，目标相同）",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng.state_at(3600 + 10)
check("t=3610 → 渐变中，sens≈1.0167",
      st["phase"] == "ramp" and abs(st["applied_sens"] - (1.0 + 0.5 * 10 / 300)) < 1e-6,
      f"got {st['applied_sens']}")
st = eng.state_at(3600 + 150)
check("t=3750（渐变中点）→ sens=1.25",
      abs(st["applied_sens"] - 1.25) < 1e-6, f"got {st['applied_sens']}")
st = eng.state_at(3900 + 5)
check("t=3905 → 块2, 1200DPI, sens=1.5",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng.state_at(7200)
check("t=7200 → 会话结束并复位 sens=1.0",
      st["finished"] and abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng.state_at(7500)
check("t=7500（超时）→ finished", st["finished"])
check("渐变计划 1 次（800→1200 在 3600s）", len(eng.transitions()) == 1
      and abs(eng.transitions()[0][0] - 3600) < 1e-6)

eng2 = ScheduleEngine(pattern_dpi=[800, 800, 1200, 1200], block_sec=1800,
                      ramp_sec=300, ramp_placement="lead_out", repeats=1,
                      base_dpi=800, cap_sec=7200)
st = eng2.state_at(3300 + 10)
check("lead_out: t=3310 → 渐变中", st["phase"] == "ramp", f"got {st['phase']}")
check("lead_out: 渐变窗口在 3300–3600",
      abs(eng2.transitions()[0][0] - 3300) < 1e-6)

eng3 = ScheduleEngine(pattern_dpi=[800], block_sec=7200, ramp_sec=0,
                      repeats=1, base_dpi=800, cap_sec=7200)
st = eng3.state_at(3600)
check("会话B 对照引擎：恒定 800DPI",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.0) < 1e-9)

# 可编辑时间窗口：1 小时实验、渐变从 30 分钟开始（5 分钟渐变）
eng4 = ScheduleEngine(pattern_dpi=[800, 1200], block_sec=1800, ramp_sec=300,
                      ramp_placement="lead_in", repeats=1, base_dpi=800,
                      cap_sec=3600)
st = eng4.state_at(1799.0)
check("1h实验: t=29:59 → 800DPI", abs(st["applied_sens"] - 1.0) < 1e-9
      and not st["finished"], f"got {st['applied_sens']}")
st = eng4.state_at(1810.0)
check("1h实验: t=30:10 → 渐变中",
      st["phase"] == "ramp" and abs(st["applied_sens"] - 1.0 - 0.5 * 10 / 300) < 1e-6)
st = eng4.state_at(2100.0)
check("1h实验: t=35:00 → 1200DPI",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng4.state_at(3600.0)
check("1h实验: t=60:00 → 会话结束", st["finished"])

# 会话上限超过块总长：90 分钟实验（块总长 60 分钟）→ 剩余时间保持 1200
eng5 = ScheduleEngine(pattern_dpi=[800, 1200], block_sec=1800, ramp_sec=300,
                      repeats=1, base_dpi=800, cap_sec=5400)
st = eng5.state_at(4000.0)
check("90min实验: t=66:40 → 保持 1200 且未结束",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.5) < 1e-9
      and not st["finished"], f"got {st}")
st = eng5.state_at(5400.0)
check("90min实验: t=90:00 → 会话结束", st["finished"])

# ======================================================================
print("== 2. 阈值策略（ECU≥16%↑、FDS≥20%↓，hold=5s，dwell=60s，迟滞=2）==")
cfg = {
    "thresholds": {"up": {"ECU": 16.0, "UT": None}, "down": {"FDS": 20.0}},
    "hold_sec": 5.0, "min_dwell_sec": 60.0, "hysteresis_mvc": 2.0,
}
pol = ThresholdPolicy(cfg)
a, r = pol.evaluate(0.0, "low", FakeMonitor({"ECU": 17.0}))
check("t=0 ECU=17 → 未触发（需持续 5s）", a is None, f"got {a}")
a, r = pol.evaluate(5.1, "low", FakeMonitor({"ECU": 17.0}))
check("t=5.1 → 触发升档", a == "up", f"got {a}")
a, r = pol.evaluate(6.0, "high", FakeMonitor({"FDS": 21.0}))
check("t=6（dwell 内）→ 不触发降档", a is None, f"got {a}")
a, r = pol.evaluate(66.0, "high", FakeMonitor({"FDS": 21.0}))
check("t=66 → 开始计时（dwell 结束，持续观察中）", a is None, f"got {a}")
a, r = pol.evaluate(71.1, "high", FakeMonitor({"FDS": 21.0}))
check("t=71.1 → 触发降档（FDS≥20 持续 5s）", a == "down", f"got {a}")
# 迟滞：升档触发后 ECU 仍高，需先回落 <14 再超过 16 才能再触发
a, r = pol.evaluate(130.0, "low", FakeMonitor({"ECU": 17.0}))
check("迟滞：未回落前不再触发", a is None, f"got {a}")
a, r = pol.evaluate(132.0, "low", FakeMonitor({"ECU": 13.0}))
check("回落 13 → re-arm（不触发）", a is None, f"got {a}")
a, r = pol.evaluate(140.0, "low", FakeMonitor({"ECU": 17.0}))
check("再超阈值开始计时 → 不立即触发", a is None, f"got {a}")
a, r = pol.evaluate(145.1, "low", FakeMonitor({"ECU": 17.0}))
check("持续 5s 后 → 再次触发", a == "up", f"got {a}")

# ======================================================================
print("== 3. 交叉点策略（模拟版）==")
crossover_cfg = {
    "crossover": {
        "margin_mvc": 1.5, "muscles": ["EDC", "ECU", "FDS", "UT"],
        "reference": {
            "800": {"EDC": 11.0, "ECU": 12.0, "FDS": 11.0, "UT": 8.0},
            "1200": {"EDC": 12.0, "ECU": 9.0, "FDS": 15.0, "UT": 5.0},
        },
    },
    "hold_sec": 5.0, "min_dwell_sec": 60.0, "hysteresis_mvc": 2.0,
}
pc = CrossoverPolicy(crossover_cfg)
a, r = pc.evaluate(0.0, "low", FakeMonitor({"ECU": 12.0}))
check("t=0 ECU=12（1200参考9，差3≥1.5）→ 需持续 5s", a is None, f"got {a}")
a, r = pc.evaluate(5.1, "low", FakeMonitor({"ECU": 12.0}))
check("t=5.1 → 交叉点升档", a == "up", f"got {a}")
a, r = pc.evaluate(66.0, "high", FakeMonitor({"FDS": 14.5}))
check("t=66 FDS=14.5 vs 800参考11 → 开始计时", a is None, f"got {a}")
a, r = pc.evaluate(71.1, "high", FakeMonitor({"FDS": 14.5}))
check("t=71.1 → 交叉点降档", a == "down", f"got {a}")

# ======================================================================
print("== 4. EMG 文本协议解析 ==")
check("JSON 行", parse_line('{"EDC": 11.2, "ECU": 12.5}') == {"EDC": 11.2, "ECU": 12.5})
check("空格键值对", parse_line("EDC 11.2 ECU 12.5") == {"EDC": 11.2, "ECU": 12.5})
check("逗号键值对", parse_line("EDC=11.2,ECU=12.5") == {"EDC": 11.2, "ECU": 12.5})
check("非法行 → None", parse_line("hello world x") is None)

# ======================================================================
print("== 5. 感度日志 ==")
_test_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp_logs")
shutil.rmtree(_test_log_dir, ignore_errors=True)
os.makedirs(_test_log_dir, exist_ok=True)
try:
    lg = SensLog(log_dir=_test_log_dir, prefix="sens_log")
    lg.row(elapsed=1.0, mode="time", event="BLOCK", block_idx=0,
           target_dpi=800, target_sens=1.0, applied_sens=1.0,
           effective_dpi=800, emg={"ECU": 12.3}, reason="测试")
    lg.close()
    import csv as _csv
    with open(lg.path, "r", encoding="utf-8-sig") as f:
        rows = list(_csv.reader(f))
    check("日志文件有表头+1行数据", len(rows) == 2, f"got {len(rows)}")
    check("表头含 effective_dpi 列", "effective_dpi" in rows[0])
finally:
    shutil.rmtree(_test_log_dir, ignore_errors=True)

# ======================================================================
print("== 6. Raw Accel 模板合并（v1.6 sens / v1.7 Output DPI）==")
tpl = {"whole": True, "sens": 1.0, "accel_mode": "off",
       "linear": {"accel": 0.01, "sens": 1.0}}
_deep_set(tpl, "sens", 1.5)
check("v1.6: 嵌套/顶层 sens 均被改写",
      tpl["sens"] == 1.5 and tpl["linear"]["sens"] == 1.5)
check("v1.6: accel_mode 未被改动", tpl["accel_mode"] == "off")
check("_find_value 可读回", _find_value(tpl, "sens") == 1.5)

tpl17 = {
    "version": "1.7.0",
    "defaultDeviceConfig": {},
    "profiles": [{
        "Output DPI": 1200.0,
        "Whole or horizontal accel parameters": {"mode": "noaccel"},
        "Vertical accel parameters": {"mode": "noaccel"},
    }],
}
# 1.0/1.5 版本：Sens Multiplier = Output DPI ÷ 1000 → 写 Output = 1000×sens
check("v1.7 映射: sens=1.0 → Output DPI 1000", sens_to_output_dpi(1.0) == 1000.0)
check("v1.7 映射: sens=1.5 → Output DPI 1500", sens_to_output_dpi(1.5) == 1500.0)
payload = build_v17_payload(tpl17, sens_to_output_dpi(1.0))
check("v1.7: sens=1.0 载荷 Output DPI = 1000",
      _find_value(payload, "Output DPI") == 1000.0)
payload2 = build_v17_payload(tpl17, sens_to_output_dpi(1.5))
check("v1.7: sens=1.5 载荷 Output DPI = 1500",
      _find_value(payload2, "Output DPI") == 1500.0)
check("Sens 显示 = 1000÷1000 = 1.0", 1000.0 / 1000.0 == 1.0)
check("Sens 显示 = 1500÷1000 = 1.5", 1500.0 / 1000.0 == 1.5)
check("v1.7: mode 强制 noaccel", _find_value(payload, "mode") == "noaccel")

# ======================================================================
print("== 7. 会话复位（进度归零、历史清空）==")
import time as _time
from controller import DPIController
ctrl = DPIController(overrides={
    "rawaccel": {"dry_run": True}, "time_scale": 50.0,
    "session_cap_sec": 600,
    "time_mode": {"pattern_dpi": [800, 1200], "block_sec": 300, "repeats": 1,
                  "ramp": {"sec": 60, "placement": "lead_in"}}})
_log_path = ctrl.start("time")
_time.sleep(2.5)   # 约 125 模拟秒
el1 = ctrl.status_snapshot()["elapsed"]
check("复位前 elapsed > 0", el1 > 10, f"got {el1}")
ok = ctrl.reset_session()
check("reset_session 返回 True（运行中）", ok)
st = ctrl.status_snapshot()
check("复位后 elapsed 归零", st["elapsed"] < 5, f"got {st['elapsed']}")
check("复位后 history 清空", len(st["history"]) <= 5,
      f"got {len(st['history'])}")
_time.sleep(1.0)
st = ctrl.status_snapshot()
check("复位后重新计时（elapsed 再次增长）", 0 < st["elapsed"] < 80,
      f"got {st['elapsed']}")
ctrl.stop()
ok = ctrl.reset_session()
check("停止后 reset_session 仍可清零显示状态", ok)
st = ctrl.status_snapshot()
check("停止后复位：elapsed 清零", st["elapsed"] == 0.0)
try:
    import os as _os
    _os.remove(_log_path)
except OSError:
    pass

# ======================================================================
print("== 8. 无会话手动切换 ==")
ctrl2 = DPIController(overrides={"rawaccel": {"dry_run": True}})
ctrl2.manual_switch("high", ramp_sec=0.0)
st = ctrl2.status_snapshot()
check("无会话手动切到 1200", abs(st["applied_sens"] - 1.5) < 1e-9,
      f"got {st['applied_sens']}")
check("无会话时档位更新为 high", st["current_level"] == "high")
check("无会话时不创建日志", st["log_path"] is None)
check("无会话时未启动会话", not st["running"] and st["mode"] is None)
ctrl2.manual_switch("low", ramp_sec=0.0)
st = ctrl2.status_snapshot()
check("无会话手动切回 800", abs(st["applied_sens"] - 1.0) < 1e-9)
_p2 = ctrl2.start("time")
ctrl2.stop()
try:
    import os as _os2
    _os2.remove(_p2)
except OSError:
    pass
check("手动切换后仍可正常开始会话", _p2 is not None)

# ======================================================================
print("== 9. 稳定块内时间线连续（每秒有点，曲线不断）==")
ctrl3 = DPIController(overrides={
    "rawaccel": {"dry_run": True}, "time_scale": 50.0,
    "session_cap_sec": 300,
    "time_mode": {"pattern_dpi": [800, 1200], "block_sec": 200, "repeats": 1,
                  "ramp": {"sec": 30, "placement": "lead_in"}}})
_p3 = ctrl3.start("time")
_time.sleep(1.2)   # 块内（无写入发生）
h1 = ctrl3.status_snapshot()["history"]
_time.sleep(1.2)
h2 = ctrl3.status_snapshot()["history"]
check("块内时间线点持续增长", len(h2) > len(h1) >= 2,
      f"h1={len(h1)} h2={len(h2)}")
check("块内点的时间戳递增", all(b[0] > a[0] for a, b in zip(h2, h2[1:])))
ctrl3.stop()
try:
    import os as _os3
    _os3.remove(_p3)
except OSError:
    pass

# ======================================================================
print("== 10. 下降モード（1200→800，PPT8 第7页设想）==")
eng_fall = ScheduleEngine(pattern_dpi=[1200, 800], block_sec=1800, ramp_sec=300,
                          ramp_placement="lead_in", repeats=1, base_dpi=800,
                          cap_sec=3600)
st = eng_fall.state_at(0.5)
check("下降模式: t=0 → 1200 DPI (sens 1.5)",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng_fall.state_at(1810.0)
check("下降模式: t=30:10 → 渐变中（1.5→1.0）",
      st["phase"] == "ramp" and abs(st["applied_sens"] - 1.5 + 0.5 * 10 / 300) < 1e-6)
st = eng_fall.state_at(2100.0)
check("下降模式: t=35:00 → 800 DPI (sens 1.0)",
      st["phase"] == "block" and abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_fall.state_at(3600.0)
check("下降模式: t=60:00 → 会话结束", st["finished"])
check("下降模式渐变计划在 1800s", len(eng_fall.transitions()) == 1
      and abs(eng_fall.transitions()[0][0] - 1800) < 1e-6)

# 会话开始立即应用首块 sens（下降模式从 1200 开始，不先写 800）
ctrl4 = DPIController(overrides={
    "rawaccel": {"dry_run": True},
    "session_cap_sec": 3600,
    "time_mode": {"pattern_dpi": [1200, 800], "block_sec": 1800, "repeats": 1,
                  "ramp": {"sec": 300, "placement": "lead_in"}}})
_p4 = ctrl4.start("time")
_time.sleep(0.3)
st = ctrl4.status_snapshot()
check("下降模式会话开始即 1200", abs(st["applied_sens"] - 1.5) < 1e-9,
      f"got {st['applied_sens']}")
ctrl4.stop()
try:
    import os as _os4
    _os4.remove(_p4)
except OSError:
    pass

# ======================================================================
print("== 11. 上升+下降合并模式（一条曲线 800→1200→800）==")
eng_both = ScheduleEngine(pattern_dpi=[800, 1200, 800], block_sec=1800,
                          ramp_sec=300, ramp_placement="lead_in", repeats=1,
                          base_dpi=800, cap_sec=5400)
st = eng_both.state_at(0.5)
check("合并: t=0 → 800", abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_both.state_at(1810.0)
check("合并: t=30:10 → 上升渐变中", st["phase"] == "ramp"
      and abs(st["applied_sens"] - (1.0 + 0.5 * 10 / 300)) < 1e-6)
st = eng_both.state_at(2100.0)
check("合并: t=35:00 → 1200", abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng_both.state_at(3610.0)
check("合并: t=60:10 → 下降渐变中", st["phase"] == "ramp"
      and abs(st["applied_sens"] - (1.5 - 0.5 * 10 / 300)) < 1e-6)
st = eng_both.state_at(3900.0)
check("合并: t=65:00 → 回到 800", abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_both.state_at(4000.0)
check("合并: t>65:00 保持 800 且未结束", not st["finished"]
      and abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_both.state_at(5400.0)
check("合并: t=90:00 → 会话结束", st["finished"])
check("合并渐变计划 2 次（1800s/3600s）", len(eng_both.transitions()) == 2
      and abs(eng_both.transitions()[0][0] - 1800) < 1e-6
      and abs(eng_both.transitions()[1][0] - 3600) < 1e-6)

# ======================================================================
print("== 12. 独立升降时间（block_durs：上升/下降时间点各自设定）==")
eng_v = ScheduleEngine(pattern_dpi=[800, 1200, 800], block_sec=1800,
                       ramp_sec=300, ramp_placement="lead_in", repeats=1,
                       base_dpi=800, cap_sec=6300,
                       block_durs=[1800, 3600, 900])
st = eng_v.state_at(0.5)
check("独立时间: t=0 → 800", abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_v.state_at(1810.0)
check("独立时间: t=30:10 → 上升渐变（30 分处）", st["phase"] == "ramp")
st = eng_v.state_at(2100.0)
check("独立时间: t=35:00 → 1200", abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng_v.state_at(4000.0)
check("独立时间: t=66:40 → 仍 1200（下降未到）", abs(st["applied_sens"] - 1.5) < 1e-9)
st = eng_v.state_at(5410.0)
check("独立时间: t=90:10 → 下降渐变（90 分处）", st["phase"] == "ramp"
      and abs(st["applied_sens"] - (1.5 - 0.5 * 10 / 300)) < 1e-6)
st = eng_v.state_at(5700.0)
check("独立时间: t=95:00 → 800", abs(st["applied_sens"] - 1.0) < 1e-9)
st = eng_v.state_at(6300.0)
check("独立时间: t=105:00 → 会话结束", st["finished"])
check("独立时间渐变计划在 1800s/5400s", len(eng_v.transitions()) == 2
      and abs(eng_v.transitions()[0][0] - 1800) < 1e-6
      and abs(eng_v.transitions()[1][0] - 5400) < 1e-6)

# ======================================================================
print()
print(f"结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
