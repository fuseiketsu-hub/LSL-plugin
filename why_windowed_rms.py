# -*- coding: utf-8 -*-
"""为什么要用窗口算 RMS：把"估计稳定性"和"时间分辨率"的权衡量化出来。

跑法：python why_windowed_rms.py
"""
import numpy as np
from scipy import signal

FS = 1000.0
BAND = (20.0, 450.0)          # EMG 带通
rng = np.random.default_rng(0)


def make_emg(n_sec, levels):
    """按给定包络造一段 EMG：带通噪声 × 分段恒定包络。"""
    n = int(n_sec * FS)
    x = rng.standard_normal(n)
    b, a = signal.butter(4, BAND, btype="bandpass", fs=FS)
    x = signal.filtfilt(b, a, x)
    env = np.repeat(levels, int(n / len(levels)))[:n]
    if env.size < n:
        env = np.pad(env, (0, n - env.size), mode="edge")
    return x * env


def win_rms(x, win_sec, hop_sec):
    w = max(1, int(round(win_sec * FS)))
    h = max(1, int(round(hop_sec * FS)))
    n_out = 1 + (x.size - w) // h if x.size >= w else 0
    if n_out <= 0:
        return np.empty(0)
    idx = np.arange(n_out)[:, None] * h + np.arange(w)[None, :]
    return np.sqrt(np.mean(x[idx] ** 2, axis=1))


print("=" * 78)
print("一、为什么单看一个样本没有意义")
print("=" * 78)
x = make_emg(2.0, [1.0, 1.0])
print(f"  采样率 {FS:g} Hz，带通 {BAND[0]:g}–{BAND[1]:g} Hz")
print(f"  最低保留频率 {BAND[0]:g} Hz → 一个周期 = {1000/BAND[0]:.1f} ms")
print(f"  单样本看起来是 {x[100]:+.4f}、{x[101]:+.4f}、{x[102]:+.4f} … "
      "正负乱跳，幅值毫无意义")
print("  取 100 ms 窗口：")
for w in (0.005, 0.02, 0.125, 0.5):
    seg = x[:int(w * FS)]
    print(f"    窗长 {w*1000:6.1f} ms  ({w*BAND[0]:5.2f} 个最低频周期)  "
          f"RMS = {np.sqrt(np.mean(seg**2)):.4f}")

print("\n" + "=" * 78)
print("二、窗长 vs 估计噪声（包络恒定时，RMS 序列的相对波动）")
print("=" * 78)
x = make_emg(20.0, [1.0])                      # 恒定包络 → 理想情况 RMS 应完全平稳
print(f"  {'窗长':>8}  {'最低频周期数':>12}  {'RMS 相对波动 CV':>16}  {'10s 内样本数':>12}")
base = None
rows = []
for w in (0.005, 0.01, 0.02, 0.05, 0.125, 0.25, 0.5, 1.0, 2.0):
    r = win_rms(x, w, w / 2)
    valid = r[int(1.0 / (w / 2)):]              # 丢掉起始瞬态
    cv = float(np.std(valid) / np.mean(valid))
    rows.append((w, cv, valid.size))
    print(f"  {w*1000:6.1f} ms  {w*BAND[0]:12.2f}  {cv*100:15.2f}%  "
          f"{int(10/(w/2)):12d}")
print("\n  规律：CV ∝ 1/√窗长 —— 窗越长估计越稳，但见第三节的代价")

print("\n" + "=" * 78)
print("三、窗长 vs 时间分辨率（负荷阶跃的 10–90% 上升时间）")
print("=" * 78)


