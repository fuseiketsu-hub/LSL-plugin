# -*- coding: utf-8 -*-
"""
mock_emg_stream.py — 独立模拟 EMG 数据流（演示用）
====================================================
向插件内置的 EMG TCP 服务器发送 %MVC JSON 行，演示阈值法/交叉点法切换，
无需任何实验硬件。

用法：
  python tools/mock_emg_stream.py --host 127.0.0.1 --port 9000

可选 --watch-log logs：读取最新 sens_log_*.csv 判断当前档位，
形成“低DPI→ECU↑ / 高DPI→FDS↑”的负荷转移闭环（更贴近真实演示）。
"""

import argparse
import csv
import glob
import json
import math
import os
import random
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class LevelFollower:
    """从最新 sens_log 读取当前档位（applied_sens 列最后一行的值）。"""

    def __init__(self, log_dir, poll_every=1.0):
        self.log_dir = log_dir
        self.poll_every = poll_every
        self._level = "low"
        self._last_check = 0.0
        self._last_file = None
        self._last_pos = 0

    def current(self):
        now = time.time()
        if now - self._last_check < self.poll_every:
            return self._level
        self._last_check = now
        files = sorted(glob.glob(os.path.join(self.log_dir, "sens_log_*.csv")))
        if not files:
            return self._level
        f = files[-1]
        if f != self._last_file:
            self._last_file, self._last_pos = f, 0
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                fh.seek(max(self._last_pos, 0))
                tail = fh.read(65536)
                self._last_pos = fh.tell()
            if not tail:
                return self._level
            lines = [l for l in tail.splitlines() if l.strip()]
            if len(lines) < 2:
                return self._level
            for row in csv.reader(lines[-3:]):
                if len(row) > 8:
                    try:
                        sens = float(row[7])  # applied_sens 列
                        self._level = "high" if sens > 1.2 else "low"
                    except ValueError:
                        pass
        except OSError:
            pass
        return self._level


def main():
    ap = argparse.ArgumentParser(description="模拟 EMG %MVC 数据流")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--interval", type=float, default=0.5, help="发送间隔（秒）")
    ap.add_argument("--watch-log", default=None, help="读取该目录最新 sens_log 形成闭环")
    args = ap.parse_args()

    follower = LevelFollower(args.watch_log) if args.watch_log else None
    state = {"EDC": 11.0, "ECU": 10.0, "FDS": 11.0, "UT": 7.0}
    up_rate, down_rate = 0.25, 0.15

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"连接 {args.host}:{args.port} …（插件 EMG 模式需先启动）")
    while True:
        try:
            sock.connect((args.host, args.port))
            break
        except OSError:
            time.sleep(1)

    print("已连接。发送 %MVC JSON 行，Ctrl+C 退出。")
    try:
        while True:
            level = follower.current() if follower else "low"
            if level == "low":
                state["ECU"] = min(state["ECU"] + up_rate * args.interval, 17.5)
                state["UT"] = min(state["UT"] + up_rate * 0.5 * args.interval, 11.0)
                state["FDS"] = max(state["FDS"] - down_rate * args.interval, 8.0)
            else:
                state["FDS"] = min(state["FDS"] + up_rate * args.interval, 22.0)
                state["ECU"] = max(state["ECU"] - down_rate * args.interval, 8.0)
                state["UT"] = max(state["UT"] - down_rate * 0.5 * args.interval, 5.0)
            state["EDC"] = max(8.0, min(15.0, state["EDC"] + random.uniform(-0.1, 0.1)))
            values = {k: max(0.0, round(v + random.uniform(-0.3, 0.3), 2))
                      for k, v in state.items()}
            line = json.dumps(values) + "\n"
            sock.sendall(line.encode("utf-8"))
            print(f"[{level}] {line.strip()}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
