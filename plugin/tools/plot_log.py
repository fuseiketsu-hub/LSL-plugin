# -*- coding: utf-8 -*-
"""
plot_log.py — 绘制 sens_log_*.csv 的有效 DPI 时间序列
======================================================
优先使用 matplotlib；未安装时输出 ASCII 图。

用法：
  python tools/plot_log.py logs/sens_log_20260829_103000.csv [--out dpi.png]
"""

import argparse
import csv
import os
import sys


def load(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                t = float(r["elapsed_s"])
                dpi = float(r["effective_dpi"])
                rows.append((t, dpi))
            except (TypeError, ValueError):
                continue
    return rows


def ascii_plot(rows, width=100, height=20):
    if not rows:
        print("无数据")
        return
    t_min, t_max = rows[0][0], rows[-1][0]
    dpis = [d for _, d in rows]
    d_min, d_max = min(dpis) - 20, max(dpis) + 20
    grid = [[" "] * width for _ in range(height)]
    for t, d in rows:
        x = int((t - t_min) / max(t_max - t_min, 1) * (width - 1))
        y = int((1 - (d - d_min) / (d_max - d_min)) * (height - 1))
        grid[y][x] = "#"
    print(f"有效 DPI 时间序列（x: 0–{t_max/60:.0f} 分钟，y: {d_min:.0f}–{d_max:.0f} DPI）")
    for line in grid:
        print("".join(line))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="sens_log csv 路径")
    ap.add_argument("--out", default=None, help="保存 PNG 路径（需 matplotlib）")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        print(f"文件不存在: {args.csv}")
        return 1
    rows = load(args.csv)
    print(f"共 {len(rows)} 个采样点。")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ts = [t / 60 for t, _ in rows]
        ds = [d for _, d in rows]
        plt.figure(figsize=(12, 4))
        plt.plot(ts, ds, linewidth=1.2)
        plt.xlabel("会话时间（分钟）")
        plt.ylabel("有效 DPI")
        plt.title("sens_log 有效 DPI 时间序列")
        plt.grid(alpha=0.3)
        if args.out:
            plt.savefig(args.out, dpi=150)
            print(f"已保存 {args.out}")
        else:
            plt.show()
        return 0
    except ImportError:
        ascii_plot(rows)
        return 0


if __name__ == "__main__":
    sys.exit(main())