def rise_time(win_sec, hop_sec, n_rep=40):
    """包络从 0.10 阶跃到 0.60 时，RMS 序列的 10%→90% 上升时间（多次实现取中位）。"""
    out = []
    for _ in range(n_rep):
        x = make_emg(20.0, [0.10, 0.60])
        r = win_rms(x, win_sec, hop_sec)
        t = np.arange(r.size) * hop_sec + win_sec
        low = np.median(r[(t > 6.0) & (t < 9.5)])       # 阶跃前
        high = np.median(r[(t > 10.5) & (t < 14.0)])    # 阶跃后
        if not (high > low):
            continue
        lo_th = low + 0.10 * (high - low)
        hi_th = low + 0.90 * (high - low)
        # 只在阶跃之后的过渡区内找过阈点（否则阶跃前的噪声尖峰会被误判）
        span = max(1.5, 4.0 * win_sec)
        m = (t >= 10.0) & (t <= 10.0 + span)
        seg, tseg = r[m], t[m]
        if seg.size < 3:
            continue
        i_lo = int(np.argmax(seg >= lo_th))
        i_hi = int(np.argmax(seg >= hi_th))
        if i_hi > i_lo:
            out.append((tseg[i_hi] - tseg[i_lo]))
    return float(np.median(out)) if out else float("nan")


print(f"  负荷阶跃 0.10 → 0.60（t = 10 s），按实测稳态电平定 10% / 90% 阈值")
print(f"  {'窗长':>9}  {'上升时间':>10}  {'理论 0.8×窗长':>14}  {'说明':>22}")
for w in (0.005, 0.02, 0.05, 0.125, 0.25, 0.5, 1.0, 2.0):
    rt = rise_time(w, w / 2)
    note = ("跟得上秒级交替" if rt < 0.4 else
            "尚可" if rt < 1.0 else "会糊掉交替")
    print(f"  {w*1000:6.1f} ms  {rt*1000:8.0f} ms  {0.8*w*1000:12.0f} ms"
          f"  {note:>22}")

print("\n" + "=" * 78)
print("四、权衡总表（窗长的两端代价）")
print("=" * 78)
print(f"  {'窗长':>9}  {'估计波动 CV':>12}  {'上升时间':>10}  {'每秒样本':>9}")
for w in (0.005, 0.02, 0.05, 0.125, 0.25, 0.5, 1.0, 2.0):
    x = make_emg(20.0, [1.0])
    r = win_rms(x, w, w / 2)
    valid = r[int(1.0 / (w / 2)):]
    cv = float(np.std(valid) / np.mean(valid))
    rt = rise_time(w, w / 2, n_rep=20)
    print(f"  {w*1000:6.1f} ms  {cv*100:11.2f}%  {rt*1000:8.0f} ms  {1/(w/2):9.0f}")
print("\n  窗太短 → CV 大（幅值序列抖，APDF 分位数不稳）")
print("  窗太长 → 上升时间长（负荷变化被抹平，抓不到肌肉交替）")
print("  0.125 s 是这两端的折中：CV ~7%、上升时间 ~0.1 s")

print("\n" + "=" * 78)
print("五、结论：0.125 s / hop 0.0625 s 的来由")
print("=" * 78)
w, hop = 0.125, 0.0625
print(f"  ① 跨过至少几个最低频周期：{w*BAND[0]:.1f} 个（20 Hz 周期 50 ms）")
print(f"     → 少于此则 RMS 反映的是振荡相位而非幅值")
print(f"  ② 估计波动：恒定负荷下 RMS 序列 CV ≈ {dict((r[0], r[1]) for r in rows)[w]*100:.1f}%")
print(f"     → APDF 的 P10/P50/P90 才稳定（不会因估计噪声而散开）")
print(f"  ③ 时间分辨率：负荷阶跃的过渡区 ≈ {w*1000:.0f} ms")
print(f"     → 肌肉交替是秒级现象，0.125 s 完全跟得上；2 s 窗会把它糊掉")
print(f"  ④ 重叠 50%（hop = {hop:g} s）：幅值序列 {1/hop:.0f} 点/秒")
print(f"     → 5 分钟决策窗内 ~{int(300/hop)} 个点，P10/P50/P90 分位数足够稳")
print(f"  ⑤ hop > window 会被拒绝（会漏数据）：实现里有显式校验")
