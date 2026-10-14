# -*- coding: utf-8 -*-
"""
scheduler.py — 时间驱动调度引擎（纯逻辑，无 I/O，可离线推演与单元测试）
=====================================================================
实现交接文档 §4.2 / §7.2.3 的实验版时间调度：

- 块模式：pattern_dpi 按块循环，如 [800, 800, 1200, 1200] × 30 分钟；
- 线性渐变：DPI 变化时用 ramp_sec 秒线性插值（默认 300 s，Krakauer 2000/2004
  依据，见文献依据链文档），渐变期间按 tick 逐步写入（分步写入 writer）；
- 会话上限：达到 session_cap_sec（默认 7200 s = 2 小时）自动结束并复位到 1.0；
- 两档只改 sens：sens = dpi / base_dpi（800→1.0，1200→1.5），加速关闭。

ramp_placement:
  lead_in  渐变发生在新块的前 ramp_sec 秒（默认）
  lead_out 渐变发生在前一块的最后 ramp_sec 秒
"""


class ScheduleEngine:
    def __init__(self, pattern_dpi, block_sec, ramp_sec=300.0,
                 ramp_placement="lead_in", repeats=1, base_dpi=800.0,
                 cap_sec=None, sens_of=None, block_durs=None):
        if not pattern_dpi:
            raise ValueError("pattern_dpi 不能为空")
        if ramp_placement not in ("lead_in", "lead_out"):
            raise ValueError("ramp_placement 必须是 lead_in 或 lead_out")
        self.base_dpi = float(base_dpi)
        self.pattern_dpi = [float(d) for d in pattern_dpi]
        self.block_sec = float(block_sec)
        # block_durs：每块时长列表（与 pattern_dpi 等长）；不传则各块等长 block_sec
        if block_durs is None:
            self.block_durs = [self.block_sec] * len(self.pattern_dpi)
        else:
            durs = [float(d) for d in block_durs]
            if len(durs) != len(self.pattern_dpi):
                raise ValueError("block_durs 长度必须与 pattern_dpi 一致")
            self.block_durs = durs
        self.ramp_sec = float(ramp_sec)
        self.placement = ramp_placement
        self.repeats = int(repeats)
        self.total_sec = self.repeats * sum(self.block_durs)
        # 会话上限可超过块总长：超过部分保持最后一块的 DPI 直到上限
        self.cap_sec = float(cap_sec) if cap_sec else self.total_sec
        if sens_of is None:
            sens_of = lambda dpi: float(dpi) / self.base_dpi

        # 块表：每块 {index, dpi, sens, start, end, ramp, ramp_from}
        self.blocks = []
        t = 0.0
        prev_sens = None
        idx = 0
        for _ in range(self.repeats):
            for dpi, dur in zip(self.pattern_dpi, self.block_durs):
                sens = sens_of(dpi)
                ramp = 0.0
                if (prev_sens is not None and abs(sens - prev_sens) > 1e-9
                        and self.ramp_sec > 0):
                    ramp = self.ramp_sec
                self.blocks.append({
                    "index": idx, "dpi": dpi, "sens": sens,
                    "start": t, "end": t + dur,
                    "ramp": ramp, "ramp_from": prev_sens,
                })
                t += dur
                prev_sens = sens
                idx += 1

    # ---------------------------------------------------------------- 状态查询
    def state_at(self, elapsed):
        """返回会话内 elapsed 秒时的状态字典。

        finished 状态下：applied_sens=1.0（已复位到基准 800 DPI）。
        """
        base = {
            "elapsed": elapsed, "finished": False, "phase": "idle",
            "block_idx": "", "target_dpi": "", "target_sens": "",
            "applied_sens": 1.0, "effective_dpi": self.base_dpi,
            "ramp_progress": None,
        }
        if elapsed >= self.cap_sec:
            base.update({
                "finished": True, "phase": "session_end",
                "block_idx": len(self.blocks),
            })
            return base

        # 超过块总长（但在会话上限内）→ 保持最后一块状态
        el = min(elapsed, self.total_sec - 1e-9)

        # 1) 是否处于某次切换的渐变窗口
        for b in self.blocks:
            if b["ramp"] <= 0 or b["ramp_from"] is None:
                continue
            if self.placement == "lead_in":
                w_start, w_end = b["start"], b["start"] + b["ramp"]
            else:
                w_start, w_end = b["start"] - b["ramp"], b["start"]
            if w_start <= el < w_end:
                frac = max(0.0, min(1.0, (el - w_start) / b["ramp"]))
                sens = b["ramp_from"] + (b["sens"] - b["ramp_from"]) * frac
                base.update({
                    "phase": "ramp",
                    "block_idx": b["index"],
                    "target_dpi": b["dpi"], "target_sens": b["sens"],
                    "applied_sens": sens,
                    "effective_dpi": self.base_dpi * sens,
                    "ramp_progress": frac,
                })
                return base

        # 2) 普通块
        for b in self.blocks:
            if b["start"] <= el < b["end"]:
                base.update({
                    "phase": "block",
                    "block_idx": b["index"],
                    "target_dpi": b["dpi"], "target_sens": b["sens"],
                    "applied_sens": b["sens"],
                    "effective_dpi": b["dpi"],
                    "ramp_progress": 1.0,
                })
                return base

        # 未命中（elapsed 略小于 0 或浮点边缘）→ 按会话结束处理
        base.update({"finished": True, "phase": "session_end",
                     "block_idx": len(self.blocks)})
        return base

    def next_change(self, elapsed):
        """下一个“事件”发生时间（块边界 / 渐变起止 / 会话结束）。"""
        candidates = [self.cap_sec]
        for b in self.blocks:
            candidates.append(b["start"])
            if b["ramp"] > 0:
                if self.placement == "lead_in":
                    candidates.append(b["start"] + b["ramp"])
                else:
                    candidates.append(b["start"])
        for c in sorted(candidates):
            if c > elapsed + 1e-9:
                return c
        return self.cap_sec

    # ---------------------------------------------------------------- 计划打印
    def transitions(self):
        """返回 [(时间, from_sens, to_sens, 目标DPI)] 渐变计划，供展示/核对。"""
        out = []
        for b in self.blocks:
            if b["ramp"] > 0 and b["ramp_from"] is not None:
                if self.placement == "lead_in":
                    t = b["start"]
                else:
                    t = b["start"] - b["ramp"]
                out.append((t, b["ramp_from"], b["sens"], b["dpi"]))
        return out

    def plan_text(self, lang="en"):
        uniform = all(abs(d - self.block_sec) < 1e-9 for d in self.block_durs)
        block_txt = (f"block {self.block_sec/60:.0f} min" if uniform
                     else "blocks " + "/".join(f"{d/60:.0f}" for d in self.block_durs) + " min")
        block_txt_ja = (f"ブロック {self.block_sec/60:.0f} 分" if uniform
                        else "ブロック " + "/".join(f"{d/60:.0f}" for d in self.block_durs) + " 分")
        if lang == "ja":
            lines = [f"合計 {self.total_sec/60:.0f} 分、セッション上限 {self.cap_sec/60:.0f} 分、"
                     f"{block_txt_ja}、ランプ {self.ramp_sec/60:.1f} 分（{self.placement}）"]
            for b in self.blocks:
                m0, m1 = b["start"] / 60, b["end"] / 60
                ramp = f", ランプ({b['ramp_from']:.2f}→{b['sens']:.2f})" if b["ramp"] > 0 else ""
                lines.append(f"  ブロック {b['index']:>2}: {m0:6.1f}–{m1:6.1f} 分  DPI={b['dpi']:.0f}  "
                             f"sens={b['sens']:.2f}{ramp}")
            return "\n".join(lines)
        lines = [f"Total {self.total_sec/60:.0f} min, session cap {self.cap_sec/60:.0f} min, "
                 f"{block_txt}, ramp {self.ramp_sec/60:.1f} min ({self.placement})"]
        for b in self.blocks:
            m0, m1 = b["start"] / 60, b["end"] / 60
            ramp = f", ramp({b['ramp_from']:.2f}→{b['sens']:.2f})" if b["ramp"] > 0 else ""
            lines.append(f"  block {b['index']:>2}: {m0:6.1f}–{m1:6.1f} min  DPI={b['dpi']:.0f}  "
                         f"sens={b['sens']:.2f}{ramp}")
        return "\n".join(lines)
