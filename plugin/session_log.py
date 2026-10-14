# -*- coding: utf-8 -*-
"""
session_log.py — 感度时序列日志 sens_log_*.csv
================================================
每行记录一个时刻的“目标 DPI / 目标 sens / 实际写入 sens / 有效 DPI”以及
该时刻的 EMG 值（%MVC），可复现任意时刻的有效增益（文档 §7.2.3 要求）。

列：
unix_ms      Unix 毫秒时间戳（用于与 EMG/任务日志对齐同步）
elapsed_s    会话内经过秒数（模拟时间 = 真实时间 × time_scale）
iso          本地时间 ISO 字符串
mode         会话模式（time/fixed/manual/emg/demo）
event        事件名（SESSION_START/BLOCK/RAMP/SWITCH/EMERGENCY/MARK/SESSION_END...）
block_idx    块序号（时间模式下为 0..N-1）
target_dpi   目标 DPI（块目标；渐变期间为渐变终点 DPI）
target_sens  目标 sens（同上）
applied_sens 本次实际写入驱动的 sens
effective_dpi 有效 DPI = base_dpi × applied_sens
emg_EDC/emg_ECU/emg_FDS/emg_UT  各肌肉最新 %MVC（p50），无则为空
reason       事件原因说明（如 emg_threshold_up(ECU=16.4)）
"""

import csv
import datetime
import os
import threading
import time

COLUMNS = [
    "unix_ms", "elapsed_s", "iso", "mode", "event", "block_idx",
    "target_dpi", "target_sens", "applied_sens", "effective_dpi",
    "emg_EDC", "emg_ECU", "emg_FDS", "emg_UT", "reason",
]


def _fmt(v, nd=4):
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.{nd}f}"


class SensLog:
    def __init__(self, log_dir="logs", prefix="sens_log"):
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(log_dir, f"{prefix}_{stamp}.csv")
        self._lock = threading.Lock()
        self._f = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._w = csv.writer(self._f)
        self._w.writerow(COLUMNS)
        self._f.flush()

    def row(self, elapsed=None, mode="", event="", block_idx="",
            target_dpi="", target_sens="", applied_sens="", effective_dpi="",
            emg=None, reason=""):
        emg = emg or {}
        now = datetime.datetime.now()
        rec = [
            int(time.time() * 1000),
            _fmt(elapsed, 1) if elapsed is not None else "",
            now.isoformat(timespec="seconds"),
            mode, event,
            "" if block_idx == "" else str(block_idx),
            _fmt(target_dpi, 1), _fmt(target_sens, 4),
            _fmt(applied_sens, 4), _fmt(effective_dpi, 1),
            _fmt(emg.get("EDC"), 2), _fmt(emg.get("ECU"), 2),
            _fmt(emg.get("FDS"), 2), _fmt(emg.get("UT"), 2),
            reason,
        ]
        with self._lock:
            self._w.writerow(rec)
            self._f.flush()

    def close(self):
        with self._lock:
            try:
                self._f.close()
            except Exception:
                pass
