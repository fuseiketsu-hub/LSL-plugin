# -*- coding: utf-8 -*-
"""
dpi_gui.py — Dynamic DPI Plugin GUI (English UI, Tkinter, stdlib only)

Layout:
  Header: title + driver backend + status badge
  Left:   1) Experiment Time Window (session duration / ramp start / ramp length)
          2) Options (simulation mode / time scale)
          3) Session Control (start / stop / reset progress)
          4) Manual Switch (800/1200, instant write to Raw Accel)
  Right:  Effective DPI card · Session Progress card
          DPI Timeline chart (block borders / ramp shading / cursor)
          Run Log (dark console)
  Footer: hints + session summary

Run: python dpi_gui.py   (or double-click 启动图形界面.bat)
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from controller import DPIController  # noqa: E402

# ---------------------------------------------------------------- colors
BG = "#eef1f6"
CARD = "#ffffff"
BORDER = "#e2e8f0"
ACCENT = "#2563eb"
TEXT = "#0f172a"
MUTED = "#64748b"
SLATE = "#334155"
DANGER = "#dc2626"
CONSOLE_BG = "#0f1419"
CONSOLE_FG = "#d7dde4"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


# ---------------------------------------------------------------- widgets
class FlatButton(tk.Button):
    """Flat button with hover effect."""

    def __init__(self, parent, text, command=None, bg=ACCENT, fg="white",
                 size=10, bold=False, **kw):
        super().__init__(parent, text=text, command=command, bg=bg, fg=fg,
                         relief="flat", bd=0, activebackground=bg,
                         activeforeground=fg, cursor="hand2",
                         font=(FONT, size, "bold" if bold else "normal"),
                         padx=10, pady=5, **kw)
        self._bg, self._hover = bg, self._darken(bg)
        self.bind("<Enter>", lambda e: self._on_hover(True))
        self.bind("<Leave>", lambda e: self._on_hover(False))

    def _on_hover(self, entering):
        if self["state"] == "disabled":
            return
        self.configure(bg=self._hover if entering else self._bg)

    @staticmethod
    def _darken(hex_color, factor=0.88):
        try:
            h = hex_color.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"
        except Exception:
            return hex_color


class Card(tk.Frame):
    """White card (border + title)."""

    def __init__(self, parent, title=None, bg=CARD, **kw):
        super().__init__(parent, bg=bg, highlightbackground=BORDER,
                         highlightthickness=1, bd=0, **kw)
        self.row = 0
        if title:
            tk.Label(self, text=title, bg=bg, fg=TEXT,
                     font=(FONT, 10, "bold")).grid(
                row=0, column=0, columnspan=12, sticky="w",
                padx=12, pady=(9, 2))
            self.row = 1

    def body(self):
        f = tk.Frame(self, bg=self["bg"])
        f.grid(row=self.row, column=0, columnspan=12, sticky="ew",
               padx=12, pady=(2, 10))
        return f


class Badge(tk.Label):
    def __init__(self, parent, bg, fg, **kw):
        super().__init__(parent, bg=bg, fg=fg, font=(FONT, 9, "bold"),
                         padx=8, pady=2, **kw)


def field_row(parent, label, var, width=8, unit=""):
    """One input row: label + entry + unit."""
    tk.Label(parent, text=label, bg=CARD, fg=SLATE,
             font=(FONT, 9)).grid(sticky="w", pady=3)
    e = tk.Entry(parent, textvariable=var, width=width, font=(FONT, 9),
                 justify="center")
    e.grid(row=parent.grid_size()[1] - 1, column=1, sticky="w", padx=(6, 2))
    if unit:
        tk.Label(parent, text=unit, bg=CARD, fg=MUTED, font=(FONT, 8)).grid(
            row=parent.grid_size()[1] - 1, column=2, sticky="w")


# ---------------------------------------------------------------- main app
class App:
    def __init__(self, root, config_path=None):
        self.root = root
        root.title("Dynamic DPI Plugin — Raw Accel Sensitivity Scheduler")
        root.configure(bg=BG)
        root.geometry("1080x760")
        root.minsize(900, 640)

        self.ctrl = DPIController(config_path=config_path)
        self._last_msg_text = None
        self._chart_reset_elapsed = 0.0   # only draw history after this time
        self._last_log_path = None
        self._draw_key = None             # chart redraw throttle key
        self._build()
        self._refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ build
    def _build(self):
        # ---- header ----
        header = tk.Frame(self.root, bg=ACCENT, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Dynamic DPI Plugin", bg=ACCENT, fg="white",
                 font=(FONT, 15, "bold")).pack(side="left", padx=16, pady=6)
        tk.Label(header, text="Raw Accel sensitivity scheduling · "
                              "load-distribution experiment",
                 bg=ACCENT, fg="#dbeafe", font=(FONT, 9)).pack(side="left")
        self.badge = Badge(header, "#94a3b8", "white")
        self.badge.pack(side="right", padx=14, pady=6)
        self.lbl_backend = tk.Label(header, text="", bg=ACCENT, fg="#dbeafe",
                                    font=(FONT, 9))
        self.lbl_backend.pack(side="right", padx=6)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # ---- left column ----
        left = tk.Frame(body, bg=BG, width=330)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        # 1) Experiment time window
        c = Card(left, title="Experiment Time Window")
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        b.grid_columnconfigure(1, weight=1)
        self.var_total = tk.StringVar(value="120")
        self.var_rise_at = tk.StringVar(value="30")
        self.var_fall_at = tk.StringVar(value="90")
        self.var_ramp_len = tk.StringVar(value="5")
        field_row(b, "Session duration", self.var_total, unit="min")
        field_row(b, "Rise start time", self.var_rise_at, unit="min")
        field_row(b, "Fall start time", self.var_fall_at, unit="min")
        field_row(b, "Ramp length", self.var_ramp_len, unit="min")
        self.lbl_dpi_hint = tk.Label(
            b, text="Pattern: 800 → 1200 → 800 DPI (rise & fall, one curve)",
            bg=CARD, fg=MUTED, font=(FONT, 8))
        self.lbl_dpi_hint.grid(row=4, column=0, columnspan=3, sticky="w")

        # 2) Options
        c = Card(left, title="Options")
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.dry_var = tk.BooleanVar(value=False)
        tk.Checkbutton(b, text="Simulation mode (no real driver writes)",
                       variable=self.dry_var, bg=CARD, activebackground=CARD,
                       fg=SLATE, font=(FONT, 9)).grid(row=0, column=0,
                                                      sticky="w")
        tk.Label(b, text="Time scale", bg=CARD, fg=MUTED, font=(FONT, 9)
                 ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.scale_combo = ttk.Combobox(b, width=5, state="readonly",
                                        values=["1", "5", "10", "30", "60", "120"],
                                        font=(FONT, 9))
        self.scale_combo.set("1")
        self.scale_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))

        # 3) Session control
        c = Card(left, title="Session Control")
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.btn_start = FlatButton(b, "▶  Start Session", self._on_start,
                                    bg=ACCENT, bold=True, width=11)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = FlatButton(b, "■  Stop Session", self._on_stop,
                                   bg="#94a3b8", bold=True, width=11)
        self.btn_stop.grid(row=0, column=1)
        FlatButton(b, "⟲ Reset Progress", self._on_chart_reset, bg="#e2e8f0",
                   fg=SLATE, size=9, bold=True, width=24).grid(
            row=1, column=0, columnspan=2, pady=(8, 0))

        # 4) Manual switch
        c = Card(left, title="Manual Switch (instant write to Raw Accel)")
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.btn_m_low = FlatButton(b, "800 DPI", lambda: self._on_manual("low"),
                                    bg="#0ea5e9", width=10)
        self.btn_m_low.grid(row=0, column=0, padx=(0, 6))
        self.btn_m_high = FlatButton(b, "1200 DPI", lambda: self._on_manual("high"),
                                     bg="#8b5cf6", width=10)
        self.btn_m_high.grid(row=0, column=1)
        tk.Label(b, text="Works in & out of session (applied immediately)",
                 bg=CARD, fg=MUTED, font=(FONT, 8)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # ---- right column ----
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        toprow = tk.Frame(right, bg=BG)
        toprow.pack(fill="x")

        # Effective DPI card
        c = Card(toprow, title="Effective DPI")
        c.pack(side="left", fill="y", padx=(0, 8))
        b = c.body()
        self.lbl_dpi = tk.Label(b, text="800", bg=CARD, fg=ACCENT,
                                font=(MONO, 34, "bold"))
        self.lbl_dpi.pack(anchor="w")
        self.lbl_dpi_sub = tk.Label(b, text="sens 1.000 · low", bg=CARD,
                                    fg=MUTED, font=(FONT, 9))
        self.lbl_dpi_sub.pack(anchor="w", pady=(0, 2))

        # Session progress card
        c = Card(toprow, title="Session Progress")
        c.pack(side="left", fill="both", expand=True)
        b = c.body()
        self.lbl_elapsed = tk.Label(b, text="00:00:00", bg=CARD, fg=TEXT,
                                    font=(MONO, 15, "bold"))
        self.lbl_elapsed.pack(side="left", padx=(0, 6))
        self.lbl_cap = tk.Label(b, text="/ 02:00:00", bg=CARD, fg=MUTED,
                                font=(MONO, 10))
        self.lbl_cap.pack(side="left")
        self.progress = ttk.Progressbar(b, maximum=100, length=120)
        self.progress.pack(side="right", padx=6, fill="x", expand=True)
        self.lbl_next = tk.Label(b, text="", bg=CARD, fg=MUTED, font=(FONT, 8))
        self.lbl_next.pack(anchor="w", pady=(2, 0))

        # Chart (fixed height + remembered height + reset button)
        chart_bar = tk.Frame(right, bg=BG)
        chart_bar.pack(fill="x", pady=(8, 0))
        tk.Label(chart_bar, text="DPI Timeline", bg=BG, fg=SLATE,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(chart_bar, text="Height", bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(side="right", padx=(0, 2))
        self.chart_h_var = tk.StringVar(value=self._load_chart_height())
        self.chart_h_combo = ttk.Combobox(
            chart_bar, textvariable=self.chart_h_var, width=5,
            state="readonly",
            values=["180", "220", "260", "320", "360", "400"],
            font=(FONT, 8))
        self.chart_h_combo.pack(side="right", padx=(0, 8))
        self.chart_h_combo.bind("<<ComboboxSelected>>", self._on_chart_height)
        FlatButton(chart_bar, "⟲ Reset", self._on_chart_reset,
                   bg="#e2e8f0", fg=SLATE, size=8).pack(side="right")
        try:
            init_h = int(self.chart_h_var.get())
        except ValueError:
            init_h = 260
        self.canvas = tk.Canvas(right, height=max(140, init_h), bg="white",
                                highlightthickness=1, highlightbackground=BORDER)
        self.canvas.pack(fill="x", pady=(4, 0))

        # Run log
        c = Card(right, title="Run Log")
        c.pack(fill="both", expand=True, pady=(8, 0))
        b = c.body()
        toolbar = tk.Frame(b, bg=CARD)
        toolbar.pack(fill="x", pady=(0, 4))
        FlatButton(toolbar, "Copy Log Path", self._copy_log, bg="#e2e8f0",
                   fg=SLATE, size=8).pack(side="left", padx=(0, 6))
        FlatButton(toolbar, "Open Log Folder", self._open_log_dir, bg="#e2e8f0",
                   fg=SLATE, size=8).pack(side="left", padx=(0, 6))
        FlatButton(toolbar, "Clear", self._clear_log, bg="#e2e8f0", fg=SLATE,
                   size=8).pack(side="left")
        self.txt = tk.Text(b, height=8, bg=CONSOLE_BG, fg=CONSOLE_FG,
                           font=(MONO, 9), relief="flat", padx=8, pady=6,
                           wrap="word")
        self.txt.pack(fill="both", expand=True, pady=(0, 12))
        self.txt.configure(state="disabled")

        # ---- footer ----
        foot = tk.Frame(self.root, bg="#1e293b")
        foot.pack(fill="x", side="bottom")
        self.lbl_hint = tk.Label(foot, text="", bg="#1e293b", fg="#94a3b8",
                                 font=(FONT, 8))
        self.lbl_hint.pack(side="left", padx=12, pady=3)
        self.lbl_stats = tk.Label(foot, text="", bg="#1e293b", fg="#cbd5e1",
                                  font=(MONO, 8))
        self.lbl_stats.pack(side="right", padx=12, pady=3)

    # ================================================================ events
    def _read_window_fields(self):
        """Read and validate the time window (total / rise / fall / ramp)."""
        try:
            total = float(self.var_total.get())
            rise_at = float(self.var_rise_at.get())
            fall_at = float(self.var_fall_at.get())
            ramp_len = float(self.var_ramp_len.get())
        except ValueError:
            self._log_line("[Input Error] Time window values must be numbers (minutes).")
            return None
        if total <= 0:
            self._log_line("[Input Error] Session duration must be greater than 0.")
            return None
        if not (0 <= rise_at < fall_at < total):
            self._log_line(
                "[Input Error] Need 0 ≤ rise start < fall start < session duration.")
            return None
        if ramp_len < 0:
            self._log_line("[Input Error] Ramp length cannot be negative.")
            return None
        if rise_at + ramp_len > fall_at:
            self._log_line(
                "[Input Error] The rise ramp must finish before the fall ramp starts "
                "(rise start + ramp length ≤ fall start).")
            return None
        if fall_at + ramp_len > total:
            self._log_line(
                "[Input Error] The fall ramp must finish before the session ends "
                "(fall start + ramp length ≤ session duration).")
            return None
        return total, rise_at, fall_at, ramp_len

    def _on_start(self):
        vals = self._read_window_fields()
        if vals is None:
            return
        total_min, rise_min, fall_min, ramp_len_min = vals
        low_dpi = float(self.ctrl.levels["low"]["dpi"])
        high_dpi = float(self.ctrl.levels["high"]["dpi"])

        # 一条曲线：800 →(rise 起渐变)→ 1200 →(fall 起渐变)→ 800
        time_mode = {
            "pattern_dpi": [low_dpi, high_dpi, low_dpi],
            "block_sec": rise_min * 60,          # 占位（block_durs 优先）
            "block_durs": [rise_min * 60,
                           (fall_min - rise_min) * 60,
                           (total_min - fall_min) * 60],
            "repeats": 1,
            "ramp": {"sec": ramp_len_min * 60, "placement": "lead_in"},
        }
        self._log_line(
            f"[Plan] {total_min:g} min: {low_dpi:.0f} → rise at {rise_min:g} min "
            f"(ramp {ramp_len_min:g} min) → {high_dpi:.0f} → fall at {fall_min:g} min "
            f"(ramp {ramp_len_min:g} min) → {low_dpi:.0f} DPI (rise & fall).")

        overrides = {
            "rawaccel": {"dry_run": bool(self.dry_var.get())},
            "time_scale": float(self.scale_combo.get()),
            "session_cap_sec": total_min * 60,
            "time_mode": time_mode,
        }
        try:
            self.ctrl.reconfigure(overrides)
            self.ctrl.start("time")
        except RuntimeError as e:
            self._log_line(f"[Error] {e}")

    def _on_stop(self):
        self.ctrl.stop(restore=True)

    def _on_chart_reset(self):
        """Reset: progress to zero, clear curve, restart schedule (also when idle)."""
        if self.ctrl.reset_session():
            self._chart_reset_elapsed = 0.0

    # ---------------------------------------------------------------- UI state persistence
    def _ui_state_path(self):
        return os.path.join(BASE_DIR, "ui_state.json")

    def _load_chart_height(self):
        try:
            with open(self._ui_state_path(), "r", encoding="utf-8") as f:
                import json as _json
                v = _json.load(f).get("chart_height", "260")
            int(v)
            return str(v)
        except Exception:
            return "260"

    def _save_chart_height(self):
        import json as _json
        try:
            with open(self._ui_state_path(), "w", encoding="utf-8") as f:
                _json.dump({"chart_height": self.chart_h_var.get()}, f)
        except Exception:
            pass

    def _on_chart_height(self, _event=None):
        """Set fixed chart height and remember it (restored next launch)."""
        try:
            h = int(self.chart_h_var.get())
        except ValueError:
            return
        self.canvas.configure(height=max(140, h))
        self._save_chart_height()

    def _on_manual(self, level):
        """Instant switch (no ramp); works in & out of session."""
        if self.ctrl.mode is None and not self.ctrl._running.is_set():
            try:
                self.ctrl.reconfigure({
                    "rawaccel": {"dry_run": bool(self.dry_var.get())},
                    "time_scale": float(self.scale_combo.get()),
                })
            except RuntimeError:
                pass
        self.ctrl.manual_switch(level, ramp_sec=0.0)

    def _copy_log(self):
        st = self.ctrl.status_snapshot()
        if st["log_path"]:
            self.root.clipboard_clear()
            self.root.clipboard_append(st["log_path"])
            self._log_line(f"[Copied] {st['log_path']}")

    def _open_log_dir(self):
        d = self.ctrl.log_dir
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)  # noqa: Windows
        except Exception as e:
            self._log_line(f"[Hint] Cannot open folder: {e}")

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _on_close(self):
        self.ctrl.stop(restore=True)
        self.root.destroy()

    # ================================================================ refresh
    def _log_line(self, text):
        import time as _t
        self.txt.configure(state="normal")
        self.txt.insert("end", f"[{_t.strftime('%H:%M:%S')}] {text}\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log_raw(self, text):
        """Controller messages already carry timestamps; insert verbatim."""
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _fmt_hms(self, sec):
        sec = int(max(0, sec))
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _refresh(self):
        st = self.ctrl.status_snapshot()
        running = st["running"]

        # ---- header ----
        if st["error"]:
            self.badge.configure(bg="#f59e0b", text="⚠ Driver Write Failed")
            self.lbl_backend.configure(text="")
        elif running:
            self.badge.configure(bg="#16a34a", text="● Running")
        elif st["phase"] in ("session_end",):
            self.badge.configure(bg="#3b82f6", text="Ended")
        elif st["mode"]:
            self.badge.configure(bg="#94a3b8", text="Stopped")
        else:
            self.badge.configure(bg="#94a3b8", text="Idle")
        self.lbl_backend.configure(
            text=f"backend {st['bridge_kind']} · log {os.path.basename(st['log_path']) if st['log_path'] else 'no session'}")

        # ---- button states ----
        self.btn_start.configure(state="normal" if not running else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")

        # ---- DPI ----
        level = st["current_level"]
        self.lbl_dpi.configure(text=f"{st['effective_dpi']:.0f}")
        chip = "1200 high" if level == "high" else "800 low"
        self.lbl_dpi_sub.configure(
            text=f"sens {st['applied_sens']:.3f} · {chip} · {st['phase']}")

        # ---- progress ----
        self.lbl_elapsed.configure(text=self._fmt_hms(st["elapsed"]))
        cap = max(st["cap_sec"], 1)
        self.lbl_cap.configure(text=f"/ {self._fmt_hms(cap)}")
        frac = min(100.0, st["elapsed"] / cap * 100)
        self.progress.configure(value=frac)
        nxt = ""
        if st["next_change"] is not None and running:
            remain = max(0, st["next_change"] - st["elapsed"])
            nxt = f"next switch in {remain/60:.1f} min"
        phase_en = {"idle": "Idle", "block": "In Block", "ramp": "Ramping",
                    "emg": "EMG Listening", "session_end": "Ended",
                    "stopped": "Stopped"}
        self.lbl_next.configure(
            text=f"{phase_en.get(st['phase'], st['phase'])}{' · ' + nxt if nxt else ''}")

        # ---- new session → reset chart baseline ----
        if st["log_path"] and st["log_path"] != self._last_log_path:
            self._last_log_path = st["log_path"]
            self._chart_reset_elapsed = 0.0

        # ---- log (UI-level dedup by last text) ----
        for text in st["messages"]:
            if text != self._last_msg_text:
                self._log_raw(text)
                self._last_msg_text = text

        # ---- chart (throttled redraw by data changes) ----
        draw_key = (int(st["elapsed"]), len(st["history"]), st["phase"],
                    st["mode"], self._chart_reset_elapsed,
                    id(self.ctrl.engine))
        if draw_key != self._draw_key:
            self._draw_key = draw_key
            self._draw(st)

        # ---- footer ----
        self.lbl_hint.configure(
            text="Hint: mouse DPI=800 · disable Windows pointer precision · "
                 "manual switch applies immediately · «⟲ Reset» restarts progress")
        self.lbl_stats.configure(
            text=f"time scale ×{self.ctrl.time_scale:g} · backend {st['bridge_kind']} · "
                 f"curve points {len(st['history'])}")

        self.root.after(200, self._refresh)

    # ================================================================ chart
    def _draw(self, st):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 300)
        h = max(c.winfo_height(), 100)
        pl, pr, pt, pb = 44, 14, 16, 26
        cap = max(st["cap_sec"], 1)
        base = self.ctrl.base_dpi
        y_lo, y_hi = base - 300, base + 600

        def X(t):
            return pl + (w - pl - pr) * (t / cap)

        def Y(d):
            return pt + (h - pt - pb) * (1 - (d - y_lo) / (y_hi - y_lo))

        # grid
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = X(cap * frac)
            c.create_line(x, pt, x, h - pb, fill="#eef2f7")
            c.create_text(x, h - pb + 11, text=f"{frac * cap / 60:.0f}min",
                          fill=MUTED, font=(MONO, 8))
        for dpi, lab in ((800, "800"), (1200, "1200")):
            c.create_line(pl, Y(dpi), w - pr, Y(dpi), fill="#e2e8f0")
            c.create_text(pl - 9, Y(dpi), text=lab, anchor="e", fill=MUTED,
                          font=(MONO, 8))
        c.create_text(pl - 9, pt, text="DPI", anchor="e", fill=SLATE,
                      font=(MONO, 8, "bold"))

        # block borders + ramp shading
        eng = self.ctrl.engine
        if eng is not None:
            for b in eng.blocks:
                x = X(b["start"])
                if x > w - pr + 2:
                    continue
                c.create_line(x, pt, x, h - pb, fill="#cbd5e1", dash=(3, 3))
                c.create_text(x + 3, pt + 1, text=f"{b['dpi']:.0f}", anchor="nw",
                              fill="#475569", font=(MONO, 8, "bold"))
            for (t, f, to, _dpi) in eng.transitions():
                x0, x1 = X(t), X(min(t + eng.ramp_sec, cap))
                if x0 > w - pr + 2:
                    continue
                c.create_rectangle(x0, pt, x1, h - pb, fill="#fef3c7", outline="")
                c.create_text((x0 + x1) / 2, h - pb - 6,
                              text=f"ramp {f:.2f}→{to:.2f}",
                              fill="#b45309", font=(MONO, 8))

        # history curve (downsampled; only after reset baseline)
        hist = [(t, d) for t, d in st["history"]
                if t >= self._chart_reset_elapsed - 1e-9]
        if len(hist) > 1:
            step = max(1, len(hist) // 600)
            pts = []
            for t, d in hist[::step]:
                pts += [X(t), Y(d)]
            if pts:
                c.create_line(*pts, fill=ACCENT, width=2)

        # current cursor
        x_cur = X(min(st["elapsed"], cap))
        c.create_line(x_cur, pt, x_cur, h - pb, fill=DANGER, dash=(2, 2), width=1)
        if hist:
            t, d = hist[-1]
            c.create_oval(X(t) - 4, Y(d) - 4, X(t) + 4, Y(d) + 4,
                          fill=ACCENT, outline="white", width=2)
            c.create_text(min(X(t) + 8, w - pr), Y(d) - 10, anchor="w",
                          text=f"{d:.0f} DPI", fill=ACCENT, font=(MONO, 8, "bold"))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
