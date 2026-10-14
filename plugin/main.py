# -*- coding: utf-8 -*-
"""
main.py — 动态 DPI 插件命令行入口
==================================
用法示例（详见 README.md）：

  # 会话A：时间驱动 800,800,1200,1200 × 30 分钟 + 5 分钟渐变（2 小时）
  python main.py --mode time

  # 会话B：对照 800 固定 2 小时
  python main.py --mode fixed

  # EMG 阈值法（TCP 接收 %MVC；ECU≥16% 升、FDS≥20% 降）
  python main.py --mode emg --policy threshold --port 9000

  # EMG 交叉点法（模拟版，需先校准 reference）
  python main.py --mode emg --policy crossover

  # 无硬件演示（内置模拟 EMG，加速 60 倍，只记录不写驱动，可录屏）
  python main.py --mode demo --policy threshold --time-scale 60 --dry-run

  # 手动模式（stdin 输入 up/down/quit）
  python main.py --mode manual

  # 抓取本机 Raw Accel 当前设置生成模板
  python main.py --dump-template

  # 运行自检
  python main.py --check
"""

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from controller import DPIController  # noqa: E402


def build_parser():
    p = argparse.ArgumentParser(
        description="Dynamic DPI via Raw Accel parameter changes (time-driven / EMG-driven)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mode", choices=["time", "fixed", "manual", "emg", "demo"],
                   default="time",
                   help="time=session A (time-driven) fixed=control session B "
                        "manual=manual switch emg=TCP %%MVC-driven demo=built-in "
                        "simulated EMG")
    p.add_argument("--policy", choices=["threshold", "crossover", "balance"], default=None,
                   help="EMG switch policy (emg/demo modes); defaults to config")
    p.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"),
                   help="config file path")
    p.add_argument("--dry-run", action="store_true",
                   help="simulation mode, no real driver writes")
    p.add_argument("--time-scale", type=float, default=None,
                   help="time acceleration factor (demo: 60 → 2h becomes 2min)")
    p.add_argument("--cap", type=float, default=None,
                   help="session cap seconds (overrides config)")
    p.add_argument("--port", type=int, default=None, help="EMG TCP port (overrides config)")
    p.add_argument("--host", default=None, help="EMG TCP listen address (overrides config)")
    p.add_argument("--log-dir", default=None, help="log directory (overrides config)")
    p.add_argument("--mark", metavar="NOTE", default=None,
                   help="append a MARK entry to the current session log and exit")
    p.add_argument("--check", action="store_true", help="run self-tests")
    p.add_argument("--dump-template", action="store_true",
                   help="capture the local Raw Accel settings as a template and exit")
    p.add_argument("--show-plan", action="store_true",
                   help="print the time schedule plan and exit")
    p.add_argument("--read-driver", action="store_true",
                   help="read back the driver's current config (Raw Accel 1.7 Output DPI) and exit")
    return p


def main():
    args = build_parser().parse_args()

    if args.check:
        import subprocess
        rc = 0
        for test_file in ("test_scheduler.py", "test_balance_policy.py"):
            path = os.path.join(BASE_DIR, "tests", test_file)
            if not os.path.exists(path):
                continue
            r = subprocess.run([sys.executable, path]).returncode
            rc = rc or r
        sys.exit(rc)

    if args.dump_template:
        from rawaccel_bridge import dump_current_settings
        sys.exit(dump_current_settings())

    overrides = {}
    if args.dry_run:
        overrides["rawaccel"] = {"dry_run": True}
    if args.time_scale:
        overrides["time_scale"] = args.time_scale
    if args.cap:
        overrides["session_cap_sec"] = args.cap
    if args.port:
        overrides.setdefault("emg", {})["port"] = args.port
    if args.host:
        overrides.setdefault("emg", {})["host"] = args.host
    if args.log_dir:
        overrides["log_dir"] = args.log_dir

    ctrl = DPIController(config_path=args.config, overrides=overrides)

    if args.show_plan:
        from scheduler import ScheduleEngine
        tm = ctrl.cfg["time_mode"]
        ramp = tm.get("ramp", {})
        eng = ScheduleEngine(
            pattern_dpi=tm["pattern_dpi"], block_sec=float(tm["block_sec"]),
            ramp_sec=float(ramp.get("sec", 300)),
            ramp_placement=ramp.get("placement", "lead_in"),
            repeats=int(tm.get("repeats", 1)), base_dpi=ctrl.base_dpi,
            cap_sec=ctrl.cap_sec)
        print(eng.plan_text())
        return 0

    if args.read_driver:
        from rawaccel_bridge import (find_v17_install_dir, read_driver_config,
                                     _find_value)
        d = find_v17_install_dir(ctrl.cfg["rawaccel"].get("install_dir", ""))
        if not d:
            print("[Error] Raw Accel 1.7 install dir not found (writer.exe).")
            return 1
        try:
            cfg = read_driver_config(d)
            out_dpi = _find_value(cfg, "Output DPI")
            print(f"Current driver config (read from {d}):")
            print(f"  Output DPI = {out_dpi}")
            if out_dpi is not None:
                print(f"  Sens (GUI display = Output DPI ÷ 1000) "
                      f"= {float(out_dpi) / 1000:.2f}")
            print(f"  Accel mode = {_find_value(cfg, 'mode')}")
            print("  Full JSON:")
            import json as _json
            print(_json.dumps(cfg, ensure_ascii=False, indent=2))
            return 0
        except Exception as e:
            print(f"[Error] {e}")
            return 1

    if args.mark:
        ctrl.open_log_for_mark()
        ok = ctrl.mark(args.mark)
        with ctrl._lock:
            if ctrl.log:
                ctrl.log.close()
                ctrl.log = None
        print("Mark written." if ok else "Failed to write mark (no active session log).")
        return 0 if ok else 1

    policy = args.policy
    if policy is None:
        policy = ctrl.cfg["emg"].get("policy", "threshold")

    try:
        log_path = ctrl.start(args.mode, policy_name=policy)
        print(f"Session log: {log_path}")
        print("Press Ctrl+C to stop the session and restore 800 DPI.")

        if args.mode == "manual":
            _manual_shell(ctrl)
        else:
            ctrl.wait()

    except KeyboardInterrupt:
        print("\nInterrupted…")
    finally:
        ctrl.stop(restore=True)
        print("Session ended, restored 800 DPI (sens=1.0).")
    return 0


def _manual_shell(ctrl):
    print("Manual mode commands: up=switch to 1200  down=switch to 800  quit=exit")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except EOFError:
            break
        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "up":
            ctrl.manual_switch("high")
        elif cmd == "down":
            ctrl.manual_switch("low")
        elif cmd:
            print("Unknown command (up/down/quit)")


if __name__ == "__main__":
    sys.exit(main())
