"""统一界面（精简版）：**一个窗口，四件事**。

设计原则：**默认可读，能力不删**。

主界面只有：
    1. 选数据源（真实 OpenSignals LSL 流 / 导入 OpenSignals 录制 .txt / 合成自检）
    2. 填每块肌肉的 MVC
    3. 一个「开始 / 停止」按钮
    4. 看结果：APDF 曲线 + APDF 三个水平 + 有效 DPI

其余参数（通道映射、阈值、决策窗、基线段、死区、渐变、肌肉对、真实写驱动）
全部收进默认折叠的「高级设置」，需要时才展开 —— 不占视野，也不削减能力。

线程模型不变：采集/判定/写驱动都在一个工作线程里（复用 :mod:`lsl_apdf.unified`
的 ``DpiActuator`` 与插件的 ``BalancePolicy``），界面线程只取快照。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from tkinter import (BOTH, END, LEFT, RIGHT, X, Y, BooleanVar, StringVar, Tk,
                     filedialog, messagebox)
from tkinter import ttk

import numpy as np

import matplotlib

matplotlib.use("TkAgg")
from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .channels import MultiChannelMvc, parse_channel_spec, parse_mvc_spec
from .config import StreamConfig
from .dsp import GrowableF64
from .session_runner import IntegratedConfig, check_contract, session_dir_for
from .sources import (PYLSL_AVAILABLE, LSLSource, OpenSignalsSource,
                      StreamMeta, discover_streams, is_opensignals_txt)
from .unified import (DpiActuator, apdf_rows_and_curves, make_demo_source,
                      write_apdf_curve, write_load_disparity)

rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

__all__ = ["UnifiedGuiApp", "launch_unified"]

SYNTHETIC_ENTRY = "★ Synthetic signal (no hardware)"
IMPORT_ENTRY = "★ Import recording (OpenSignals .txt)..."
MUSCLE_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e",
                 "#9467bd", "#8c564b"]


def stream_config_from(cfg: IntegratedConfig) -> StreamConfig:
    """把会话配置里的 ``stream`` 段转成 StreamConfig（忽略未知键）。"""
    allowed = {f.name for f in __import__("dataclasses").fields(StreamConfig)}
    return StreamConfig(**{k: v for k, v in (cfg.stream or {}).items()
                           if k in allowed})


# --------------------------------------------------------------------------- #
@dataclass
class UiSnapshot:
    elapsed: float = 0.0
    apdf: list = field(default_factory=list)
    curves: dict = field(default_factory=dict)
    latest: dict = field(default_factory=dict)
    effective_dpi: float = 800.0
    level: str = "low"
    ramping: bool = False
    switches: int = 0
    crossings: int = 0
    threshold: float = 20.0
    messages: list = field(default_factory=list)


class _Engine(threading.Thread):
    """采集 → %MVC → APDF → balance 判定 → 写 DPI，全在一个线程里。"""

    def __init__(self, cfg: IntegratedConfig, source):
        super().__init__(daemon=True, name="unified-engine")
        self.cfg = cfg
        self.source = source
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._snap = UiSnapshot(threshold=float(cfg.high_thr or 20.0))
        self._messages: list[str] = []
        self._apdf: list = []
        self._curves: dict = {}
        self._accum: dict[str, GrowableF64] = {}
        self._pct_buf: list[tuple[float, dict]] = []
        self.error: str | None = None
        self.session_dir: Path | None = None
        self.summary: dict = {}

    # ------------------------------------------------------------------ #
    def snapshot(self) -> UiSnapshot:
        with self._lock:
            self._snap.messages = list(self._messages[-200:])
            return self._snap

    def stop(self) -> None:
        self._stop.set()

    def log(self, text: str) -> None:
        with self._lock:
            self._messages.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def _trim(self) -> None:
        if len(self._pct_buf) > 4000:
            del self._pct_buf[: len(self._pct_buf) - 4000]

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # pragma: no cover - 需要真实运行环境
        try:
            self._run()
        except Exception:
            self.error = traceback.format_exc()
            self.log("Error: " + self.error.splitlines()[-1])

    def _run(self) -> None:
        cfg = self.cfg
        problems = check_contract(cfg)
        if problems:
            raise ValueError("; ".join(problems))

        self.session_dir = session_dir_for(cfg)
        self.log(f"Session dir {self.session_dir.name}")

        meta = self.source.connect()
        pipeline = MultiChannelMvc.from_spec(
            fs=meta.nominal_srate, channel_spec=cfg.channels, mvc_spec=cfg.mvc,
            meta=meta)
        self.log(f"Connected {meta.name} | {meta.nominal_srate:g}Hz | "
                 f"{meta.n_channels} channels")
        self.log("Channels " + pipeline.describe())
        offline = bool(getattr(self.source, "is_file", False))
        if offline:
            self.log(f"Importing recording: {getattr(self.source, 'path', '?')}"
                     " (ends automatically when replay finishes)")

        plugin_dir = str(Path(cfg.plugin_dir).resolve())
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        from emg_server import BalancePolicy  # type: ignore

        policy = BalancePolicy({
            "switch_ramp_sec": cfg.switch_ramp_sec,
            "balance": cfg.balance_block(
                event_log=str(self.session_dir / "balance_events.csv")),
        })
        monitor = type("M", (), {"last": {}})()
        actuator_cfg = cfg
        if offline and not cfg.dry_run:
            actuator_cfg = replace(cfg, dry_run=True)
            self.log("Import mode: forced DRY-RUN; mouse DPI is not written")
        actuator = DpiActuator(actuator_cfg, self.session_dir, pipeline.muscles)
        self.log(f"{'DRY-RUN (no mouse writes)' if actuator_cfg.dry_run else '★ writing Raw Accel'}"
                 f" | {actuator_cfg.levels['low']['dpi']:.0f} <-> "
                 f"{actuator_cfg.levels['high']['dpi']:.0f} DPI")

        fh = (self.session_dir / "pctmvc.csv").open("w", encoding="utf-8", newline="")
        writer = csv.writer(fh)
        writer.writerow(["t_lsl", "t_wall"] + pipeline.muscles)
        self._accum = {m: GrowableF64() for m in pipeline.muscles}

        t0 = time.time()
        next_send = next_apdf = next_flush = 0.0
        ev_offset, ev_header = [0], [None]
        try:
            while not self._stop.is_set():
                chunk = self.source.read_chunk(timeout=0.05)
                if chunk is None and getattr(self.source, "finished", False):
                    self.log("Replay finished; shutting down")
                    break
                now = time.time()
                elapsed = now - t0
                if chunk is not None and len(chunk):
                    times, values = pipeline.push(chunk.data, chunk.timestamps)
                    if times.size:
                        latest = pipeline.latest(values)
                        for m, series in values.items():
                            good = series[np.isfinite(series)]
                            if good.size:
                                self._accum[m].append(good)
                        if latest:
                            writer.writerow(
                                [f"{float(times[-1]):.6f}", f"{now:.6f}"]
                                + [f"{latest.get(m, float('nan')):.4f}"
                                   for m in pipeline.muscles])
                            with self._lock:
                                self._pct_buf.append((float(times[-1]), dict(latest)))
                                self._trim()
                if elapsed >= next_send:
                    if self._pct_buf:
                        latest = self._pct_buf[-1][1]
                        monitor.last = latest
                        actuator.tick(elapsed, latest)
                        action, reason = policy.evaluate(elapsed, actuator.level,
                                                         monitor)
                        if action:
                            target = "high" if action == "up" else "low"
                            if actuator.request(target, reason, elapsed):
                                self.log(f"Switch -> {actuator._dpi_of(target):.0f} DPI"
                                         f" ({policy.A if action == 'up' else policy.B}"
                                         f" relatively more loaded)")
                    next_send = elapsed + 0.5
                if elapsed >= next_apdf:
                    self._apdf, self._curves = apdf_rows_and_curves(
                        self._accum, pipeline.muscles, cfg.high_thr, cfg.signal)
                    next_apdf = elapsed + 1.0
                if elapsed >= next_flush:          # 周期落盘，中途崩溃也不丢
                    fh.flush()
                    next_flush = elapsed + 5.0
                for row in _read_new(policy, ev_offset, ev_header):
                    if row.get("event") == "CROSSING":
                        self.log(f"Crossover ({row['reason'][:40]})")
                with self._lock:
                    s = self._snap
                    window = self._pct_buf[-900:]
                    s.elapsed = elapsed
                    s.latest = dict(window[-1][1]) if window else {}
                    s.apdf = self._apdf
                    s.curves = dict(self._curves)
                    s.effective_dpi = actuator.effective_dpi
                    s.level = actuator.level
                    s.ramping = actuator.ramping
                    s.switches = policy.stats["switches"]
                    s.crossings = policy.stats["crossings"]
        finally:
            actuator.reset(now - t0)
            rows, curves = apdf_rows_and_curves(self._accum, pipeline.muscles,
                                                cfg.high_thr, cfg.signal)
            with (self.session_dir / "apdf_summary.csv").open(
                    "w", encoding="utf-8", newline="") as out:
                w = csv.writer(out)
                w.writerow(type(rows[0]).header() if rows else [])
                for r in rows:
                    w.writerow(r.as_row())
            # 与命令行会话**产物一致**：界面会话过去只写 apdf_summary，
            # 于是 compare-direction 拿到界面跑的目录会说"找不到
            # load_disparity.csv"，曲线也无从比较 —— 而界面才是日常用法。
            curve_path = write_apdf_curve(curves, pipeline.muscles,
                                          self.session_dir)
            disparity = write_load_disparity(rows, cfg.pair, self.session_dir)
            with self._lock:                       # 收尾后界面还能看到最终曲线
                self._apdf, self._curves = rows, curves
                self._snap.apdf = rows
                self._snap.curves = dict(curves)
            policy.close()
            actuator.close()
            fh.close()
            try:
                self.source.close()
            except Exception:
                pass
            self.summary = {
                "mode": "unified GUI (single process)",
                "session_dir": str(self.session_dir),
                "duration_sec": round(time.time() - t0, 2),
                "dry_run": cfg.dry_run,
                "fixed_level": getattr(cfg, "fixed_level", ""),
                "channels": cfg.channels,
                "mvc": dict(cfg.mvc),
                "pair": list(cfg.pair),
                "pctmvc_rows": sum(1 for _ in self._pct_buf) or None,
                "balance": dict(policy.stats),
                "final_level": actuator.level,
                "apdf": {r.muscle: r.as_row() for r in rows},
                "load_disparity": disparity,
                "files": {
                    "pctmvc_csv": str(self.session_dir / "pctmvc.csv"),
                    "apdf_summary": str(self.session_dir / "apdf_summary.csv"),
                    "apdf_curve": str(curve_path) if curve_path else None,
                    "load_disparity": str(self.session_dir / "load_disparity.csv"),
                    "balance_events": policy.log_path,
                },
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (self.session_dir / "session_summary.json").write_text(
                json.dumps(self.summary, indent=2, ensure_ascii=False,
                           default=str), encoding="utf-8")
            self.log("Session ended; base level restored")
            self.log("Artifacts: apdf_summary.csv / apdf_curve.csv / "
                     "load_disparity.csv / session_summary.json")


def _read_new(policy, offset: list, header: list) -> list[dict]:
    path = policy.log_path
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            if header[0] is None:
                first = fh.readline()
                if not first:
                    return []
                header[0] = [c.strip() for c in first.rstrip("\r\n").split(",")]
                offset[0] = fh.tell()
            fh.seek(offset[0])
            raw = fh.read()
            if not raw or not raw.endswith("\n"):
                return []
            offset[0] += len(raw.encode("utf-8"))
    except OSError:
        return []
    out = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) == len(header[0]):
            out.append(dict(zip(header[0], row)))
    return out


# --------------------------------------------------------------------------- #
class UnifiedGuiApp:
    """精简界面：选流 → 填 MVC → 开始 → 看结果。"""

    def __init__(self, root: Tk, cfg: IntegratedConfig | None = None):
        self.root = root
        self.cfg = cfg or IntegratedConfig()
        self.engine: _Engine | None = None
        self._streams: list[StreamMeta] = []
        self._import_path: Path | None = None
        self._mvc_vars: dict[str, StringVar] = {}
        self._apdf_lines: dict[str, object] = {}
        self._logged = 0
        self._tick = 0
        self._adv_open = False

        root.title("EMG -> APDF -> DPI dynamic adjustment")
        root.geometry("1180x760")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self.root.after(300, self._refresh_streams)
        self.root.after(150, self._poll)

    # ================================================================== #
    # 构建界面
    # ================================================================== #
    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=BOTH, expand=True)

        # ---------- 第 1 行：数据源 + 开始 ---------- #
        row1 = ttk.Frame(outer)
        row1.pack(fill=X)
        ttk.Label(row1, text="Data source", font=("", 10, "bold")).pack(side=LEFT)
        self.stream_var = StringVar(value=SYNTHETIC_ENTRY)
        self.stream_combo = ttk.Combobox(
            row1, textvariable=self.stream_var, width=46, state="readonly",
            values=[SYNTHETIC_ENTRY])
        self.stream_combo.pack(side=LEFT, padx=6)
        self.stream_combo.bind("<<ComboboxSelected>>", self._on_stream_selected)
        ttk.Button(row1, text="Refresh", width=8,
                   command=self._refresh_streams).pack(side=LEFT)
        self.start_btn = ttk.Button(row1, text="Start", width=12,
                                    command=self._start)
        self.start_btn.pack(side=LEFT, padx=16)
        self.stop_btn = ttk.Button(row1, text="Stop", width=10,
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side=LEFT)

        # ---------- 第 2 行：MVC 输入 ---------- #
        row2 = ttk.Frame(outer)
        row2.pack(fill=X, pady=(8, 2))
        ttk.Label(row2, text="MVC baseline", font=("", 10, "bold")).pack(side=LEFT)
        self.mvc_row = ttk.Frame(row2)
        self.mvc_row.pack(side=LEFT, padx=6)
        self._rebuild_mvc_boxes()
        # ---------- 第 3 行：选择要对比的两块肌肉 ---------- #
        row3 = ttk.LabelFrame(outer, text="Compare", padding=6)
        row3.pack(fill=X, pady=(8, 0))
        grid = ttk.Frame(row3)
        grid.pack(fill=X)
        self.pair_a_var = StringVar()
        self.pair_a_combo = ttk.Combobox(grid, textvariable=self.pair_a_var,
                                        width=9, state="readonly")
        self.pair_a_combo.grid(row=0, column=0)
        ttk.Label(grid, text="vs").grid(row=0, column=1, padx=10)
        self.pair_b_var = StringVar()
        self.pair_b_combo = ttk.Combobox(grid, textvariable=self.pair_b_var,
                                        width=9, state="readonly")
        self.pair_b_combo.grid(row=0, column=2)
        for combo in (self.pair_a_combo, self.pair_b_combo):
            combo.bind("<<ComboboxSelected>>",
                       lambda _e: self._sync_pair_colors())
        # 各自的实时值直接落在对应下拉框下方（不用读文字就知道谁高）
        self.val_a_var = StringVar(value="—")
        self.val_b_var = StringVar(value="—")
        self.val_a_label = ttk.Label(grid, textvariable=self.val_a_var,
                                     font=("Consolas", 13, "bold"),
                                     foreground=MUSCLE_COLORS[0])
        self.val_a_label.grid(row=1, column=0, pady=(2, 0))
        self.val_b_label = ttk.Label(grid, textvariable=self.val_b_var,
                                     font=("Consolas", 13, "bold"),
                                     foreground=MUSCLE_COLORS[1])
        self.val_b_label.grid(row=1, column=2, pady=(2, 0))
        # 只显示两块肌肉的实时值 —— 谁高谁低一眼就看得出，不再写结论文字
        self.adv_btn = ttk.Button(row2, text="Advanced ▸", width=12,
                                  command=self._toggle_advanced)
        self.adv_btn.pack(side=RIGHT)
        self._rebuild_pair_boxes()

        # ---------- 高级设置（默认折叠）---------- #
        self.adv = ttk.LabelFrame(outer, text="Advanced (usually no changes needed)", padding=6)
        a1 = ttk.Frame(self.adv)
        a1.pack(fill=X)
        ttk.Label(a1, text="Channel mapping").pack(side=LEFT)
        self.ch_var = StringVar(value=self.cfg.channels)
        ttk.Entry(a1, textvariable=self.ch_var, width=30).pack(side=LEFT, padx=3)
        ttk.Button(a1, text="Apply mapping", width=12,
                   command=self._apply_mapping).pack(side=LEFT, padx=3)
        a2 = ttk.Frame(self.adv)
        a2.pack(fill=X, pady=(4, 0))
        for label, key, width in (("Thr %MVC", "thr_var", 5),
                                  ("Window s", "win_var", 5),
                                  ("Baseline s", "base_var", 5),
                                  ("Deadband", "db_var", 5),
                                  ("Ramp s", "ramp_var", 5)):
            self.__dict__[key] = StringVar(value={
                "thr_var": f"{self.cfg.high_thr:g}",
                "win_var": f"{self.cfg.roll_window_sec:g}",
                "base_var": f"{self.cfg.baseline_sec:g}",
                "db_var": f"{self.cfg.deadband:g}",
                "ramp_var": f"{self.cfg.switch_ramp_sec:g}",
            }[key])
            ttk.Label(a2, text=label).pack(side=LEFT)
            ttk.Entry(a2, textvariable=self.__dict__[key],
                      width=width).pack(side=LEFT, padx=3)
        self.real_var = BooleanVar(value=False)
        ttk.Checkbutton(self.adv, text="★ Write Raw Accel for real (changes mouse sensitivity)",
                        variable=self.real_var).pack(anchor="w", pady=(4, 0))

        # ---------- 主体：左图 右读数 ---------- #
        mid = ttk.Frame(outer)
        mid.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.mid_anchor = mid      # 高级面板展开时插到它前面

        left = ttk.Frame(mid)
        left.pack(side=LEFT, fill=BOTH, expand=True)
        self.fig = Figure(figsize=(8.0, 6.2), dpi=100, constrained_layout=True)
        self.ax_apdf = self.fig.add_subplot(111)
        # 坐标轴按**原文的约定**（Jonsson 1982, Fig. 6/7）：
        #   X = 幅值（%MVC），Y = 概率 P（幅值不超过该水平的时间比例，0→1）
        # 曲线因此从**左下升到右上**。我先前画成"X=超概率、Y=幅值"——
        # 那是转置并取了补，方向和形状都与文献不一致。
        self.ax_apdf.set_title("APDF（X = 幅值, Y = P = 不超过该水平的时间比例）")
        self.ax_apdf.set_xlabel("幅值 (%MVC)")
        self.ax_apdf.set_ylabel("P  (proportion of time at or below the level)")
        self.ax_apdf.set_xlim(0, 80)
        self.ax_apdf.set_ylim(0.0, 1.0)
        self.ax_apdf.grid(alpha=0.25)
        # 参考元素只画一次（先前在 _render 里每帧 axvline，会在一次会话里
        # 累积成百上千条重复线）。
        # 只保留 static (P=0.1) 一条参考线（按需求：去掉 median/peak 线与限值点）
        self.ax_apdf.axhline(0.1, color="#aaaaaa", ls=":", lw=0.9, zorder=1)
        self.ax_apdf.text(0.6, 0.115, "static P10  (P=0.1)",
                          fontsize=7, color="#888888", va="bottom")
        self.ax_apdf.axvline(float(self.cfg.high_thr), color="#e0a800",
                             ls="--", lw=1.1, zorder=1)
        canvas = FigureCanvasTkAgg(self.fig, master=left)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        NavigationToolbar2Tk(canvas, left).update()
        self.canvas = canvas

        right = ttk.Frame(mid, width=350)
        right.pack(side=RIGHT, fill=Y, padx=(10, 0))

        summ = ttk.LabelFrame(right, text="APDF load levels (%MVC)", padding=6)
        summ.pack(fill=X, pady=6)
        # Jonsson(1982) 的三个负荷水平，一个都不能少：
        #   static = 幅值分布 P10（90% 时间超过的水平）
        #   median = P50
        #   peak   = P90（10% 时间超过的水平）
        # 之前只做了 median/peak 两列 —— 而 static 是"持续性负荷"的判据，
        # 恰恰是最需要看的一个。
        self.apdf_tree = ttk.Treeview(
            summ, columns=("m", "p10", "p50", "p90"),
            show="headings", height=5)
        for c, t, w in (("m", "Muscle", 66), ("p10", "Static P10", 82),
                        ("p50", "Median P50", 86), ("p90", "Peak P90", 78)):
            self.apdf_tree.heading(c, text=t)
            self.apdf_tree.column(c, width=w, anchor="e")
        self.apdf_tree.pack(fill=X)
        # 把 Jonsson(1982) 的定义直接写在表下面：APDF 用**超概率**描述三个水平，
        # 而"10% level"指的是 peak（被超过 10% 时间 → 高），与分位 P90 对应；
        # "90% level"是 static（被超过 90% 时间 → 低），对应 P10。数字是交叉的，
        # 不写清方向极易被读反。
        ttk.Label(
            summ,
            text="static = exceeded 90% of time (P10)\n"
                 "median = exceeded 50% of time (P50)\n"
                 "peak   = exceeded 10% of time (P90)\n"
                 "ref. limits (Jonsson 1982): 5 / 14 / 70 %MVC",
            foreground="#777777", font=("", 8), justify="left").pack(
                anchor="w", pady=(3, 0))

        dpi = ttk.LabelFrame(right, text="Mouse sensitivity", padding=6)
        dpi.pack(fill=X)
        self.dpi_label = ttk.Label(dpi, text="—", font=("", 20, "bold"))
        self.dpi_label.pack(anchor="w")
        self.level_var = StringVar(value="Level —")
        ttk.Label(dpi, textvariable=self.level_var).pack(anchor="w")
        mrow = ttk.Frame(dpi)
        mrow.pack(fill=X, pady=(4, 0))
        ttk.Button(mrow, text="Manual 800", command=lambda: self._manual("low")).pack(
            side=LEFT, expand=True, fill=X)
        ttk.Button(mrow, text="Manual 1200", command=lambda: self._manual("high")).pack(
            side=LEFT, expand=True, fill=X, padx=(4, 0))

        log = ttk.LabelFrame(right, text="Run log", padding=4)
        log.pack(fill=BOTH, expand=True, pady=(6, 0))
        self.log_text = __import__("tkinter").Text(
            log, height=8, wrap="word", bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4", font=("Consolas", 9))
        sb = ttk.Scrollbar(log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self.log_text.pack(fill=BOTH, expand=True)

        self.status_var = StringVar(value="Ready -- pick a data source, fill in MVC, then click Start")
        ttk.Label(outer, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=4).pack(fill=X, side="bottom", pady=(8, 0))

    # ------------------------------------------------------------------ #
    def _muscles(self) -> list[str]:
        return list(parse_channel_spec(self.ch_var.get() if hasattr(self, "ch_var")
                                       else self.cfg.channels).values())

    def _rebuild_mvc_boxes(self) -> None:
        """按肌肉名重建 MVC 输入框（肌肉来自通道映射）。"""
        for w in self.mvc_row.winfo_children():
            w.destroy()
        self._mvc_vars = {}
        for i, muscle in enumerate(self._muscles()):
            ttk.Label(self.mvc_row, text=muscle,
                      foreground=MUSCLE_COLORS[i % len(MUSCLE_COLORS)],
                      font=("", 10, "bold")).pack(side=LEFT, padx=(8, 1))
            var = StringVar(value=f"{self.cfg.mvc.get(muscle, 0):g}")
            ttk.Entry(self.mvc_row, textvariable=var, width=7).pack(side=LEFT)
            self._mvc_vars[muscle] = var

    def _rebuild_pair_boxes(self) -> None:
        """重建"对比"下拉框（选项 = 通道映射里的肌肉）。"""
        muscles = self._muscles()
        for combo in (self.pair_a_combo, self.pair_b_combo):
            combo["values"] = muscles
        a, b = (self.cfg.pair + ["", ""])[:2]
        if a not in muscles:
            a = muscles[0] if muscles else ""
        if b not in muscles or b == a:
            b = next((m for m in muscles if m != a), "")
        self.pair_a_var.set(a)
        self.pair_b_var.set(b)
        self._sync_pair_colors()

    def _sync_pair_colors(self) -> None:
        """让下方两个数值的颜色跟图上那两条曲线一致。"""
        muscles = self._muscles()
        for var, label in ((self.pair_a_var, self.val_a_label),
                           (self.pair_b_var, self.val_b_label)):
            name = var.get()
            idx = muscles.index(name) if name in muscles else 0
            label.configure(foreground=MUSCLE_COLORS[idx % len(MUSCLE_COLORS)])

    def _apply_mapping(self) -> None:
        """通道映射改了以后，重建 MVC 输入框与对比下拉框。"""
        self.cfg.channels = self.ch_var.get().strip()
        try:
            self._muscles()
        except Exception as exc:
            messagebox.showerror("Invalid channel mapping", str(exc))
            return
        self._rebuild_mvc_boxes()
        self._rebuild_pair_boxes()
        self.status_var.set("Input boxes rebuilt for the new channel mapping")

    def _current_pair(self) -> tuple[str, str]:
        return self.pair_a_var.get(), self.pair_b_var.get()

    def _toggle_advanced(self) -> None:
        self._adv_open = not self._adv_open
        if self._adv_open:
            self.adv.pack(fill=X, pady=(6, 0), before=self.mid_anchor)
            self.adv_btn.configure(text="Advanced ▾")
        else:
            self.adv.pack_forget()
            self.adv_btn.configure(text="Advanced ▸")

    # ================================================================== #
    # 数据源
    # ================================================================== #
    def _refresh_streams(self) -> None:
        """刷新可见的 LSL 流。

        **真实流排在前面并自动选中** —— 正常用法就是"接上 OpenSignals → 刷新 →
        开始"，不该再多一步点下拉框。合成信号只在没发现任何流时兜底（列在最后）。
        """
        entries: list[str] = []
        self._streams = []
        n_foreign = 0
        if PYLSL_AVAILABLE:
            try:
                found = discover_streams(wait_time=0.6)
            except Exception as exc:
                found = []
                self.status_var.set(f"Failed to refresh stream list: {exc}")
            # 排序：OpenSignals 排最前并优先自动选中；其它流（同网段上别的
            # 机器发的示例流，如 LSLOutletStreamName-EEG / Accelerometer）
            # 排后面并加警告前缀 —— 它们能被 LSL 发现但**不是 EMG**，
            # 误选会把 EEG/加速度计数据当成肌电录下来。
            found = sorted(found, key=lambda m: (m.name != "OpenSignals", m.name))
            for meta in found:
                self._streams.append(meta)
                is_os = meta.name == "OpenSignals"
                if not is_os:
                    n_foreign += 1
                prefix = "" if is_os else "⚠ 非 OpenSignals："
                entries.append(f"{prefix}{meta.name} | {meta.type} | "
                               f"{meta.hostname} | {meta.n_channels}ch | "
                               f"{meta.nominal_srate:g}Hz")
            if found:
                if found[0].name == "OpenSignals":
                    self.status_var.set(
                        f"Found {len(found)} LSL stream(s); OpenSignals auto-selected"
                        + (f" · {n_foreign} non-OpenSignals stream(s) listed below"
                           if n_foreign else "")
                        + " -> click Start")
                else:
                    self.status_var.set(
                        f"Found {len(found)} LSL stream(s) but none is named "
                        f"OpenSignals — check that OpenSignals is recording before "
                        f"selecting one")
            else:
                self.status_var.set(
                    "No LSL streams found -> enable Settings->INTEGRATIONS->"
                    "Lab Streaming Layer in OpenSignals and press record; or pick "
                    "\"Import recording\" to analyze a recorded .txt")
        else:
            self.status_var.set("pylsl not installed (pip install pylsl)")

        entries.append(IMPORT_ENTRY)
        entries.append(SYNTHETIC_ENTRY)
        self.stream_combo["values"] = entries
        # 索引布局：真实流 0..n-1，导入文件 n，合成信号最后一位
        if self._import_path is not None and self.stream_var.get() == IMPORT_ENTRY:
            self.stream_combo.current(len(self._streams))
        elif self._streams:
            if self.stream_combo.current() < 0 or self.stream_var.get() == SYNTHETIC_ENTRY:
                self.stream_combo.current(0)
                self.stream_var.set(entries[0])
        elif self.stream_combo.current() < 0:
            self.stream_combo.current(len(entries) - 1)
            self.stream_var.set(SYNTHETIC_ENTRY)

    def _selected_stream(self) -> StreamMeta | None:
        """选中的真实流；选到导入文件或合成信号时返回 None。"""
        idx = self.stream_combo.current()
        if 0 <= idx < len(self._streams):
            return self._streams[idx]
        return None

    def _is_import_selected(self) -> bool:
        """下拉框当前是不是「导入录制文件」那一项。"""
        return self.stream_var.get() == IMPORT_ENTRY

    def _on_stream_selected(self, _event=None) -> None:
        """选到导入项就立刻弹出文件选择；选别的清掉上一次的导入路径。"""
        if self._is_import_selected():
            self._choose_import_file()
        else:
            self._import_path = None

    def _choose_import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select an OpenSignals .txt export",
            filetypes=[("OpenSignals text", "*.txt"), ("All files", "*.*")])
        if not path:
            self._revert_to_synthetic("Import cancelled; back to synthetic signal")
            return
        if not is_opensignals_txt(path):
            messagebox.showerror(
                "Not an OpenSignals export file",
                "The file header does not contain \"OpenSignals Text File Format\".\n"
                "Export it as .txt from OpenSignals first.")
            self._revert_to_synthetic("This file is not in OpenSignals export format")
            return
        self._import_path = Path(path)
        self.status_var.set(
            f"Recording selected: {self._import_path.name} -> fill in MVC and click Start")

    def _revert_to_synthetic(self, status: str) -> None:
        """取消/选错文件时回到兜底的合成信号，避免停在空导入状态。"""
        self._import_path = None
        values = list(self.stream_combo["values"])
        self.stream_combo.current(len(values) - 1)
        self.stream_var.set(SYNTHETIC_ENTRY)
        self.status_var.set(status)

    def _build_source(self):
        """按当前选择建立数据源（真实 LSL / 导入录制 / 合成）。

        ``discover_streams()`` 返回的是我们自己的 :class:`StreamMeta`（描述用），
        不是 ``pylsl.StreamInfo``，所以要按 MAC(type) > hostname > name 重新解析。
        """
        if self._is_import_selected():
            if self._import_path is None:
                raise ValueError("Select an OpenSignals .txt file to analyze first")
            return OpenSignalsSource(self._import_path, realtime=False)
        meta = self._selected_stream()
        if meta is None:
            return make_demo_source(self.cfg.channels, self.cfg.mvc)
        # 选到非 OpenSignals 的流时先确认：同网段上别的机器发的示例流
        # （EEG / Accelerometer / Quality / Markers）也长这样，误选会把
        # 完全不同的信号当成肌电录下来，而事后从数据上很难发现。
        if meta.name != "OpenSignals":
            ok = messagebox.askyesno(
                "Selected stream is not OpenSignals",
                f"You selected:\n    name = {meta.name}\n    type = {meta.type}\n"
                f"    host = {meta.hostname}\n    {meta.n_channels} ch @ "
                f"{meta.nominal_srate:g} Hz\n\n"
                "This does not look like an OpenSignals EMG stream. It is "
                "probably a demo/placeholder stream published by another "
                "computer on the network.\n\n"
                "Continue anyway?")
            if not ok:
                raise ValueError("Cancelled: the selected stream is not OpenSignals")
        sc = stream_config_from(self.cfg)
        if meta.type:
            sc.type, sc.hostname = meta.type, ""
        elif meta.hostname:
            sc.hostname, sc.type = meta.hostname, ""
        else:
            sc.name = meta.name
        return LSLSource(sc)

    # ================================================================== #
    # 读取表单
    # ================================================================== #
    def _read_form(self) -> bool:
        cfg = self.cfg
        cfg.channels = self.ch_var.get().strip()
        muscles = self._muscles()
        mvc: dict[str, float] = {}
        for m in muscles:
            var = self._mvc_vars.get(m)
            if var is None:
                continue
            try:
                mvc[m] = float(var.get())
            except ValueError:
                messagebox.showerror("Invalid MVC input", f"MVC for {m} is not a number: {var.get()!r}")
                return False
        cfg.mvc = mvc
        try:
            cfg.high_thr = float(self.thr_var.get())
            cfg.roll_window_sec = float(self.win_var.get())
            cfg.baseline_sec = float(self.base_var.get())
            cfg.deadband = float(self.db_var.get())
            cfg.switch_ramp_sec = float(self.ramp_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid advanced settings", f"Check the numeric fields: {exc}")
            return False
        pair = list(self._current_pair())
        if len(set(pair)) != 2 or not all(pair):
            messagebox.showerror("Incomplete comparison", "Pick two **different** muscles to compare.")
            return False
        if any(m not in muscles for m in pair):
            messagebox.showerror("Invalid comparison",
                                 f"Selected muscles are not in the channel mapping: {pair}\nCurrent mapping gives {muscles}")
            return False
        cfg.pair = pair
        cfg.dry_run = not self.real_var.get()

        problems = check_contract(cfg)
        if problems:
            messagebox.showerror("Cannot start yet", "\n".join(f"• {p}" for p in problems))
            return False
        if not cfg.dry_run:
            if not messagebox.askyesno(
                    "Confirm real driver",
                    "Raw Accel will really be written and the mouse sensitivity will change (800 <-> 1200 DPI).\n\n"
                    "Confirm the experiment environment is ready and the mouse is usable. Continue?"):
                self.real_var.set(False)
                cfg.dry_run = True
        return True

    # ================================================================== #
    # 交互
    # ================================================================== #
    def _start(self) -> None:
        if self.engine is not None and self.engine.is_alive():
            return
        if not self._read_form():
            return
        try:
            source = self._build_source()
        except Exception as exc:
            messagebox.showerror("Cannot create data source", str(exc))
            return
        self.engine = _Engine(self.cfg, source)
        self.engine.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Connecting...")

    def _stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
            self.status_var.set("Stopping and restoring...")

    def _manual(self, level: str) -> None:
        if self.engine is not None and self.engine.is_alive():
            self.status_var.set("Session running: manual switches will be overridden by the next automatic decision")
            return
        if not self.real_var.get():
            messagebox.showinfo("DRY-RUN", "Currently in DRY-RUN; the driver is not written.\n"
                                          "To really switch, enable real writes in \"Advanced\" first.")
            return
        try:
            act = DpiActuator(self.cfg, session_dir_for(self.cfg), ["ECU"])
            act._ramp = None
            act.sens = act._sens_of(level)
            act._write_driver(act.sens)
            act.level = level
            self.dpi_label.configure(text=f"{act.effective_dpi:.0f}")
            act.close()
            self.status_var.set(f"Manually switched to {level}")
        except Exception as exc:
            messagebox.showerror("Manual switch failed", str(exc))

    # ================================================================== #
    # 刷新
    # ================================================================== #
    def _poll(self) -> None:
        try:
            if self.engine is not None:
                if self.engine.error:
                    self.status_var.set("Error")
                    messagebox.showerror("Runtime error", self.engine.error)
                    self.engine = None
                    self._idle_buttons()
                else:
                    finished = (not self.engine.is_alive()
                                and bool(self.engine.summary))
                    self._render(self.engine.snapshot(), force=finished)
                    if finished:
                        self._idle_buttons()
                        self.status_var.set(
                            "Finished -> " + self.engine.summary["session_dir"])
                        self.engine = None
        except Exception:
            self.status_var.set("GUI refresh error: " + traceback.format_exc().splitlines()[-1])
        finally:
            self.root.after(150, self._poll)

    def _idle_buttons(self) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _render(self, s: UiSnapshot, force: bool = False) -> None:
        self._tick += 1
        pair = list(self._current_pair())
        self.dpi_label.configure(text=f"{s.effective_dpi:.0f}")
        self.level_var.set(f"Level {s.level}" + (" (ramping)" if s.ramping else ""))
        if s.elapsed:
            self.status_var.set(
                f"Running {s.elapsed:.0f}s | switches {s.switches} | crossovers {s.crossings}")

        # 只把两块肌肉的实时值显示到各自下拉框下方
        if len(pair) == 2 and all(pair):
            a, b = pair
            va, vb = s.latest.get(a), s.latest.get(b)
            self.val_a_var.set("—" if va is None or not np.isfinite(va) else f"{va:.1f}")
            self.val_b_var.set("—" if vb is None or not np.isfinite(vb) else f"{vb:.1f}")
        else:
            self.val_a_var.set("—")
            self.val_b_var.set("—")

        if s.apdf and (force or self._tick % 5 == 0):
            for item in self.apdf_tree.get_children():
                self.apdf_tree.delete(item)
            # 选中的两块排在最前并用 ● 标出
            rows = sorted(s.apdf, key=lambda r: (r.muscle not in pair, r.muscle))

            def fmt(v):
                return "—" if v is None or not np.isfinite(v) else f"{v:.1f}"

            for r in rows:
                mark = "● " if r.muscle in pair else "   "
                self.apdf_tree.insert("", END, values=(
                    mark + r.muscle,
                    fmt(r.static_pct), fmt(r.median_pct), fmt(r.peak_pct)))

        msgs = s.messages
        if msgs and self._logged != len(msgs):
            for line in msgs[self._logged:]:
                self.log_text.insert(END, line + "\n")
            self._logged = len(msgs)
            self.log_text.see(END)

        if not force and self._tick % 3:
            return
        if s.curves:
            for i, (m, xy) in enumerate(s.curves.items()):
                line = self._apdf_lines.get(m)
                if line is None:
                    line, = self.ax_apdf.plot([], [], lw=1.6,
                                              color=MUSCLE_COLORS[i % len(MUSCLE_COLORS)],
                                              label=m)
                    self._apdf_lines[m] = line
                    self.ax_apdf.legend(fontsize=9, loc="upper right", frameon=False)
                self._style_pair_line(line, m, pair, i, base_lw=1.6)
                # 原文 Fig.6/7 的约定：X = 幅值，Y = P（不超过该水平的时间比例）
                # curves 里存的是 (超概率 %, 幅值 %MVC)，所以
                #   x = 幅值（反转让它递增 → 曲线从左下升到右上）
                #   y = 1 − 超概率/100  = Jonsson 的 P
                line.set_data(xy[1][::-1], 1.0 - xy[0][::-1] / 100.0)
            tops = [r.peak_pct for r in s.apdf if np.isfinite(r.peak_pct)]
            # 右边界要容得下 peak 限值点(70)，否则看不到最上面那个判据
            self.ax_apdf.set_xlim(0, max(max(tops) if tops else 10.0, 74.0) * 1.05)
            self.ax_apdf.set_ylim(0.0, 1.0)
            self.canvas.draw_idle()

    @staticmethod
    def _style_pair_line(line, muscle: str, pair: list, index: int,
                         base_lw: float = 1.2) -> None:
        """选中的两块画粗+上色，其余压灰变细（只做背景参考）。"""
        if muscle in pair:
            line.set_color(MUSCLE_COLORS[index % len(MUSCLE_COLORS)])
            line.set_linewidth(base_lw + 0.7)
            line.set_alpha(1.0)
            line.set_zorder(3)
        else:
            line.set_color("#9aa0a6")
            line.set_linewidth(max(0.6, base_lw - 0.5))
            line.set_alpha(0.45)
            line.set_zorder(1)

    # ================================================================== #
    def _on_close(self) -> None:
        try:
            if self.engine is not None:
                self.engine.stop()
                self.engine.join(timeout=6.0)
        finally:
            self.root.destroy()


def launch_unified(cfg: IntegratedConfig | None = None,
                   autostart: bool = False) -> None:
    """打开精简界面。``autostart=True`` 时建完窗自动开始（演示用）。"""
    root = Tk()
    app = UnifiedGuiApp(root, cfg)
    if autostart:
        root.after(600, app._start)
    root.mainloop()
