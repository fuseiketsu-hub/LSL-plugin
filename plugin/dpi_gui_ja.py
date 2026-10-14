# -*- coding: utf-8 -*-
"""
dpi_gui_ja.py — Dynamic DPI Plugin GUI（日本語版，Tkinter，標準ライブラリのみ）
=============================================================================
英語版 dpi_gui.py と同じ機能・レイアウトで、UI 文字列を日本語にしたもの。
コントローラーは ui.lang=ja で起動し、ログメッセージも日本語で表示される。

レイアウト:
  ヘッダー: タイトル + ドライバーバックエンド + 状態バッジ
  左: 1) 実験時間ウィンドウ  2) オプション  3) セッション制御  4) 手動切替
  右: 実効 DPI · セッション進捗 · DPI タイムライン · 実行ログ
  フッター: ヒント + セッション概要

実行: python dpi_gui_ja.py
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

# ---------------------------------------------------------------- Japanese UI strings
L = {
    "title": "動的 DPI プラグイン — Raw Accel 感度スケジューラー",
    "header_title": "動的 DPI プラグイン",
    "header_sub": "Raw Accel 感度スケジューリング · 負荷分散実験",
    "card_window": "① 実験時間ウィンドウ",
    "f_session": "セッション時間",
    "f_rise_at": "上昇開始時間",
    "f_fall_at": "下降開始時間",
    "f_ramp_len": "変化区間（ランプ長さ）",
    "unit_min": "分",
    "pat_fixed": "パターン: 800 → 1200 → 800 DPI（上昇・下降、1本の曲線）",
    "card_options": "② オプション",
    "chk_sim": "シミュレーションモード（ドライバーを書き換えない）",
    "lbl_timescale": "時間スケール",
    "card_control": "③ セッション制御",
    "btn_start": "▶ セッション開始",
    "btn_stop": "■ セッション停止",
    "btn_reset": "⟲ リセット",
    "card_manual": "④ 手動切替（Raw Accel に即書き込み）",
    "manual_hint": "セッション内外で即時反映（Raw Accel に即書き込み）",
    "card_dpi": "実効 DPI",
    "card_progress": "セッション進捗",
    "lbl_timeline": "DPI タイムライン",
    "lbl_height": "高さ",
    "btn_reset2": "⟲ リセット",
    "card_log": "実行ログ",
    "btn_copy": "ログパスをコピー",
    "btn_folder": "ログフォルダを開く",
    "btn_clear": "クリア",
    "badge_fail": "⚠ ドライバー書込失敗",
    "badge_run": "● 実行中",
    "badge_ended": "終了",
    "badge_stopped": "停止済み",
    "badge_idle": "未開始",
    "lbl_backend": "バックエンド {bk} · ログ {log}",
    "lbl_no_session": "セッションなし",
    "chip_high": "1200 高",
    "chip_low": "800 低",
    "phase": {"idle": "待機", "block": "ブロック内", "ramp": "ランプ中",
              "emg": "EMG 待受中", "session_end": "終了", "stopped": "停止"},
    "next_switch": "次回切替 {min} 分後",
    "hint": "ヒント：マウス DPI=800 · ポインタ精度を無効化 · 手動切替は即時反映 · "
           "「⟲ リセット」で進捗をやり直し",
    "stats": "時間スケール ×{ts} · バックエンド {bk} · 曲線点数 {n}",
    "err_num": "[入力エラー] 時間ウィンドウの値は数値（分）で入力してください。",
    "err_total": "[入力エラー] セッション時間は 0 より大きい必要があります。",
    "err_order": "[入力エラー] 0 ≤ 上昇開始時間 < 下降開始時間 < セッション時間 が必要です。",
    "err_ramp_neg": "[入力エラー] 変化区間は負の値にできません。",
    "err_rise_early": "[入力エラー] 上昇ランプは下降開始前に終わる必要があります（上昇開始＋変化区間 ≤ 下降開始）。",
    "err_fall_early": "[入力エラー] 下降ランプはセッション終了前に終わる必要があります（下降開始＋変化区間 ≤ セッション時間）。",
    "plan": "[計画] {total:g} 分：{low:.0f} → {rise:g} 分から上昇（変化 {len:g} 分）→ {high:.0f} → {fall:g} 分から下降（変化 {len2:g} 分）→ {low2:.0f} DPI（上昇・下降）。",
    "err_run": "[エラー] {e}",
    "copied": "[コピー済み] {path}",
    "hint_folder": "[ヒント] フォルダを開けません: {e}",
    "unit_min2": "分",
}


# ---------------------------------------------------------------- widgets
class FlatButton(tk.Button):
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
        root.title(L["title"])
        root.configure(bg=BG)
        root.geometry("1080x760")
        root.minsize(900, 640)

        # 日本語ログ用に ui.lang=ja でコントローラーを起動
        self.ctrl = DPIController(config_path=config_path,
                                  overrides={"ui": {"lang": "ja"}})
        self._last_msg_text = None
        self._chart_reset_elapsed = 0.0
        self._last_log_path = None
        self._draw_key = None
        self._build()
        self._refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ build
    def _build(self):
        header = tk.Frame(self.root, bg=ACCENT, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=L["header_title"], bg=ACCENT, fg="white",
                 font=(FONT, 15, "bold")).pack(side="left", padx=16, pady=6)
        tk.Label(header, text=L["header_sub"], bg=ACCENT, fg="#dbeafe",
                 font=(FONT, 9)).pack(side="left")
        self.badge = Badge(header, "#94a3b8", "white")
        self.badge.pack(side="right", padx=14, pady=6)
        self.lbl_backend = tk.Label(header, text="", bg=ACCENT, fg="#dbeafe",
                                    font=(FONT, 9))
        self.lbl_backend.pack(side="right", padx=6)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        left = tk.Frame(body, bg=BG, width=330)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        # 1) 実験時間ウィンドウ
        c = Card(left, title=L["card_window"])
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        b.grid_columnconfigure(1, weight=1)
        self.var_total = tk.StringVar(value="120")
        self.var_rise_at = tk.StringVar(value="30")
        self.var_fall_at = tk.StringVar(value="90")
        self.var_ramp_len = tk.StringVar(value="5")
        field_row(b, L["f_session"], self.var_total, unit=L["unit_min"])
        field_row(b, L["f_rise_at"], self.var_rise_at, unit=L["unit_min"])
        field_row(b, L["f_fall_at"], self.var_fall_at, unit=L["unit_min"])
        field_row(b, L["f_ramp_len"], self.var_ramp_len, unit=L["unit_min"])
        tk.Label(b, text=L["pat_fixed"], bg=CARD, fg=MUTED,
                 font=(FONT, 8)).grid(row=4, column=0, columnspan=3,
                                      sticky="w", pady=(6, 0))

        # 2) オプション
        c = Card(left, title=L["card_options"])
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.dry_var = tk.BooleanVar(value=False)
        tk.Checkbutton(b, text=L["chk_sim"], variable=self.dry_var,
                       bg=CARD, activebackground=CARD, fg=SLATE,
                       font=(FONT, 9)).grid(row=0, column=0, sticky="w")
        tk.Label(b, text=L["lbl_timescale"], bg=CARD, fg=MUTED, font=(FONT, 9)
                 ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.scale_combo = ttk.Combobox(b, width=5, state="readonly",
                                        values=["1", "5", "10", "30", "60", "120"],
                                        font=(FONT, 9))
        self.scale_combo.set("1")
        self.scale_combo.grid(row=1, column=1, sticky="w", pady=(6, 0))

        # 3) セッション制御
        c = Card(left, title=L["card_control"])
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.btn_start = FlatButton(b, L["btn_start"], self._on_start,
                                    bg=ACCENT, bold=True, width=11)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = FlatButton(b, L["btn_stop"], self._on_stop,
                                   bg="#94a3b8", bold=True, width=11)
        self.btn_stop.grid(row=0, column=1)
        FlatButton(b, L["btn_reset"], self._on_chart_reset, bg="#e2e8f0",
                   fg=SLATE, size=9, bold=True, width=24).grid(
            row=1, column=0, columnspan=2, pady=(8, 0))

        # 4) 手動切替
        c = Card(left, title=L["card_manual"])
        c.pack(fill="x", pady=(0, 8))
        b = c.body()
        self.btn_m_low = FlatButton(b, "800 DPI", lambda: self._on_manual("low"),
                                    bg="#0ea5e9", width=10)
        self.btn_m_low.grid(row=0, column=0, padx=(0, 6))
        self.btn_m_high = FlatButton(b, "1200 DPI", lambda: self._on_manual("high"),
                                     bg="#8b5cf6", width=10)
        self.btn_m_high.grid(row=0, column=1)
        tk.Label(b, text=L["manual_hint"], bg=CARD, fg=MUTED,
                 font=(FONT, 8)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        toprow = tk.Frame(right, bg=BG)
        toprow.pack(fill="x")

        c = Card(toprow, title=L["card_dpi"])
        c.pack(side="left", fill="y", padx=(0, 8))
        b = c.body()
        self.lbl_dpi = tk.Label(b, text="800", bg=CARD, fg=ACCENT,
                                font=(MONO, 34, "bold"))
        self.lbl_dpi.pack(anchor="w")
        self.lbl_dpi_sub = tk.Label(b, text="sens 1.000 · 低", bg=CARD,
                                    fg=MUTED, font=(FONT, 9))
        self.lbl_dpi_sub.pack(anchor="w", pady=(0, 2))

        c = Card(toprow, title=L["card_progress"])
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

        # チャート
        chart_bar = tk.Frame(right, bg=BG)
        chart_bar.pack(fill="x", pady=(8, 0))
        tk.Label(chart_bar, text=L["lbl_timeline"], bg=BG, fg=SLATE,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(chart_bar, text=L["lbl_height"], bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(side="right", padx=(0, 2))
        self.chart_h_var = tk.StringVar(value=self._load_chart_height())
        self.chart_h_combo = ttk.Combobox(
            chart_bar, textvariable=self.chart_h_var, width=5,
            state="readonly",
            values=["180", "220", "260", "320", "360", "400"],
            font=(FONT, 8))
        self.chart_h_combo.pack(side="right", padx=(0, 8))
        self.chart_h_combo.bind("<<ComboboxSelected>>", self._on_chart_height)
        FlatButton(chart_bar, L["btn_reset2"], self._on_chart_reset,
                   bg="#e2e8f0", fg=SLATE, size=8).pack(side="right")
        try:
            init_h = int(self.chart_h_var.get())
        except ValueError:
            init_h = 260
        self.canvas = tk.Canvas(right, height=max(140, init_h), bg="white",
                                highlightthickness=1, highlightbackground=BORDER)
        self.canvas.pack(fill="x", pady=(4, 0))

        # 実行ログ
        c = Card(right, title=L["card_log"])
        c.pack(fill="both", expand=True, pady=(8, 0))
        b = c.body()
        toolbar = tk.Frame(b, bg=CARD)
        toolbar.pack(fill="x", pady=(0, 4))
        FlatButton(toolbar, L["btn_copy"], self._copy_log, bg="#e2e8f0",
                   fg=SLATE, size=8).pack(side="left", padx=(0, 6))
        FlatButton(toolbar, L["btn_folder"], self._open_log_dir, bg="#e2e8f0",
                   fg=SLATE, size=8).pack(side="left", padx=(0, 6))
        FlatButton(toolbar, L["btn_clear"], self._clear_log, bg="#e2e8f0",
                   fg=SLATE, size=8).pack(side="left")
        self.txt = tk.Text(b, height=8, bg=CONSOLE_BG, fg=CONSOLE_FG,
                           font=(MONO, 9), relief="flat", padx=8, pady=6,
                           wrap="word")
        self.txt.pack(fill="both", expand=True, pady=(0, 12))
        self.txt.configure(state="disabled")

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
        try:
            total = float(self.var_total.get())
            rise_at = float(self.var_rise_at.get())
            fall_at = float(self.var_fall_at.get())
            ramp_len = float(self.var_ramp_len.get())
        except ValueError:
            self._log_line(L["err_num"])
            return None
        if total <= 0:
            self._log_line(L["err_total"])
            return None
        if not (0 <= rise_at < fall_at < total):
            self._log_line(L["err_order"])
            return None
        if ramp_len < 0:
            self._log_line(L["err_ramp_neg"])
            return None
        if rise_at + ramp_len > fall_at:
            self._log_line(L["err_rise_early"])
            return None
        if fall_at + ramp_len > total:
            self._log_line(L["err_fall_early"])
            return None
        return total, rise_at, fall_at, ramp_len

    def _on_start(self):
        vals = self._read_window_fields()
        if vals is None:
            return
        total_min, rise_min, fall_min, ramp_len_min = vals
        low_dpi = float(self.ctrl.levels["low"]["dpi"])
        high_dpi = float(self.ctrl.levels["high"]["dpi"])

        # 1本の曲線：800 →(rise から上昇)→ 1200 →(fall から下降)→ 800
        time_mode = {
            "pattern_dpi": [low_dpi, high_dpi, low_dpi],
            "block_sec": rise_min * 60,          # プレースホルダ（block_durs 優先）
            "block_durs": [rise_min * 60,
                           (fall_min - rise_min) * 60,
                           (total_min - fall_min) * 60],
            "repeats": 1,
            "ramp": {"sec": ramp_len_min * 60, "placement": "lead_in"},
        }
        self._log_line(L["plan"].format(
            total=total_min, low=low_dpi, rise=rise_min, len=ramp_len_min,
            high=high_dpi, fall=fall_min, len2=ramp_len_min, low2=low_dpi))

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
            self._log_line(L["err_run"].format(e=e))

    def _on_stop(self):
        self.ctrl.stop(restore=True)

    def _on_chart_reset(self):
        if self.ctrl.reset_session():
            self._chart_reset_elapsed = 0.0

    # ---------------------------------------------------------------- UI state
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
        try:
            h = int(self.chart_h_var.get())
        except ValueError:
            return
        self.canvas.configure(height=max(140, h))
        self._save_chart_height()

    def _on_manual(self, level):
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
            self._log_line(L["copied"].format(path=st["log_path"]))

    def _open_log_dir(self):
        d = self.ctrl.log_dir
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)  # noqa: Windows
        except Exception as e:
            self._log_line(L["hint_folder"].format(e=e))

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

        if st["error"]:
            self.badge.configure(bg="#f59e0b", text=L["badge_fail"])
            self.lbl_backend.configure(text="")
        elif running:
            self.badge.configure(bg="#16a34a", text=L["badge_run"])
        elif st["phase"] in ("session_end",):
            self.badge.configure(bg="#3b82f6", text=L["badge_ended"])
        elif st["mode"]:
            self.badge.configure(bg="#94a3b8", text=L["badge_stopped"])
        else:
            self.badge.configure(bg="#94a3b8", text=L["badge_idle"])
        log_txt = (os.path.basename(st["log_path"])
                   if st["log_path"] else L["lbl_no_session"])
        self.lbl_backend.configure(
            text=L["lbl_backend"].format(bk=st["bridge_kind"], log=log_txt))

        self.btn_start.configure(state="normal" if not running else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")

        level = st["current_level"]
        self.lbl_dpi.configure(text=f"{st['effective_dpi']:.0f}")
        chip = L["chip_high"] if level == "high" else L["chip_low"]
        self.lbl_dpi_sub.configure(
            text=f"sens {st['applied_sens']:.3f} · {chip} · {st['phase']}")

        self.lbl_elapsed.configure(text=self._fmt_hms(st["elapsed"]))
        cap = max(st["cap_sec"], 1)
        self.lbl_cap.configure(text=f"/ {self._fmt_hms(cap)}")
        frac = min(100.0, st["elapsed"] / cap * 100)
        self.progress.configure(value=frac)
        nxt = ""
        if st["next_change"] is not None and running:
            remain = max(0, st["next_change"] - st["elapsed"])
            nxt = L["next_switch"].format(min=remain / 60)
        self.lbl_next.configure(
            text=f"{L['phase'].get(st['phase'], st['phase'])}"
                 f"{' · ' + nxt if nxt else ''}")

        if st["log_path"] and st["log_path"] != self._last_log_path:
            self._last_log_path = st["log_path"]
            self._chart_reset_elapsed = 0.0

        for text in st["messages"]:
            if text != self._last_msg_text:
                self._log_raw(text)
                self._last_msg_text = text

        draw_key = (int(st["elapsed"]), len(st["history"]), st["phase"],
                    st["mode"], self._chart_reset_elapsed,
                    id(self.ctrl.engine))
        if draw_key != self._draw_key:
            self._draw_key = draw_key
            self._draw(st)

        self.lbl_hint.configure(text=L["hint"])
        self.lbl_stats.configure(
            text=L["stats"].format(ts=self.ctrl.time_scale,
                                   bk=st["bridge_kind"],
                                   n=len(st["history"])))

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

        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = X(cap * frac)
            c.create_line(x, pt, x, h - pb, fill="#eef2f7")
            c.create_text(x, h - pb + 11, text=f"{frac * cap / 60:.0f}分",
                          fill=MUTED, font=(MONO, 8))
        for dpi, lab in ((800, "800"), (1200, "1200")):
            c.create_line(pl, Y(dpi), w - pr, Y(dpi), fill="#e2e8f0")
            c.create_text(pl - 9, Y(dpi), text=lab, anchor="e", fill=MUTED,
                          font=(MONO, 8))
        c.create_text(pl - 9, pt, text="DPI", anchor="e", fill=SLATE,
                      font=(MONO, 8, "bold"))

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
                              text=f"ランプ {f:.2f}→{to:.2f}",
                              fill="#b45309", font=(MONO, 8))

        hist = [(t, d) for t, d in st["history"]
                if t >= self._chart_reset_elapsed - 1e-9]
        if len(hist) > 1:
            step = max(1, len(hist) // 600)
            pts = []
            for t, d in hist[::step]:
                pts += [X(t), Y(d)]
            if pts:
                c.create_line(*pts, fill=ACCENT, width=2)

        x_cur = X(min(st["elapsed"], cap))
        c.create_line(x_cur, pt, x_cur, h - pb, fill=DANGER, dash=(2, 2), width=1)
        if hist:
            t, d = hist[-1]
            c.create_oval(X(t) - 4, Y(d) - 4, X(t) + 4, Y(d) + 4,
                          fill=ACCENT, outline="white", width=2)
            c.create_text(min(X(t) + 8, w - pr), Y(d) - 10, anchor="w",
                          text=f"{d:.0f} DPI", fill=ACCENT,
                          font=(MONO, 8, "bold"))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
