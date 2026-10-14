"""实时界面：波形 + RMS/%MVE 负荷 + APDF 曲线 + 标定面板。

界面技术选型：**Tkinter + matplotlib(TkAgg)**，因为这两者随 Python 与
matplotlib 一起提供，无需再装 Qt/PySide —— 插件开箱即用。

线程模型
--------
* 采集线程（:class:`_AcquisitionThread`）只跑 ``session.step()``；
* 界面线程用 ``root.after(100ms)`` 定时取 :meth:`AcquisitionSession.snapshot`，
  快照内部已加锁并返回数组拷贝，不会与采集线程抢数据；
* 写盘在 ``SessionWriter`` 的第三个线程里，三者互不阻塞。

曲线读法（界面右下角的三个数字）
--------------------------------
``static`` 是**低**负荷水平（幅值分布 P10，几乎一直存在的那一档），
``peak`` 是**高**负荷水平（幅值分布 P90，偶尔出现的那一档）。
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
import traceback
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk

import numpy as np

import matplotlib

matplotlib.use("TkAgg")
from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .config import AppConfig
from .recording import export_apdf_result
from .session import AcquisitionSession, SessionSnapshot
from .sources import (
    PYLSL_AVAILABLE,
    LSLSource,
    SampleSource,
    StreamMeta,
    SyntheticProfile,
    SyntheticSource,
    discover_streams,
)

# 中文显示：Windows 上优先用雅黑/黑体，缺失时退回 DejaVu Sans（英文标签）
rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "Source Han Sans SC",
    "Noto Sans CJK SC",
    "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False

SYNTHETIC_ENTRY = "★ Synthetic signal (no hardware)"

__all__ = ["ApdfPluginApp", "launch"]


# --------------------------------------------------------------------------- #
class _AcquisitionThread(threading.Thread):
    """后台采集线程：只做 step()，异常记录下来交给界面线程展示。"""

    def __init__(self, session: AcquisitionSession):
        super().__init__(name="lsl-apdf-acquire", daemon=True)
        self.session = session
        self.error: str | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - 需要真实运行环境
        try:
            while not self._stop.is_set():
                self.session.step(timeout=0.05)
        except Exception:  # noqa: BLE001 - 任何异常都要让界面知道
            self.error = traceback.format_exc()


# --------------------------------------------------------------------------- #
class ApdfPluginApp:
    """主窗口。"""

    def __init__(self, root: Tk, cfg: AppConfig | None = None):
        self.root = root
        self.cfg = cfg or AppConfig()
        self.session: AcquisitionSession | None = None
        self.thread: _AcquisitionThread | None = None
        self._streams: list[StreamMeta] = []
        self._tick_count = 0
        self._last_error_shown = False
        self._apdf_curve_dirty = True

        root.title("OpenSignals LSL -> APDF realtime curve plugin")
        root.geometry("1360x860")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.root.after(200, self._refresh_streams)
        self.root.after(100, self._tick)

    # ================================================================== #
    # 界面构建
    # ================================================================== #
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:  # pragma: no cover - 非 Windows
            pass
        style.configure("Danger.TButton", foreground="#a00")

        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill=BOTH, expand=True)

        # ---------------- 顶部：连接栏 ---------------- #
        top = ttk.LabelFrame(outer, text="1. Connect to OpenSignals LSL stream", padding=6)
        top.pack(fill=X)

        ttk.Label(top, text="Stream:").pack(side=LEFT)
        self.stream_var = StringVar(value=SYNTHETIC_ENTRY)
        self.stream_combo = ttk.Combobox(
            top, textvariable=self.stream_var, width=62, state="readonly"
        )
        self.stream_combo.pack(side=LEFT, padx=4)
        ttk.Button(top, text="Refresh list", command=self._refresh_streams).pack(side=LEFT, padx=2)

        ttk.Label(top, text="Channel:").pack(side=LEFT, padx=(10, 0))
        self.channel_var = StringVar(value=str(self.cfg.signal.channel))
        ttk.Spinbox(
            top, from_=1, to=64, width=4, textvariable=self.channel_var
        ).pack(side=LEFT, padx=2)

        self.connect_btn = ttk.Button(top, text="Connect and start", command=self._toggle_connection)
        self.connect_btn.pack(side=LEFT, padx=8)
        self.synthetic_var = BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="Synthetic self-test", variable=self.synthetic_var
        ).pack(side=LEFT, padx=4)

        mid = ttk.Frame(outer)
        mid.pack(fill=BOTH, expand=True, pady=6)

        # ---------------- 左侧：图 ---------------- #
        plot_frame = ttk.Frame(mid)
        plot_frame.pack(side=LEFT, fill=BOTH, expand=True)

        self.fig = Figure(figsize=(8.6, 7.4), dpi=100, constrained_layout=True)
        gs = self.fig.add_gridspec(3, 1, height_ratios=[2.0, 1.5, 2.2])
        self.ax_raw = self.fig.add_subplot(gs[0])
        self.ax_rms = self.fig.add_subplot(gs[1], sharex=None)
        self.ax_apdf = self.fig.add_subplot(gs[2])

        self.ax_raw.set_title("Raw signal (DC-removed + band-pass filtered)")
        self.ax_rms.set_title("RMS and %MVE load series")
        self.ax_apdf.set_title("APDF curve (X = amplitude %MVE, Y = exceedance P%)")

        (self.line_raw,) = self.ax_raw.plot([], [], lw=0.6, color="#1f77b4")
        (self.line_rms,) = self.ax_rms.plot([], [], lw=1.1, color="#d62728", label="RMS")
        self.ax_rms.set_ylabel(f"RMS [{self.cfg.signal.unit_label}]")
        self.ax_rms.grid(alpha=0.25)
        (self.line_apdf,) = self.ax_apdf.plot([], [], lw=1.6, color="#2ca02c")
        self.ax_apdf.grid(alpha=0.25)
        self.markers = [
            self.ax_apdf.plot([], [], "o", ms=6, color=color)[0]
            for color in ("#1f77b4", "#ff7f0e", "#d62728")
        ]
        self.marker_labels = [
            self.ax_apdf.text(0, 0, "", fontsize=8) for _ in range(3)
        ]
        self.ax_apdf.set_xlim(0, max(10.0, self.cfg.apdf.max_level_pct))
        self.ax_apdf.set_ylim(0, 100)
        self.ax_apdf.set_xlabel("Amplitude level [%MVE]")
        self.ax_apdf.set_ylabel("Exceedance probability P (% of time above level)")
        self.ax_raw.set_ylabel(f"Amplitude [{self.cfg.signal.unit_label}]")
        self.ax_raw.grid(alpha=0.25)
        self.ax_raw.set_xlabel("Time [s]")
        self.ax_rms.set_xlabel("Time [s]")

        canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        NavigationToolbar2Tk(canvas, plot_frame).update()
        self.canvas = canvas

        if not PYLSL_AVAILABLE:
            self.ax_raw.set_title(
                "Raw signal -- pylsl not installed; only the synthetic self-test works (pip install pylsl)"
            )

        # ---------------- 右侧：控制面板 ---------------- #
        side = ttk.Frame(mid, width=380)
        side.pack(side=RIGHT, fill=Y, padx=(8, 0))

        self._build_calibration_panel(side)
        self._build_metrics_panel(side)
        self._build_export_panel(side)
        self._build_settings_panel(side)

        # ---------------- 底部：状态栏 ---------------- #
        self.status_var = StringVar(value="Ready")
        status = ttk.Label(
            outer, textvariable=self.status_var, relief="sunken", anchor="w", padding=3
        )
        status.pack(fill=X, side="bottom")

    # ------------------------------------------------------------------ #
    def _build_calibration_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="2. %MVE calibration (required)", padding=6)
        frame.pack(fill=X, pady=(0, 6))

        row = ttk.Frame(frame)
        row.pack(fill=X)
        ttk.Button(row, text="Start calibration", command=self._start_calibration).pack(
            side=LEFT, fill=X, expand=True, padx=1
        )
        ttk.Button(row, text="Next phase", command=self._skip_phase).pack(
            side=LEFT, fill=X, expand=True, padx=1
        )

        self.cal_phase_var = StringVar(value="Not started")
        ttk.Label(
            frame, textvariable=self.cal_phase_var, font=("", 10, "bold"), wraplength=340
        ).pack(fill=X, pady=(4, 2))

        self.cal_progress = ttk.Progressbar(frame, maximum=100.0)
        self.cal_progress.pack(fill=X)

        self.cal_detail_var = StringVar(value="Rest -> 3 MVC trials -> best plateau auto-picked")
        ttk.Label(frame, textvariable=self.cal_detail_var, wraplength=340, justify=LEFT).pack(
            fill=X, pady=2
        )

        self.trials_text = ttk.Treeview(
            frame, columns=("trial", "rms"), show="headings", height=3
        )
        self.trials_text.heading("trial", text="Trial")
        self.trials_text.heading("rms", text="Plateau RMS")
        self.trials_text.column("trial", width=60, anchor="center")
        self.trials_text.column("rms", width=140, anchor="e")
        self.trials_text.pack(fill=X, pady=3)

        row2 = ttk.Frame(frame)
        row2.pack(fill=X, pady=(4, 0))
        ttk.Label(row2, text="Manual MVE:").pack(side=LEFT)
        self.manual_mve_var = StringVar(value="")
        ttk.Entry(row2, textvariable=self.manual_mve_var, width=12).pack(side=LEFT, padx=2)
        ttk.Button(row2, text="Apply", command=self._apply_manual_mve).pack(side=LEFT)
        self.include_cal_var = BooleanVar(value=self.cfg.include_calibration_in_apdf)
        ttk.Checkbutton(
            frame,
            text="Include calibration in APDF (off by default)",
            variable=self.include_cal_var,
            command=self._apply_include_cal,
        ).pack(fill=X, pady=(4, 0))

    def _build_metrics_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="3. Realtime load metrics (%MVE)", padding=6)
        frame.pack(fill=X, pady=(0, 6))
        self.metric_vars = {
            "static": StringVar(value="static (low, P10): -"),
            "median": StringVar(value="median (P50): -"),
            "peak": StringVar(value="peak (high, P90): -"),
            "mean": StringVar(value="mean / max: -"),
            "mve": StringVar(value="MVE reference: -"),
            "cover": StringVar(value="APDF span / windows: -"),
        }
        for key in ("static", "median", "peak", "mean", "mve", "cover"):
            ttk.Label(frame, textvariable=self.metric_vars[key]).pack(fill=X, anchor="w")

        self.rms_var = StringVar(value="Current RMS: -")
        ttk.Label(frame, textvariable=self.rms_var, font=("", 10, "bold")).pack(
            fill=X, anchor="w", pady=(4, 0)
        )

    def _build_export_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="4. Realtime export", padding=6)
        frame.pack(fill=X, pady=(0, 6))
        self.export_path_var = StringVar(value=f"Output dir: {self.cfg.export.root_dir}\\<session>")
        ttk.Label(frame, textvariable=self.export_path_var, wraplength=340, justify=LEFT).pack(
            fill=X
        )
        row = ttk.Frame(frame)
        row.pack(fill=X, pady=4)
        ttk.Button(row, text="Open dir", command=self._open_export_dir).pack(
            side=LEFT, fill=X, expand=True, padx=1
        )
        ttk.Button(row, text="Export APDF snapshot", command=self._export_snapshot).pack(
            side=LEFT, fill=X, expand=True, padx=1
        )
        ttk.Button(row, text="Clear accumulator", command=self._reset_accumulator).pack(
            side=LEFT, fill=X, expand=True, padx=1
        )
        self.writer_var = StringVar(value="Writer: -")
        ttk.Label(frame, textvariable=self.writer_var).pack(fill=X)

    def _build_settings_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="5. Processing params (applied on reconnect)", padding=6)
        frame.pack(fill=X, pady=(0, 6))

        def row(label: str, value: str, width: int = 8):
            r = ttk.Frame(frame)
            r.pack(fill=X, pady=1)
            ttk.Label(r, text=label, width=18).pack(side=LEFT)
            var = StringVar(value=value)
            ttk.Entry(r, textvariable=var, width=width).pack(side=LEFT)
            return var

        self.param_vars = {
            "rms_window": row("RMS window [s]", f"{self.cfg.signal.rms_window_sec:g}"),
            "rms_hop": row("RMS hop [s]", f"{self.cfg.signal.rms_hop_sec:g}"),
            "skip": row("Startup skip [s]", f"{self.cfg.signal.startup_skip_sec:g}"),
            "hp": row("High-pass [Hz]", f"{self.cfg.signal.highpass_hz:g}"),
            "lp": row("Low-pass [Hz]", f"{self.cfg.signal.lowpass_hz:g}"),
            "notch": row("Notch [Hz]", f"{self.cfg.signal.notch_hz or 0:g}"),
            "display": row("Display window [s]", f"{self.cfg.display_window_sec:g}"),
        }
        ttk.Label(
            frame,
            text="Note: APDF is expressed in %MVE, unit conversion does not affect results;\n"
            "set notch to 0 to disable it.",
            wraplength=340,
            justify=LEFT,
        ).pack(fill=X, pady=(4, 0))

    # ================================================================== #
    # 连接
    # ================================================================== #
    def _refresh_streams(self) -> None:
        entries = [SYNTHETIC_ENTRY]
        self._streams = []
        if PYLSL_AVAILABLE:
            try:
                found = discover_streams(wait_time=0.6)
            except Exception as exc:
                found = []
                self.status_var.set(f"Failed to refresh stream list: {exc}")
            for meta in found:
                self._streams.append(meta)
                entries.append(
                    f"{meta.name} | type(MAC)={meta.type} | {meta.hostname} | "
                    f"{meta.n_channels}ch | {meta.nominal_srate:g}Hz"
                )
            if not found and not self.synthetic_var.get():
                self.status_var.set(
                    "No LSL streams found: enable INTEGRATIONS -> Lab Streaming Layer "
                    "in OpenSignals and press record; or use the synthetic self-test."
                )
        else:
            entries.append("(pylsl not installed: pip install pylsl)")
        self.stream_combo["values"] = entries
        if self.stream_var.get() not in entries:
            self.stream_var.set(entries[0])

    def _selected_meta(self) -> StreamMeta | None:
        label = self.stream_var.get()
        if label == SYNTHETIC_ENTRY:
            return None
        try:
            return self._streams[self.stream_combo.current() - 1]
        except Exception:
            return None

    def _toggle_connection(self) -> None:
        if self.session is not None:
            self._disconnect()
        else:
            self._connect()

    def _apply_params(self) -> None:
        """把界面上的参数写回配置（在建立会话之前调用）。"""
        def as_float(var: StringVar, fallback: float) -> float:
            try:
                return float(var.get())
            except Exception:
                return fallback

        s = self.cfg.signal
        s.rms_window_sec = max(1e-3, as_float(self.param_vars["rms_window"], s.rms_window_sec))
        s.rms_hop_sec = max(1e-3, as_float(self.param_vars["rms_hop"], s.rms_hop_sec))
        if s.rms_hop_sec > s.rms_window_sec:
            s.rms_hop_sec = s.rms_window_sec
        s.startup_skip_sec = max(0.0, as_float(self.param_vars["skip"], s.startup_skip_sec))
        s.highpass_hz = max(0.0, as_float(self.param_vars["hp"], s.highpass_hz))
        s.lowpass_hz = max(0.0, as_float(self.param_vars["lp"], s.lowpass_hz))
        notch = as_float(self.param_vars["notch"], s.notch_hz or 0.0)
        s.notch_hz = notch if notch > 0 else None
        self.cfg.display_window_sec = max(
            1.0, as_float(self.param_vars["display"], self.cfg.display_window_sec)
        )
        self.cfg.include_calibration_in_apdf = bool(self.include_cal_var.get())
        try:
            self.cfg.signal.channel = max(1, int(self.channel_var.get()))
        except Exception:
            self.cfg.signal.channel = 1

    def _connect(self) -> None:
        self._apply_params()
        problems = self.cfg.validate()
        if problems:
            messagebox.showerror("Invalid config", "\n".join(problems))
            return

        meta = self._selected_meta()
        try:
            if meta is None:
                profile = SyntheticProfile(
                    segments=[(20.0, 8.0), (15.0, 35.0), (10.0, 60.0), (20.0, 8.0)]
                )
                source: SampleSource = SyntheticSource(
                    profile=profile, srate=1000.0, realtime=True
                )
            else:
                source = LSLSource(self.cfg.stream, stream_info=meta)
            self.session = AcquisitionSession(self.cfg, source)
            self.session.open()
        except Exception as exc:
            self.session = None
            hint = ""
            if meta is not None:
                hint = (
                    "\n\nIf OpenSignals appears in the list but cannot be reached, make sure "
                    "the record button in OpenSignals is pressed (the stream is sent only while recording)."
                )
            messagebox.showerror("Connection failed", f"{exc}{hint}")
            self.status_var.set(f"Connection failed: {exc}")
            return

        if self.session.paths is not None:
            self.export_path_var.set(f"Output dir: {self.session.paths.root}")

        self.thread = _AcquisitionThread(self.session)
        self.thread.start()
        self.connect_btn.configure(text="Stop acquisition")
        self.status_var.set("Connected: " + self.session.meta.describe())
        self._last_error_shown = False
        # 若配置里已有 MVE 参考值，则不需要标定
        if self.cfg.mve_ref is None:
            self.cal_phase_var.set("Not calibrated yet: click \"Start calibration\" (APDF needs a %MVE reference)")
        else:
            self.manual_mve_var.set(f"{self.cfg.mve_ref:g}")

    def _disconnect(self) -> None:
        if self.thread is not None:
            self.thread.stop()
            self.thread.join(timeout=2.0)
            self.thread = None
        if self.session is not None:
            try:
                summary = self.session.close("GUI stop")
                apdf = summary.get("apdf") or {}
                if apdf:
                    self.status_var.set(
                        "Stopped | static={:.1f} median={:.1f} peak={:.1f} %MVE | "
                        "files: {}".format(
                            apdf.get("static_level_pct", float("nan")),
                            apdf.get("median_level_pct", float("nan")),
                            apdf.get("peak_level_pct", float("nan")),
                            (summary.get("files") or {}).get("raw_csv"),
                        )
                    )
            finally:
                self.session = None
        self.connect_btn.configure(text="Connect and start")
        self.cal_progress["value"] = 0

    # ================================================================== #
    # 标定 / 导出动作
    # ================================================================== #
    def _start_calibration(self) -> None:
        if self.session is None:
            messagebox.showinfo("Notice", "Connect to a data stream first.")
            return
        self.cfg.include_calibration_in_apdf = bool(self.include_cal_var.get())
        self.session.start_calibration()

    def _skip_phase(self) -> None:
        if self.session is not None:
            self.session.skip_calibration_phase()

    def _apply_manual_mve(self) -> None:
        if self.session is None:
            messagebox.showinfo("Notice", "Connect to a data stream first.")
            return
        try:
            value = float(self.manual_mve_var.get())
            self.session.set_mve_ref(value)
        except Exception as exc:
            messagebox.showerror("Invalid input", f"MVE reference must be a positive number: {exc}")

    def _apply_include_cal(self) -> None:
        self.cfg.include_calibration_in_apdf = bool(self.include_cal_var.get())

    def _open_export_dir(self) -> None:
        if self.session is None or self.session.paths is None:
            path = Path(self.cfg.export.root_dir)
        else:
            path = self.session.paths.root
        path.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showinfo("Output dir", f"{path}\n(could not open automatically: {exc})")

    def _export_snapshot(self) -> None:
        if self.session is None:
            messagebox.showinfo("Notice", "Connect to a data stream first.")
            return
        result = self.session.current_apdf()
        if result is None or result.n_windows == 0:
            messagebox.showinfo("Notice", "No APDF to export yet (finish %MVE calibration and acquire data first).")
            return
        out_dir = (
            self.session.paths.root
            if self.session.paths is not None
            else Path(self.cfg.export.root_dir)
        )
        stem = f"apdf_snapshot_{time.strftime('%H%M%S')}"
        files = export_apdf_result(result, out_dir, stem=stem)
        messagebox.showinfo("Exported", "\n".join(str(f) for f in files))

    def _reset_accumulator(self) -> None:
        if self.session is not None:
            self.session.reset_accumulator()

    # ================================================================== #
    # 定时刷新
    # ================================================================== #
    def _tick(self) -> None:
        self._tick_count += 1
        try:
            if self.thread is not None and self.thread.error and not self._last_error_shown:
                self._last_error_shown = True
                self.status_var.set("Acquisition thread failed; stopped")
                messagebox.showerror("Acquisition thread error", self.thread.error)
                self._disconnect()
            if self.session is not None:
                snap = self.session.snapshot()
                self._update_plots(snap)
                self._update_panels(snap)
        except Exception:  # pragma: no cover - 界面异常不应终止采集
            self.status_var.set("GUI refresh error: " + traceback.format_exc().splitlines()[-1])
        finally:
            self.root.after(100, self._tick)

    def _update_plots(self, snap: SessionSnapshot) -> None:
        window = float(self.cfg.display_window_sec)
        # 原始信号
        if snap.raw_t.size:
            t = snap.raw_t - snap.raw_t[-1]
            self.line_raw.set_data(t, snap.raw_y)
            self.ax_raw.set_xlim(-window, 0)
            span = float(np.max(np.abs(snap.raw_y))) or 1.0
            self.ax_raw.set_ylim(-span * 1.15, span * 1.15)
        # RMS / %MVE
        if snap.rms_t.size:
            t = snap.rms_t - snap.rms_t[-1]
            self.line_rms.set_data(t, snap.rms_value)
            self.ax_rms.set_xlim(-window, 0)
            peak = float(np.max(snap.rms_value))
            self.ax_rms.set_ylim(0, peak * 1.2 if peak > 0 else 1.0)
            self.ax_rms.set_ylabel(f"RMS [{snap.unit_label}]")
            self.ax_raw.set_ylabel(f"Amplitude [{snap.unit_label}]")
        # APDF（重算不频繁，隔拍刷新以省 CPU）
        result = snap.apdf
        if result is not None and result.n_windows > 0 and self._tick_count % 3 == 0:
            self.line_apdf.set_data(result.level_at_probability_pct, result.probability_pct)
            top = max(result.max_level_pct * 1.1, 10.0)
            self.ax_apdf.set_xlim(0, top)
            self.ax_apdf.set_ylim(0, 100)
            for line, text, (px, py, name) in zip(
                self.markers, self.marker_labels, result.marker_points()
            ):
                if not np.isfinite(py):
                    continue
                line.set_data([py], [px])
                text.set_position((py, px))
                text.set_text(f"{name} {py:.1f}%")
            self.canvas.draw_idle()

    def _update_panels(self, snap: SessionSnapshot) -> None:
        st = snap.stats
        self.status_var.set(
            f"Status: {'acquiring' if snap.connected else 'disconnected'} | "
            f"srate (measured): {st.srate_measured:.1f} Hz | samples: {st.n_samples} | "
            f"RMS windows: {st.n_rms_windows} | gaps: {st.gap_events}/{st.gap_sec:.2f}s | "
            f"writer: {st.writer.get('written', 0)} written / {st.writer.get('dropped', 0)} dropped | "
            f"{snap.messages[-1] if snap.messages else ''}"
        )

        if snap.calibration is not None:
            cal = snap.calibration
            self.cal_phase_var.set(f"{cal.phase_label} — {cal.message}")
            self.cal_progress["value"] = cal.overall_progress * 100.0
            if cal.phase.value in ("mvc", "between"):
                self.cal_detail_var.set(
                    f"Progress {cal.elapsed_in_phase:.1f}/{cal.duration_in_phase:.1f} s  "
                    f"({cal.remaining_in_phase:.1f} s left)"
                )
            elif cal.mve_ref:
                self.cal_detail_var.set(f"MVE reference = {cal.mve_ref:.4g} {snap.unit_label}")
            if len(self.trials_text.get_children()) != len(cal.trials):
                for item in self.trials_text.get_children():
                    self.trials_text.delete(item)
                for trial in cal.trials:
                    self.trials_text.insert(
                        "", END, values=(trial.index, f"{trial.plateau_rms:.4g}")
                    )

        result = snap.apdf
        if result is not None and result.n_windows:
            self.metric_vars["static"].set(
                f"static (low, P10): {result.static_level:.1f} %MVE"
            )
            self.metric_vars["median"].set(
                f"median (P50): {result.median_level:.1f} %MVE"
            )
            self.metric_vars["peak"].set(
                f"peak (high, P90): {result.peak_level:.1f} %MVE"
            )
            self.metric_vars["mean"].set(
                f"mean {result.mean_level_pct:.1f} / max {result.max_level_pct:.1f} %MVE"
            )
            self.metric_vars["cover"].set(
                f"APDF span {result.total_sec:.1f} s / {result.n_windows} windows"
            )
        self.metric_vars["mve"].set(
            "MVE reference: "
            + (f"{snap.mve_ref:.4g} {snap.unit_label}" if snap.mve_ref else "not set (calibrate first)")
        )
        self.rms_var.set(
            "Current RMS: "
            + (
                f"{snap.latest_rms:.4g} {snap.unit_label}"
                f"   |   {snap.latest_level_pct:.1f} %MVE"
                if np.isfinite(snap.latest_rms)
                else "—"
            )
        )
        if snap.stats.writer:
            w = snap.stats.writer
            self.writer_var.set(
                f"writer: {w.get('written', 0)} written, queue {w.get('queued', 0)}, "
                f"dropped {w.get('dropped', 0)}"
            )

    # ================================================================== #
    def _on_close(self) -> None:
        try:
            self._disconnect()
        finally:
            self.root.destroy()


def launch(cfg: AppConfig | None = None, autoconnect: bool = False) -> None:
    """启动实时界面。"""
    root = Tk()
    app = ApdfPluginApp(root, cfg)
    if autoconnect:
        root.after(800, app._connect)
    root.mainloop()
