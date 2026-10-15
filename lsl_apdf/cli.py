"""命令行入口：``python -m lsl_apdf <子命令>``。

======  ==============================================================
命令     作用
======  ==============================================================
``gui``      打开实时界面（默认）
``list``     列出当前可见的 LSL 流（排查"找不到流"第一步）
``mock``     发布一个模拟 OpenSignals 流（无硬件验证）
``run``      无界面采集：标定 + 采集 + 实时导出 + 打印 APDF 摘要
``calibrate``只做标定，把 MVE 参考值存成 JSON 供 ``run --mve-file`` 复用
``apdf``     对已录制的 raw.csv 离线重算 APDF（可出 PNG）
``selftest`` 用合成信号做端到端自检（含解析真值校验）
======  ==============================================================
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__
from .apdf import compute_apdf
from .calibration import CalibrationOutcome, CalibrationPhase
from .config import AppConfig
from .mock_stream import publish_mock_stream
from .offline import analyze_csv
from .recording import export_apdf_result
from .session import AcquisitionSession
from .sources import (
    PYLSL_AVAILABLE,
    LSLSource,
    SampleSource,
    SyntheticProfile,
    SyntheticSource,
    discover_streams,
)

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _setup_console() -> None:
    """Windows 控制台默认 GBK，中文/百分号会乱码 —— 强制 UTF-8 输出。

    ``pythonw.exe``（无控制台启动，用于隐藏命令框）下 ``sys.stdout`` 是 ``None``，
    这里必须整体跳过：``None.reconfigure`` 会抛异常。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:          # pythonw 无控制台
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _flush() -> None:
    """安全冲刷 stdout —— ``pythonw`` 下 ``sys.stdout`` 为 ``None``。

    ``print()`` 在 stdout 为 None 时是静默空操作（CPython 行为），但显式的
    ``sys.stdout.flush()`` 会 ``AttributeError: 'NoneType' object has no
    attribute 'flush'`` 直接崩掉整个启动流程。
    """
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.flush()
    except Exception:
        pass


def _load_config(args: argparse.Namespace) -> AppConfig:
    cfg = AppConfig.load(args.config) if getattr(args, "config", None) else AppConfig()
    overrides = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise SystemExit(f"--set expects k=v, got {item!r}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = _coerce(value.strip())
    if overrides:
        cfg.apply_overrides(overrides)

    if getattr(args, "channel", None):
        cfg.signal.channel = int(args.channel)
    if getattr(args, "stream_name", None):
        cfg.stream.name = args.stream_name
    if getattr(args, "mac", None):
        cfg.stream.type = args.mac
    if getattr(args, "host", None):
        cfg.stream.hostname = args.host
    if getattr(args, "resolve_timeout", None) is not None:
        cfg.stream.resolve_timeout = float(args.resolve_timeout)
    if getattr(args, "out", None):
        cfg.export.root_dir = args.out
    if getattr(args, "no_export", False):
        cfg.export.enabled = False
    if getattr(args, "subject", None):
        cfg.subject_id = args.subject
    if getattr(args, "rms_window", None) is not None:
        cfg.signal.rms_window_sec = float(args.rms_window)
    if getattr(args, "rms_hop", None) is not None:
        cfg.signal.rms_hop_sec = float(args.rms_hop)
    if getattr(args, "units", None):
        from .units import get_preset

        preset = get_preset(args.units)
        cfg.signal.adc_scale = preset.scale
        cfg.signal.adc_offset = preset.offset
        cfg.signal.unit_label = preset.unit
    if getattr(args, "mve", None) is not None:
        cfg.mve_ref = float(args.mve)
    if getattr(args, "mve_file", None):
        cfg.mve_ref = CalibrationOutcome.load(args.mve_file).mve_ref
        print(f"Loaded MVE reference = {cfg.mve_ref:.6g} from {args.mve_file}")
    return cfg


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _make_source(args: argparse.Namespace, cfg: AppConfig) -> SampleSource:
    """按命令行选择数据源：合成 / 回放 / 真实 LSL。"""
    if getattr(args, "synthetic", False) or getattr(args, "replay", None):
        if getattr(args, "replay", None):
            from .sources import ReplaySource

            return ReplaySource(
                args.replay,
                realtime=not getattr(args, "fast", False),
            )
        profile_kind = getattr(args, "profile", "work")
        if profile_kind == "balance":
            # 四通道交替负荷的"模拟被试"：ECU/UT 与 FDS/EDC 反相，
            # 专门用来端到端验证 DPI 插件的 balance 策略（每段 25 s 轮换）。
            from .sources import SyntheticProfile

            seg = float(getattr(args, "seg", 25.0) or 25.0)
            unit = float(getattr(args, "srate", 1000.0) or 1000.0)
            hi, lo = 30.0, 10.0
            a_phase = SyntheticProfile(
                segments=[(seg, hi), (seg, lo)], noise_floor_pct=0.0,
                mve_unit=unit, dc_offset=0.0)
            b_phase = SyntheticProfile(
                segments=[(seg, lo), (seg, hi)], noise_floor_pct=0.0,
                mve_unit=unit, dc_offset=0.0)
            return SyntheticSource(
                profiles=[a_phase, b_phase, a_phase, b_phase],
                channel_labels=["A1", "A2", "A3", "A4"],
                n_channels=4,
                srate=unit,
                realtime=not getattr(args, "fast", False),
            )
        if profile_kind == "calibration":
            from .mock_stream import CALIBRATION_PROFILE

            profile = CALIBRATION_PROFILE
        else:
            from .mock_stream import WORK_PROFILE

            profile = WORK_PROFILE
        return SyntheticSource(
            profile=profile,
            srate=float(getattr(args, "srate", 1000.0) or 1000.0),
            realtime=not getattr(args, "fast", False),
        )
    return LSLSource(cfg.stream)


def _print_apdf(result, unit: str = "%MVE") -> None:
    if result is None or result.n_windows == 0:
        print("  (No APDF yet: not calibrated or no data collected)")
        return
    print(f"  APDF span     : {result.total_sec:.1f} s ({result.n_windows} RMS windows, "
          f"window {result.window_sec:g}s / hop {result.hop_sec:g}s)")
    print(f"  MVE reference : {result.mve_ref:.6g}")
    print("  ------------------------------------------------------------")
    print(f"  static (low level, amplitude P10) : {result.static_level:8.2f} %MVE")
    print(f"  median (amplitude P50)            : {result.median_level:8.2f} %MVE")
    print(f"  peak   (high level, amplitude P90): {result.peak_level:8.2f} %MVE")
    print("  ------------------------------------------------------------")
    print(f"  mean {result.mean_level_pct:.2f} | std {result.std_level_pct:.2f} | "
          f"min {result.min_level_pct:.2f} | max {result.max_level_pct:.2f} %MVE")


def _run_calibration_interactive(session: AcquisitionSession) -> bool:
    """命令行标定：打印每个阶段的提示，等待状态机自动推进。"""
    cal = session.cfg.calibration
    print("\n===== %MVE calibration =====")
    print(f"{cal.mvc_trials} maximal voluntary contractions (MVC), {cal.mvc_sec:g} s each, "
          f"{cal.between_trials_sec:g} s rest between trials.")
    print("Requirements: good electrode contact, stable posture; stay fully relaxed during rest.\n")
    session.start_calibration()

    last_msg = ""
    warned = False
    while True:
        session.step(timeout=0.05)
        state = session.calibration_state
        if state.message != last_msg:
            last_msg = state.message
            print(f"[{state.phase_label}] {state.message}")
            if state.phase in (CalibrationPhase.REST, CalibrationPhase.MVC, CalibrationPhase.BETWEEN):
                print(f"    {state.remaining_in_phase:5.1f} s remaining")
        if not state.phase.active:
            break
    outcome = session.calibration_outcome
    if outcome is None or not np.isfinite(outcome.mve_ref):
        print("Calibration failed: no valid MVE reference.")
        return False
    print("\nCalibration result: " + outcome.summary_line())
    for trial in outcome.trials:
        print(f"  trial {trial.index}: plateau RMS={trial.plateau_rms:.6g} "
              f"peak={trial.peak_rms:.6g} mean={trial.mean_rms:.6g} "
              f"({trial.duration_sec:.2f}s)")
    if outcome.warnings and not warned:
        warned = True
        for warn in outcome.warnings:
            print("  (!) " + warn)
    return True


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def launch_unified_safe(icfg, autostart: bool = False) -> int:
    """启动统一界面，**未捕获异常也能被人看到**。

    用 ``pythonw`` 隐藏命令框后没有 stderr，异常会静默消失（双击后什么都不发生，
    完全无法排查）。所以这里兜住所有异常：写 ``last_crash.log`` + 弹一个 Tk 错误框。
    """
    import traceback

    try:
        from .gui_unified import launch_unified

        launch_unified(icfg, autostart=autostart)
        return 0
    except Exception:
        tb = traceback.format_exc()
        log_path = Path(__file__).resolve().parent / "last_crash.log"
        try:
            log_path.write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{tb}", encoding="utf-8")
        except Exception:
            log_path = None
        try:
            from tkinter import Tk, messagebox

            root = Tk()
            root.withdraw()
            messagebox.showerror(
                "统一界面启动失败",
                f"{tb.splitlines()[-1]}\n\n"
                + (f"完整信息已写入：\n{log_path}" if log_path else "")
                + "\n\n排查用：先双击「启动统一界面_带控制台.bat」看完整报错。")
            root.destroy()
        except Exception:
            pass
        return 2


def cmd_gui(args: argparse.Namespace) -> int:
    cfg = _load_config(args)

    # ---- 统一界面：一个窗口同时看信号与 DPI 控制（单进程，无 TCP）---- #
    if getattr(args, "unified", False):
        from .gui_unified import launch_unified
        from .session_runner import IntegratedConfig

        icfg = (IntegratedConfig.load(args.session_config)
                if getattr(args, "session_config", None) else IntegratedConfig())
        if not getattr(args, "session_config", None):
            # 界面默认值面向"打开就能看到东西"：正式实验的 300s 基线/窗口在界面上
            # 意味着点开始后 5 分钟毫无反应，会被误认为卡死。
            icfg.roll_window_sec = 30.0
            icfg.baseline_sec = 30.0
            icfg.confirm_sec = 5.0
            icfg.settle_sec = 30.0
            icfg.switch_ramp_sec = 20.0
            icfg.report_sec = 2.0
        if getattr(args, "channels", None):
            icfg.channels = args.channels
        if getattr(args, "mvc_spec", None):
            from .channels import parse_mvc_spec

            icfg.mvc = parse_mvc_spec(args.mvc_spec)
        if getattr(args, "pair", None):
            pair = [p.strip().upper()
                    for p in args.pair.replace(",", " ").split()]
            if len(pair) == 2:
                icfg.pair = pair
        icfg.dry_run = not bool(getattr(args, "real_driver", False))
        print("=" * 66)
        print("  Unified GUI (single window): left = signal & APDF, right = DPI control")
        print(f"  Default: {'DRY-RUN (no driver writes)' if icfg.dry_run else '* writing Raw Accel'}")
        print("  Click \"Start session\" to run the synthetic 4-muscle source (no hardware needed)")
        print("=" * 66)
        _flush()
        launch_unified_safe(icfg, autostart=bool(getattr(args, "autostart", False)))
        return 0

    from .gui import launch

    if not PYLSL_AVAILABLE:
        print("Warning: pylsl not installed; the GUI can only use the synthetic self-test (pip install pylsl)")
    print("=" * 62)
    print("  Realtime GUI started; three steps:")
    print("    1) Pick an LSL stream in the dropdown (click \"Refresh list\" first)")
    print("       -- with no hardware, pick \"* Synthetic signal (no hardware)\"")
    print("    2) Click \"Connect and start\"")
    print("    3) Click \"Start calibration\" and follow the rest/MVC prompts;")
    print("       after calibration the static/median/peak levels and APDF curve appear")
    print()
    print("  Export dir: %s\\session_<time>\\  (raw.csv / rms.csv / apdf_curve.csv ...)"
          % cfg.export.root_dir)
    print("=" * 62)
    _flush()  # 输出被重定向时是块缓冲，显式冲刷保证用户立刻看到
    launch(cfg, autoconnect=getattr(args, "autoconnect", False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if not PYLSL_AVAILABLE:
        print("pylsl is not installed; cannot list LSL streams. Run: pip install pylsl")
        return 1
    print(f"Searching for LSL streams (waiting {args.wait:g} s)...\n")
    found = discover_streams(wait_time=args.wait)
    if not found:
        print("No LSL streams found. Troubleshooting checklist (docs §6):")
        print("  1) OpenSignals -> Settings -> INTEGRATIONS: enable Lab Streaming Layer")
        print("  2) Press the red record button in the main window (the stream is sent only while recording)")
        print("  3) Firewall; usually not an issue when both run on the same PC")
        print("  4) To verify the pipeline first: python -m lsl_apdf mock (in another terminal)")
        return 1
    for i, meta in enumerate(found):
        print(f"[{i}] {meta.describe()}")
        print(f"     locate by MAC: --mac {meta.type!r}   by host: --host {meta.hostname!r}")
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    if not PYLSL_AVAILABLE:
        print("pylsl is not installed; cannot publish an LSL stream. Run: pip install pylsl")
        return 1
    try:
        pushed = publish_mock_stream(
            duration=args.duration,
            profile=args.profile,
            srate=args.srate,
            n_channels=args.channels,
            mac=args.mac,
        )
    except KeyboardInterrupt:
        print("\nPublishing stopped.")
        return 0
    print(f"\nPublishing finished; {pushed} samples.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    problems = cfg.validate()
    if problems:
        print("Invalid config:\n  " + "\n  ".join(problems))
        return 2

    source = _make_source(args, cfg)
    session = AcquisitionSession(cfg, source)
    try:
        meta = session.open()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1

    print("Connected: " + meta.describe())
    print(f"Filter chain: {session.filter_design.describe()}")
    if session.paths is not None:
        print(f"Output dir: {session.paths.root}")
    print(f"RMS window {cfg.signal.rms_window_sec:g}s / hop {cfg.signal.rms_hop_sec:g}s")

    if cfg.mve_ref is None:
        if args.no_calibration:
            print("\nNo MVE reference and --no-calibration was given: only signals will be exported, no APDF.")
        else:
            ok = _run_calibration_interactive(session)
            if not ok:
                session.close("calibration failed")
                return 1
            if args.save_mve:
                outcome = session.calibration_outcome
                if outcome is not None:
                    path = Path(args.save_mve)
                    outcome.save(path)
                    print(f"MVE reference saved: {path} (reuse with --mve-file next time)")
        if session.paths is not None:
            print(f"(calibration result also written to {session.paths.calibration_json})")

    print(f"\n===== Acquisition started ({args.duration:g} s) =====" if args.duration
          else "\n===== Acquisition started (Ctrl+C to stop) =====")

    start = time.time()
    next_report = 0.0
    try:
        while True:
            session.step(timeout=0.05)
            now = time.time()
            if now - start >= next_report:
                snap = session.snapshot()
                apdf = snap.apdf
                line = (
                    f"  t={now - start:6.1f}s  samples={snap.stats.n_samples:>8d}  "
                    f"srate={snap.stats.srate_measured:7.1f}Hz  "
                    f"RMS={snap.latest_rms:9.4g} {snap.unit_label}"
                )
                if np.isfinite(snap.latest_level_pct):
                    line += f"  ({snap.latest_level_pct:6.2f} %MVE)"
                if apdf and apdf.n_windows:
                    line += (
                        f"  | static {apdf.static_level:5.1f}"
                        f" median {apdf.median_level:5.1f}"
                        f" peak {apdf.peak_level:5.1f} %MVE"
                    )
                print(line, flush=True)
                next_report = now - start + args.report
            if args.duration is not None and (now - start) >= args.duration:
                break
    except KeyboardInterrupt:
        print("\nReceived Ctrl+C; stopping acquisition early.")

    result = session.current_apdf()
    summary = session.close(f"CLI run {args.duration or 'until-interrupt'}")
    print("\n===== Done: APDF result =====")
    _print_apdf(result)
    files = summary.get("files") or {}
    if files.get("raw_csv"):
        print("\nExported files:")
        for key, value in files.items():
            if value:
                print(f"  {key:18s} {value}")
    if args.plot and result is not None and result.n_windows:
        from .plotting import save_apdf_plot

        out = Path(cfg.export.root_dir) / f"apdf_{time.strftime('%Y%m%d_%H%M%S')}.png"
        save_apdf_plot(result, out)
        print(f"  APDF plot          {out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    source = _make_source(args, cfg)
    session = AcquisitionSession(cfg, source, create_writer=False)
    try:
        session.open()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1
    print("Connected: " + session.meta.describe())
    if not _run_calibration_interactive(session):
        session.close("calibration failed")
        return 1
    outcome = session.calibration_outcome
    session.close("calibration only")
    assert outcome is not None
    path = Path(args.save or "mve_calibration.json")
    outcome.save(path)
    print(f"\nCalibration result saved: {path}")
    print(f"Reuse it next run: python -m lsl_apdf run --mve-file {path}")
    return 0


def cmd_apdf(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    mve = cfg.mve_ref
    if mve is None:
        print("Offline APDF needs an MVE reference: use --mve 1.23 or --mve-file cal.json")
        return 2
    try:
        result, _, _, recording = analyze_csv(
            args.input,
            mve_ref=mve,
            cfg=cfg,
            channel_name=args.channel_name,
            channel=cfg.signal.channel,
            zero_phase=not args.causal,
        )
    except Exception as exc:
        print(f"Analysis failed: {exc}")
        return 1
    print(f"Input: {recording.path} ({recording.times.size} samples, "
          f"{recording.srate:.1f} Hz, channels {list(recording.channels)})")
    print(f"Filter: {'zero-phase filtfilt' if not args.causal else 'causal sosfilt (same as realtime)'}")
    print("\n===== APDF result =====")
    _print_apdf(result)

    out_dir = Path(args.out_dir or (Path(args.input).parent / "apdf_offline"))
    files = export_apdf_result(result, out_dir, stem=Path(args.input).stem + "_apdf")
    if args.plot:
        from .plotting import save_apdf_plot

        files.append(save_apdf_plot(result, out_dir / (Path(args.input).stem + "_apdf.png")))
    print("\nExported:")
    for f in files:
        print(f"  {f}")
    return 0


def cmd_stream_mvc(args: argparse.Namespace) -> int:
    """Part 2 → Part 3：把每肌肉 %MVC 实时推给 DPI 插件的 balance 策略。"""
    from .channels import MultiChannelMvc, parse_channel_spec, parse_mvc_spec
    from .emg_link import MvcCsvRecorder, MvcTcpSender

    cfg = _load_config(args)
    mapping = parse_channel_spec(args.channels)
    mvc = parse_mvc_spec(args.mvc_spec)
    if not mapping:
        print("Need --channels, e.g. --channels \"A1=ECU,A2=FDS,A3=UT,A4=EDC\"")
        return 2

    source = _make_source(args, cfg)
    try:
        meta = source.connect()
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1
    if not meta.nominal_srate:
        print("Stream declares no sampling rate (nominal_srate=0); cannot compute RMS window length")
        return 1

    try:
        pipeline = MultiChannelMvc.from_spec(
            fs=meta.nominal_srate,
            channel_spec=mapping,
            mvc_spec=mvc,
            meta=meta,
            signal_cfg=cfg.signal,
        )
    except Exception as exc:
        print(f"Invalid channel mapping: {exc}")
        return 1

    sender = MvcTcpSender(
        host=args.emg_host or cfg.stream.hostname or "127.0.0.1",
        port=int(args.port),
        protocol=args.protocol,
        retry_interval=args.retry,
    )
    sender.connect(force=True)

    recorder = None
    if args.pctmvc:
        recorder = MvcCsvRecorder(args.pctmvc, pipeline.muscles)
        recorder.open()

    print("=" * 66)
    print(f"Source   : {meta.describe()}")
    print(f"Filter   : {pipeline.design.describe()}")
    print(f"Channels : {pipeline.describe()}")
    print(f"Send to  : {sender.host}:{sender.port} (protocol {sender.protocol}, "
          f"{'connected' if sender.connected else 'disconnected, will keep retrying'})")
    print(f"Send rate: {args.send_hz:g} Hz   local log: {args.pctmvc or 'off'}")
    print("=" * 66)
    _flush()

    send_interval = 1.0 / max(args.send_hz, 0.01)
    next_send = 0.0
    start = time.time()
    next_report = 0.0
    latest: dict[str, float] = {}
    try:
        while True:
            chunk = source.read_chunk(timeout=0.05)
            if chunk is not None and len(chunk):
                times, values = pipeline.push(chunk.data, chunk.timestamps)
                if times.size:
                    latest = pipeline.latest(values)
                    if recorder is not None:
                        recorder.write(float(times[-1]), latest)
            now = time.time()
            if latest and now >= next_send:
                sender.send(latest)
                next_send = now + send_interval
                if args.print:
                    print("  -> " + ", ".join(f"{k}={v:.1f}" for k, v in latest.items()),
                          flush=True)
            if now - start >= next_report:
                print(f"  t={now - start:6.1f}s  %MVC=" +
                      (", ".join(f"{k}={v:.1f}" for k, v in latest.items()) or "-") +
                      f"  | {sender.describe()}", flush=True)
                next_report = now - start + args.report
            if args.duration is not None and (now - start) >= args.duration:
                break
    except KeyboardInterrupt:
        print("\nReceived Ctrl+C; stopping push.")
    finally:
        sender.close()
        if recorder is not None:
            recorder.close()
        try:
            source.close()
        except Exception:
            pass

    print("\n===== Push statistics =====")
    for key, value in sender.stats.as_dict().items():
        print(f"  {key:14s} {value}")
    if sender.last_error:
        print(f"  last error    {sender.last_error}")
    if recorder is not None:
        print(f"  local log     {recorder.path} ({recorder.rows} rows)")
    if sender.stats.sent == 0:
        print("\n(!) Nothing was sent: make sure the DPI plugin is running and listening, e.g.")
        print(f"   python main.py --mode emg --policy balance --port {sender.port}")
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """组合集成：一份配置同时驱动"信号侧 + 执行侧"，统一状态与安全收尾。"""
    from .session_runner import IntegratedConfig, check_contract, run_integrated

    if args.session_config:
        cfg = IntegratedConfig.load(args.session_config)
    else:
        cfg = IntegratedConfig()
    # 命令行覆盖（便于临时试跑，不改文件）
    if args.channels:
        cfg.channels = args.channels
    if args.mvc_spec:
        from .channels import parse_mvc_spec

        cfg.mvc = parse_mvc_spec(args.mvc_spec)
    if args.pair:
        pair = [p.strip().upper() for p in args.pair.replace(",", " ").split()]
        if len(pair) != 2:
            print("--pair needs exactly two muscles, e.g. --pair ECU,FDS")
            return 2
        cfg.pair = pair
    if args.plugin_dir:
        cfg.plugin_dir = args.plugin_dir
    if args.port is not None:
        cfg.port = int(args.port)
    if args.duration is not None:
        cfg.duration = float(args.duration)
    if args.out:
        cfg.out_dir = args.out
    if args.subject:
        cfg.subject_id = args.subject
    if args.real_driver:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True
    if not args.launch_plugin:
        cfg.launch_plugin = False
    if args.report is not None:
        cfg.report_sec = float(args.report)

    problems = check_contract(cfg)
    if problems:
        print("Integrated config failed check:")
        for p in problems:
            print(f"  x {p}")
        return 2

    def factory(c) -> "SampleSource":
        # 集成会话用合成源时，按**配置里的 MVC** 反推合成幅值（mve_unit=MVC），
        # 这样包络百分数直接等于 %MVC —— 否则演示会出现 ECU=389%MVC 这种荒谬值。
        if getattr(args, "synthetic", False) and not getattr(args, "replay", None):
            from .channels import parse_channel_spec
            from .sources import SyntheticProfile, SyntheticSource

            mapping = parse_channel_spec(c.channels)
            seg = 25.0
            profiles, labels = [], []
            for i, (token, muscle) in enumerate(mapping.items()):
                unit = float(c.mvc.get(muscle) or 1000.0)
                first, second = (30.0, 10.0) if i % 2 == 0 else (10.0, 30.0)
                profiles.append(SyntheticProfile(
                    segments=[(seg, first), (seg, second)],
                    noise_floor_pct=0.0, mve_unit=unit, dc_offset=0.0))
                labels.append(token)
            print(f"  Synthetic: normalized by configured MVC (mve_unit=MVC); load alternates "
                  f"between {' / '.join(mapping.values())}, {seg:g}s per segment")
            return SyntheticSource(
                profiles=profiles, channel_labels=labels,
                n_channels=len(profiles), srate=1000.0, realtime=True,
            )
        return _make_source(args, _load_config(args))

    try:
        summary = run_integrated(cfg, factory)
    except ValueError as exc:
        print(f"Start failed: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"Start failed: {exc}")
        return 1
    return 0 if not summary.get("interrupted") else 130


def cmd_unified(args: argparse.Namespace) -> int:
    """★ 统一插件：单进程内跑完采集→%MVC→APDF→balance→写 DPI（无 TCP）。"""
    from .session_runner import IntegratedConfig, check_contract
    from .unified import run_unified

    if args.session_config:
        cfg = IntegratedConfig.load(args.session_config)
    else:
        cfg = IntegratedConfig()
    if args.channels:
        cfg.channels = args.channels
    if args.mvc_spec:
        from .channels import parse_mvc_spec

        cfg.mvc = parse_mvc_spec(args.mvc_spec)
    if args.pair:
        pair = [p.strip().upper() for p in args.pair.replace(",", " ").split()]
        if len(pair) != 2:
            print("--pair needs exactly two muscles, e.g. --pair ECU,FDS")
            return 2
        cfg.pair = pair
    if args.plugin_dir:
        cfg.plugin_dir = args.plugin_dir
    if getattr(args, "fixed_level", None):
        cfg.fixed_level = args.fixed_level
    if args.duration is not None:
        cfg.duration = float(args.duration)
    if args.out:
        cfg.out_dir = args.out
    if args.subject:
        cfg.subject_id = args.subject
    if args.real_driver:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True
    if args.report is not None:
        cfg.report_sec = float(args.report)

    problems = check_contract(cfg)
    if problems:
        print("Config failed check:")
        for p in problems:
            print(f"  x {p}")
        return 2

    def factory(c) -> "SampleSource":
        if getattr(args, "synthetic", False) and not getattr(args, "replay", None):
            from .channels import parse_channel_spec
            from .sources import SyntheticProfile, SyntheticSource

            mapping = parse_channel_spec(c.channels)
            seg = 25.0
            profiles, labels = [], []
            for i, (token, muscle) in enumerate(mapping.items()):
                unit = float(c.mvc.get(muscle) or 1000.0)
                first, second = (30.0, 10.0) if i % 2 == 0 else (10.0, 30.0)
                profiles.append(SyntheticProfile(
                    segments=[(seg, first), (seg, second)],
                    noise_floor_pct=0.0, mve_unit=unit, dc_offset=0.0))
                labels.append(token)
            return SyntheticSource(profiles=profiles, channel_labels=labels,
                                   n_channels=len(profiles), srate=1000.0,
                                   realtime=True)
        return _make_source(args, _load_config(args))

    try:
        summary = run_unified(cfg, factory)
    except (ValueError, RuntimeError) as exc:
        print(f"Start failed: {exc}")
        return 1
    return 0 if not summary.get("interrupted") else 130


def cmd_selftest(args: argparse.Namespace) -> int:
    """端到端自检：合成信号有解析真值，因此可以严格判定对错。"""
    print("=" * 70)
    print("lsl_apdf self-test (synthetic signals + analytic ground truth)")
    print("=" * 70)
    checks: list[tuple[str, bool, str]] = []

    # --- 1. APDF 数学正确性：均匀分布下分位点有解析值 --- #
    uniform = np.linspace(0.0, 100.0, 10001)  # 0..100 的均匀幅值
    r = compute_apdf(uniform, mve_ref=100.0, window_sec=0.125, hop_sec=0.0625)
    ok = (
        abs(r.static_level - 10.0) < 0.05
        and abs(r.median_level - 50.0) < 0.05
        and abs(r.peak_level - 90.0) < 0.05
    )
    checks.append((
        "APDF quantiles (uniform 0-100 %MVE -> 10/50/90)",
        ok,
        f"static={r.static_level:.2f} median={r.median_level:.2f} peak={r.peak_level:.2f}",
    ))

    # 曲线读法与三个水平必须自洽：静态水平被超过的时间≈90%，峰值≈10%
    from .apdf import exceedance_probability

    levels_pct = uniform / 100.0 * 100.0
    ex_static = float(exceedance_probability(levels_pct, np.asarray([r.static_level]))[0])
    ex_peak = float(exceedance_probability(levels_pct, np.asarray([r.peak_level]))[0])
    ok = abs(ex_static - 0.90) < 0.02 and abs(ex_peak - 0.10) < 0.02
    checks.append((
        "Curve consistency: static exceeded ~90%, peak exceeded ~10%",
        ok,
        f"P(>static)={ex_static * 100:.1f}%  P(>peak)={ex_peak * 100:.1f}%",
    ))
    p10 = r.level_at_exceedance(0.10)
    checks.append((
        "Curve reading: at P=10% the level is the peak value 90",
        abs(p10 - 90.0) < 0.3,
        f"L(P=10%)={p10:.2f} %MVE",
    ))

    # 阶梯型分布：验证线性插值在跳变处的取值（不是 bug，是约定）
    stepped = np.concatenate([np.full(500, 5.0), np.full(300, 30.0), np.full(200, 80.0)])
    rs = compute_apdf(stepped, mve_ref=100.0, window_sec=0.125)
    checks.append((
        "Stepped distribution linear interpolation (5/30/80 at 50/30/20% -> 5 / 17.5 / 80)",
        abs(rs.static_level - 5.0) < 1e-9
        and abs(rs.median_level - 17.5) < 1e-9
        and abs(rs.peak_level - 80.0) < 1e-9,
        f"static={rs.static_level:.2f} median={rs.median_level:.2f} peak={rs.peak_level:.2f}",
    ))

    r2 = compute_apdf(uniform * 1000.0, mve_ref=100000.0, window_sec=0.125)
    checks.append((
        "Scale invariance (signal x1000 and MVE x1000; curve unchanged)",
        abs(r2.peak_level - r.peak_level) < 1e-9,
        f"peak={r2.peak_level:.2f} vs {r.peak_level:.2f}",
    ))

    # --- 2. 滤波器去 DC --- #
    from .dsp import design_filter_chain, filter_offline

    fs = 1000.0
    t = np.arange(int(fs * 5)) / fs
    sig = 1650.0 + 10.0 * np.sin(2 * np.pi * 100 * t)
    design = design_filter_chain(fs, 20.0, 450.0, 50.0, 30.0, 4)
    out = filter_offline(sig, fs, design)
    ok = abs(float(np.mean(out))) < 0.5 and abs(np.std(out) - 10 / np.sqrt(2)) < 0.5
    checks.append((
        "Band-pass removes DC (1650 offset removed; 100 Hz sine RMS ~7.07)",
        ok,
        f"mean={float(np.mean(out)):.3f} rms={float(np.std(out)):.3f}",
    ))

    # --- 3. 全链路：标定 → %MVE → APDF（对照解析真值） --- #
    prof = SyntheticProfile(
        segments=[(5.0, 0.0), (3.0, 100.0), (2.0, 0.0), (3.0, 100.0), (2.0, 0.0),
                  (3.0, 100.0), (5.0, 0.0), (20.0, 10.0), (20.0, 60.0), (20.0, 5.0)],
        noise_floor_pct=1.0,
        mve_unit=1000.0,
    )
    cfg = AppConfig()
    cfg.export.enabled = False
    cfg.signal.startup_skip_sec = 0.5
    cfg.calibration.between_trials_sec = 2.0
    cfg.calibration.plateau_sec = 1.5
    src = SyntheticSource(profile=prof, srate=1000.0, realtime=False)
    sess = AcquisitionSession(cfg, src, create_writer=False)
    sess.open()
    sess.start_calibration()
    while src.n_samples < 83 * 1000:
        sess.step(timeout=0.0)
    mve = sess.cfg.mve_ref
    checks.append((
        "MVC calibration (ground truth 1000)",
        mve is not None and abs(mve - 1000.0) / 1000.0 < 0.10,
        f"estimated {mve:.1f}, error {abs((mve or 0) - 1000) / 10:.2f}%" if mve else "not obtained",
    ))
    result = sess.current_apdf()
    sess.close("selftest")
    ok = (
        result is not None
        and abs(result.static_level - 6.0) < 4.0
        and abs(result.median_level - 11.0) < 6.0
        and abs(result.peak_level - 61.0) < 8.0
    )
    checks.append((
        "End-to-end APDF (task truth 10%/60%/5% + 1% noise floor)",
        ok,
        "static~6 median~11 peak~61 -> "
        + (
            f"static={result.static_level:.1f} median={result.median_level:.1f} "
            f"peak={result.peak_level:.1f}"
            if result is not None
            else "no result"
        ),
    ))

    # --- 4. 环境能力 --- #
    checks.append(("pylsl available (real LSL send/receive)", PYLSL_AVAILABLE,
                   "installed" if PYLSL_AVAILABLE else "not installed: pip install pylsl"))
    try:
        import tkinter  # noqa: F401

        gui_ok = True
    except Exception:
        gui_ok = False
    checks.append(("Tkinter available (realtime GUI)", gui_ok,
                   "available" if gui_ok else "tkinter missing; GUI unavailable"))

    print()
    failures = 0
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")
        failures += 0 if ok else 1
    print()
    if failures:
        print(f"Self-test failed: {failures} check(s) failed.")
        return 1
    print("Self-test passed. Next steps:")
    print("  Terminal A: python -m lsl_apdf mock")
    print("  Terminal B: python -m lsl_apdf gui")
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lsl_apdf",
        description="OpenSignals LSL realtime signal export + APDF curve plugin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m lsl_apdf selftest                 # verify environment and algorithms\n"
            "  python -m lsl_apdf mock                     # Terminal A: simulated OpenSignals stream\n"
            "  python -m lsl_apdf gui                      # Terminal B: realtime GUI\n"
            "  python -m lsl_apdf run --duration 120 --mac 00:07:80:AA:BB:CC\n"
            "  python -m lsl_apdf calibrate --save mve.json\n"
            "  python -m lsl_apdf apdf exports/session_x/raw.csv --mve-file mve.json --plot\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"lsl_apdf {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser, source: bool = True) -> None:
        p.add_argument("--config", help="Load config from JSON")
        p.add_argument("--set", action="append", metavar="k=v",
                       help="Override config via dotted path, e.g. --set signal.rms_window_sec=0.25")
        p.add_argument("--channel", type=int, help="Analysis channel (1-based)")
        p.add_argument("--out", help="Export root dir (default: exports)")
        p.add_argument("--no-export", action="store_true", help="Do not write any files")
        p.add_argument("--subject", help="Subject ID (written to the dir name and summary)")
        p.add_argument("--units", help="ADC conversion preset: raw / bitalino_emg / biosignalsplux_emg")
        p.add_argument("--mve", type=float, help="Set the MVE reference directly (skip calibration)")
        p.add_argument("--mve-file", help="Load the MVE reference from a calibration JSON")
        p.add_argument("--rms-window", type=float, help="RMS window length (s)")
        p.add_argument("--rms-hop", type=float, help="RMS hop (s)")
        if source:
            p.add_argument("--stream-name", default=None, help="Resolve by stream name (default: OpenSignals)")
            p.add_argument("--mac", default=None, help="Resolve by device MAC (stored in the type field)")
            p.add_argument("--host", default=None, help="Resolve by hostname")
            p.add_argument("--resolve-timeout", type=float, default=None)
            p.add_argument("--synthetic", action="store_true",
                           help="No hardware; use the built-in synthetic EMG")
            p.add_argument("--replay", help="Replay an exported CSV as the data source")
            p.add_argument("--fast", action="store_true",
                           help="With --synthetic/--replay: run as fast as possible instead of realtime")
            p.add_argument("--profile", default="work",
                           choices=["work", "calibration", "balance"],
                           help="Synthetic profile (balance = simulated 4-channel alternating load)")

    p_gui = sub.add_parser("gui", help="Open the realtime GUI")
    add_common(p_gui)
    p_gui.add_argument("--autoconnect", action="store_true", help="Auto-connect to the first source on start")
    p_gui.add_argument("--unified", action="store_true",
                       help="* Unified GUI: signal and DPI control in one window (single process, no TCP)")
    p_gui.add_argument("--session-config", help="For the unified GUI: session config JSON")
    p_gui.add_argument("--mvc", dest="mvc_spec", help="For the unified GUI: per-muscle MVC")
    p_gui.add_argument("--pair", help="For the unified GUI: balance muscle pair, e.g. ECU,FDS")
    p_gui.add_argument("--real-driver", action="store_true",
                       help="For the unified GUI: allow real Raw Accel writes (still toggled in the GUI)")
    p_gui.add_argument("--unified-autostart", dest="autostart", action="store_true",
                       help="For the unified GUI: auto-start the session on open (demo)")
    p_gui.set_defaults(func=cmd_gui)

    p_list = sub.add_parser("list", help="List visible LSL streams")
    p_list.add_argument("--wait", type=float, default=2.0, help="Search wait time (s)")
    p_list.set_defaults(func=cmd_list)

    p_mock = sub.add_parser("mock", help="Publish a simulated OpenSignals stream (hardware-free test)")
    p_mock.add_argument("--duration", type=float, default=None, help="Publish duration (s); unlimited by default")
    p_mock.add_argument("--profile", default="work", choices=["work", "calibration"])
    p_mock.add_argument("--srate", type=float, default=1000.0)
    p_mock.add_argument("--channels", type=int, default=1)
    p_mock.add_argument("--mac", default="00:07:80:AA:BB:CC")
    p_mock.set_defaults(func=cmd_mock)

    p_run = sub.add_parser("run", help="Headless acquisition: calibrate + acquire + export + print APDF")
    add_common(p_run)
    p_run.add_argument("--duration", type=float, default=None, help="Acquisition duration (s); until Ctrl+C by default")
    p_run.add_argument("--report", type=float, default=5.0, help="Progress print interval (s)")
    p_run.add_argument("--no-calibration", action="store_true",
                       help="Skip calibration (requires --mve/--mve-file, otherwise no APDF)")
    p_run.add_argument("--save-mve", help="Save the calibration result to JSON (for reuse with --mve-file)")
    p_run.add_argument("--plot", action="store_true", help="Save an APDF PNG when done")
    p_run.set_defaults(func=cmd_run)

    p_cal = sub.add_parser("calibrate", help="Run MVC calibration only and save the MVE reference")
    add_common(p_cal)
    p_cal.add_argument("--save", default="mve_calibration.json", help="Calibration output path")
    p_cal.set_defaults(func=cmd_calibrate)

    p_apdf = sub.add_parser("apdf", help="Recompute APDF offline from a recorded raw.csv")
    add_common(p_apdf, source=False)
    p_apdf.add_argument("input", help="Input CSV (raw.csv)")
    p_apdf.add_argument("--channel-name", help="Pick a channel by header name, e.g. ch1")
    p_apdf.add_argument("--out-dir", help="Output dir (default: <input dir>/apdf_offline)")
    p_apdf.add_argument("--causal", action="store_true",
                        help="Use causal filtering (exactly like realtime) instead of zero-phase")
    p_apdf.add_argument("--plot", action="store_true", help="Also write a PNG")
    p_apdf.set_defaults(func=cmd_apdf)

    p_stream = sub.add_parser(
        "stream-mvc",
        help="Push per-muscle %%MVC to the DPI plugin in realtime (Part 2 -> Part 3 link)",
    )
    add_common(p_stream)
    p_stream.add_argument("--channels", required=True,
                          help='channel->muscle mapping, e.g. "A1=ECU,A2=FDS,A3=UT,A4=EDC"')
    p_stream.add_argument("--mvc", dest="mvc_spec",
                          help='per-muscle MVC baseline, e.g. "ECU=80,FDS=57,UT=83,EDC=59"')
    p_stream.add_argument("--port", type=int, default=9000, help="DPI plugin listen port")
    p_stream.add_argument("--emg-host", default=None, help="DPI plugin host (default: 127.0.0.1)")
    p_stream.add_argument("--protocol", default="json", choices=["json", "pairs", "comma"],
                          help="Send protocol (the plugin accepts all three)")
    p_stream.add_argument("--send-hz", type=float, default=2.0, help="Push rate in Hz")
    p_stream.add_argument("--retry", type=float, default=2.0, help="Retry interval when unreachable (s)")
    p_stream.add_argument("--pctmvc", default=None,
                          help="Also record %%MVC to CSV (e.g. out/pctmvc.csv)")
    p_stream.add_argument("--duration", type=float, default=None, help="Push duration (s)")
    p_stream.add_argument("--report", type=float, default=10.0, help="Progress print interval (s)")
    p_stream.add_argument("--print", action="store_true", help="Also print every line sent")
    p_stream.set_defaults(func=cmd_stream_mvc)

    p_sess = sub.add_parser(
        "session",
        help="* Integrated: one config runs the signal side and the DPI plugin together (unified state + safe shutdown)",
    )
    add_common(p_sess)
    p_sess.add_argument("--session-config", help="Integrated session config JSON (one file for both sides)")
    p_sess.add_argument("--channels", help='channel->muscle mapping, e.g. "A1=ECU,A2=FDS,A3=UT,A4=EDC"')
    p_sess.add_argument("--mvc", dest="mvc_spec",
                        help='per-muscle MVC baseline, e.g. "ECU=80,FDS=57,UT=83,EDC=59"')
    p_sess.add_argument("--pair", help="balance muscle pair, e.g. ECU,FDS")
    p_sess.add_argument("--plugin-dir", help="DPI plugin directory")
    p_sess.add_argument("--port", type=int, default=None, help="EMG TCP port")
    p_sess.add_argument("--duration", type=float, default=None, help="Session duration (s)")
    p_sess.add_argument("--report", type=float, default=None, help="Unified status print interval (s)")
    p_sess.add_argument("--launch-plugin", action="store_true", default=True,
                        help="Launch the plugin from this command (default)")
    p_sess.add_argument("--no-launch-plugin", dest="launch_plugin", action="store_false",
                        help="Use the already running plugin")
    p_sess.add_argument("--real-driver", action="store_true",
                        help="* Really write Raw Accel (changes mouse DPI); dry-run by default")
    p_sess.add_argument("--dry-run", action="store_true", default=False,
                        help="Force dry-run (no driver writes)")
    p_sess.set_defaults(func=cmd_session)

    p_uni = sub.add_parser(
        "unified",
        help="** Unified plugin: single process for acquisition -> %%MVC -> APDF -> balance -> DPI write (no TCP, no port)",
    )
    add_common(p_uni)
    p_uni.add_argument("--session-config", help="Session config JSON (one file for both sides)")
    p_uni.add_argument("--channels", help='channel->muscle mapping, e.g. "A1=ECU,A2=FDS,A3=UT,A4=EDC"')
    p_uni.add_argument("--mvc", dest="mvc_spec", help='per-muscle MVC, e.g. "ECU=80,FDS=57"')
    p_uni.add_argument("--pair", help="balance muscle pair, e.g. ECU,FDS")
    p_uni.add_argument("--plugin-dir", help="DPI plugin dir (provides rawaccel_bridge and the balance policy)")
    p_uni.add_argument("--duration", type=float, default=None, help="Session duration (s)")
    p_uni.add_argument("--fixed-level", choices=["low", "high"], default=None,
                       help="lock the DPI level and disable automatic switching "
                            "(needed for the direction-verification runs)")
    p_uni.add_argument("--report", type=float, default=None, help="Status print interval (s)")
    p_uni.add_argument("--real-driver", action="store_true",
                       help="* Really write Raw Accel (changes mouse DPI); dry-run by default")
    p_uni.add_argument("--dry-run", action="store_true", default=False,
                       help="Force dry-run (no driver writes)")
    p_uni.set_defaults(func=cmd_unified)

    p_cmp = sub.add_parser(
        "compare-direction",
        help="Compare two locked-level sessions (direction verification)")
    p_cmp.add_argument("sessions", nargs="+",
                       help="two or more session directories produced by unified/session")
    p_cmp.add_argument("--labels",
                       help="comma-separated labels, e.g. \"800,1200\"")
    p_cmp.add_argument("--output", help="where to write apdf_compare.png (default: cwd)")
    p_cmp.set_defaults(func=cmd_compare_direction)

    p_self = sub.add_parser("selftest", help="End-to-end self-test (algorithms + environment)")
    p_self.set_defaults(func=cmd_selftest)

    return parser


def cmd_compare_direction(args: argparse.Namespace) -> int:
    """比较两次"锁档"会话，帮助判断改变 DPI 把负荷推向了哪块肌肉。

    **只报告数字，不下"方向正确"的结论。** 单次会话内 P50 本身就有波动，
    两次之间的差异是否可信，取决于它是否明显大于会话内波动 —— 那是统计判断，
    不该由脚本替你宣布"验证通过"（那会给出一个建立在噪声上的假绿灯）。
    """
    import csv as _csv

    dirs = [Path(p) for p in args.sessions]
    for d in dirs:
        if not (d / "load_disparity.csv").exists():
            print(f"找不到 {d / 'load_disparity.csv'}")
            print()
            print("  可能原因：")
            print("   · 这个会话是用**旧版本**跑的 —— load_disparity.csv / "
                  "apdf_curve.csv 是后来才加入的产物")
            print("   · 或者界面会话跑在旧版本上（界面此前不写这两个文件）")
            print("  处理：用当前版本**重新跑一次会话**，再对新目录做对比。")
            print()
            print("  （已有的旧会话无法补救 —— 曲线与负荷差异当时没有落盘，"
                  "数据不在磁盘上。）")
            return 2

    def summarise(d: Path) -> dict:
        levels: dict[str, dict] = {}
        with (d / "load_disparity.csv").open(encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                levels[r["level"]] = {
                    "A": float(r["A_value"]), "B": float(r["B_value"]),
                    "diff": float(r["diff_A_minus_B"]),
                    "absdiff": float(r["abs_diff"]),
                }
        pair = ["?", "?"]
        with (d / "load_disparity.csv").open(encoding="utf-8") as fh:
            first = next(_csv.DictReader(fh), None)
            if first:
                pair = first["muscle_pair"].split("-")
        # 会话内波动：pctmvc.csv 每 60 s 块的均值 → 标准差
        vol = {m: float("nan") for m in pair}
        p = d / "pctmvc.csv"
        if p.exists():
            with p.open(encoding="utf-8") as fh:
                rd = _csv.reader(fh)
                header = next(rd, [])
                idx = {m: header.index(m) for m in pair if m in header}
                ts, series = [], {m: [] for m in idx}
                for row in rd:
                    if len(row) != len(header):
                        continue
                    try:
                        ts.append(float(row[0]))
                        for m, i in idx.items():
                            series[m].append(float(row[i]))
                    except ValueError:
                        continue
            if ts:
                t = np.asarray(ts)
                for m, vals in series.items():
                    a = np.asarray(vals, dtype=float)
                    # 60 s 分块；会话太短时退回 20 s（至少要 2 块才算得出 SD）
                    for blk in (60.0, 20.0):
                        blocks = [float(np.nanmean(a[(t >= s) & (t < s + blk)]))
                                  for s in np.arange(t[0], t[0] + max(
                                      0.0, t[-1] - t[0] - blk) + 1e-9, blk)
                                  if ((t >= s) & (t < s + blk)).sum() > 10]
                        if len(blocks) > 1:
                            vol[m] = float(np.nanstd(blocks, ddof=1))
                            break
        return {"dir": d, "levels": levels, "pair": pair, "vol": vol}

    info = [summarise(d) for d in dirs]
    labels = (args.labels.split(",") if getattr(args, "labels", None)
              else [d.name for d in dirs])
    while len(labels) < len(info):
        labels.append(info[len(labels)]["dir"].name)

    pair = info[0]["pair"]
    a, b = pair[0], pair[1]
    print("=" * 104)
    print("锁档会话对比（方向验证）—— 三个负荷水平**并排全部呈现**")
    print("=" * 104)
    print("  按 Jonsson(1982) 的判据，三个水平各自与本水平的限值比较；"
          "这里不把三者合成任何单一指标。")
    print("  限值（不得超过档）：static 5 / median 14 / peak 70 %MVC —— "
          "只适用于群体分析。")
    print()
    print(f"  {'条件':<14}{'会话目录':<32}{'水平':<8}"
          f"{a:>9}{b:>9}{'|差|':>8}{a + '/限':>9}{b + '/限':>9}{'超标':>10}")
    limits = {"static": 5.0, "median": 14.0, "peak": 70.0}
    for lab, s in zip(labels, info):
        any_row = False
        for lv in ("static", "median", "peak"):
            d = s["levels"].get(lv)
            if not d:
                continue
            va, vb = d["A"], d["B"]
            ra, rb = va / limits[lv], vb / limits[lv]
            over = [n for n, r in ((a, ra), (b, rb)) if r > 1.0]
            over_txt = ("、".join(over) if over else "—")
            head = (f"  {lab:<14}{s['dir'].name:<32}" if not any_row
                    else " " * 46)
            print(f"{head}{lv:<8}{va:>9.2f}{vb:>9.2f}{d['absdiff']:>8.2f}"
                  f"{ra:>9.2f}{rb:>9.2f}{over_txt:>10}")
            any_row = True
        sd_a, sd_b = s["vol"][a], s["vol"][b]
        if np.isfinite(sd_a) and np.isfinite(sd_b):
            print(" " * 46 + f"会话内 SD(P50): {a} {sd_a:.2f} / {b} {sd_b:.2f}"
                  f"   （判读：变化需 >1×SD 才值得当证据）")
    print()
    print("  超标个数（计数，非加权合成）：")
    for lab, s in zip(labels, info):
        cnt_a = sum(1 for lv, d in s["levels"].items()
                    if d["A"] / limits.get(lv, 1e9) > 1.0)
        cnt_b = sum(1 for lv, d in s["levels"].items()
                    if d["B"] / limits.get(lv, 1e9) > 1.0)
        print(f"    {lab:<14}{a}: {cnt_a}/3 个水平超标    "
              f"{b}: {cnt_b}/3 个水平超标")

    print()
    print("  每块肌肉在两次条件之间的变化（中位负荷 P50，单位 %MVC）：")
    if len(info) >= 2:
        first, last = info[0], info[-1]
        m1, m2 = first["levels"].get("median"), last["levels"].get("median")
        if m1 and m2:
            for name, key in ((a, "A"), (b, "B")):
                v1, v2 = m1[key], m2[key]
                sd = last["vol"].get(name, float("nan"))
                ratio = abs(v2 - v1) / sd if sd and sd > 0 else float("nan")
                print(f"    {name:<6}{v1:>8.2f} -> {v2:>8.2f}   "
                      f"{v2 - v1:>+7.2f}   "
                      f"（会话内 SD {sd:.2f}，变化 = {ratio:.1f}×SD）"
                      if np.isfinite(ratio) else
                      f"    {name:<6}{v1:>8.2f} -> {v2:>8.2f}   {v2 - v1:>+7.2f}")

    print()
    print("  判读提示（**不下结论**）：")
    print(f"    · 若 {labels[0]}(低档) -> {labels[-1]}(高档) 时 "
          f"{a} 下降而 {b} 上升，则与「高档把负荷从 {a} 转向 {b}」的预期一致")
    print("    · 变化是否可信，取决于它相对「会话内 SD」的倍数：")
    print("      1x 以内基本是噪声；个位数倍以上才值得当方向证据。")
    print("      必要时重复几次，或延长单次时长。")

    _compare_apdf_curves(
        info, labels, pair,
        Path(args.output) if getattr(args, "output", None) else Path.cwd())
    return 0


def _load_curve(session_dir: Path) -> dict:
    """读 ``apdf_curve.csv``（长表）→ {muscle: (probability, level)}。"""
    import csv as _csv

    path = session_dir / "apdf_curve.csv"
    if not path.exists():
        return {}
    out: dict[str, list] = {}
    with path.open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            try:
                slot = out.setdefault(row["muscle"], [[], []])
                slot[0].append(float(row["probability_pct"]))
                slot[1].append(float(row["level_pct_mvc"]))
            except (KeyError, ValueError):
                continue
    return {m: (np.asarray(p), np.asarray(l)) for m, (p, l) in out.items()}


def _level_at(prob: np.ndarray, level: np.ndarray, p: float) -> float:
    """曲线在超概率 p 处的幅值（曲线按概率递增存储 → 直接插值）。"""
    if prob.size == 0:
        return float("nan")
    return float(np.interp(p, prob, level))


def _compare_apdf_curves(info, labels, pair, out_dir: Path) -> None:
    """比较两次条件的 **APDF 曲线**：标准概率处取值表 + 叠加图。

    汇总数字只给 3 个百分位点；曲线比较看的是**整条曲线的移动** —— 那才是
    "负荷是否在两块肌肉间重新分配"的直接证据。
    """
    curves = [(s["dir"], _load_curve(s["dir"])) for s in info]
    if not any(c for _, c in curves):
        print()
        print("  （两次会话都没有 apdf_curve.csv，无法比较曲线 ——")
        print("    该文件是本次改动才开始写出的，重新跑一次会话即可）")
        return

    probs = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
    print()
    print("=" * 92)
    print("APDF 各水平的幅值（按 Jonsson 的 P 读取：P = 不超过该水平的时间比例）")
    print("=" * 92)
    print("  水平定义（Jonsson 1982, J. Human Ergol. 11(1):73-88，原文以 P 表示"
          "「幅值不超过该水平的时间比例」= 分位）：")
    print("    static  P=0.1  （肌休息 ≤10% 时间，即 90% 时间在此水平之上）= P10")
    print("    median  P=0.5                                                = P50")
    print("    peak    P=0.9  （收缩高于此水平的时间 ≤10%）                 = P90")
    print("  图表坐标（同原文 Fig.6/7）：X = 幅值 %MVC，Y = P（0→1），"
          "曲线从左下升到右上；")
    print("  三条水平虚线即 P=0.1/0.5/0.9 的读取位置，向左对齐到 X 轴读出三个水平。")
    print("  参考限值（原文 p.80，引自 Jonsson 1978；适用：持续 ≥1 小时的受限作业）：")
    print("    static  不超过 2%， 不得超过 5%  MVC")
    print("    median  不超过 10%，不得超过 14% MVC")
    print("    peak    不超过 50%，不得超过 70% MVC")
    print("  ⚠ 原文强调：这些限值**只适用于群体分析**（groups of subjects），"
          "个体数据超出不必然代表作业不可接受。")
    for muscle in pair:
        print(f"\n  【{muscle}】")
        head = f"  {'P (Jonsson)':>12}"
        for lab in labels:
            head += f"{lab:>12}"
        if len(labels) >= 2:
            head += f"{'后-前':>10}"
        print(head)
        for p in probs:
            line = f"  {p:>11.2f}"
            vals = []
            for _, c in curves:
                xy = c.get(muscle)
                # 曲线按**超概率**存储 → Jonsson 的 P 对应超概率 (1−P)
                v = (_level_at(xy[0], xy[1], (1.0 - p) * 100.0)
                     if xy else float("nan"))
                vals.append(v)
                line += f"{v:>12.2f}" if np.isfinite(v) else f"{'—':>12}"
            if len(vals) >= 2 and all(np.isfinite(v) for v in vals):
                line += f"{vals[-1] - vals[0]:>+10.2f}"
            tag = ""
            if abs(p - 0.1) < 1e-9:
                tag = "   ← static"
            elif abs(p - 0.5) < 1e-9:
                tag = "   ← median"
            elif abs(p - 0.9) < 1e-9:
                tag = "   ← peak"
            print(line + tag)

    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import rcParams
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
        rcParams["axes.unicode_minus"] = False
        muscle_colors = ["#1f77b4", "#d62728"]
        fig = Figure(figsize=(9.2, 5.8), dpi=130, constrained_layout=True)
        ax = fig.add_subplot(1, 1, 1)
        for idx, (d, c) in enumerate(curves):
            if not c:
                continue
            for j, muscle in enumerate(pair):
                xy = c.get(muscle)
                if xy is None:
                    continue
                # 原文 Fig.6/7 的约定：X = 幅值，Y = P（不超过该水平的时间比例）。
                # 存储是 (超概率 %, 幅值 %MVC) → 反转 + 取补 ⇒ 曲线从左下升到右上。
                ax.plot(xy[1][::-1], 1.0 - xy[0][::-1] / 100.0,
                        ls="--" if idx == 0 else "-",
                        color=muscle_colors[j % len(muscle_colors)],
                        # 先跑的那条画**更粗**并放在上层：两条完全重合时
                        # （例如该条件没有真正改变负荷）虚线仍能露出来，
                        # 否则实线会把它整个盖住，看不出"没有变化"。
                        lw=2.6 if idx == 0 else 1.5,
                        zorder=3 if idx == 0 else 2,
                        label=f"{labels[idx]} · {muscle}")
        ax.set_xlabel("幅值 (%MVC)")
        ax.set_ylabel("P  (proportion of time at or below the level)")
        ax.set_title("APDF 曲线比较（坐标约定同 Jonsson 1982 Fig.6/7）")
        ax.set_xlim(0, 78)
        ax.set_ylim(0.0, 1.0)
        ax.grid(alpha=0.25)
        # 三个标准负荷水平的读取位置：Y = 0.1 / 0.5 / 0.9（水平线，向右读 X）
        for p, name in ((0.1, "static P10"), (0.5, "median P50"),
                        (0.9, "peak P90")):
            ax.axhline(p, color="#aaaaaa", ls=":", lw=0.9, zorder=1)
            ax.text(0.6, p + 0.012, f"{name}  (P={p:g})",
                    fontsize=7.5, color="#888888", va="bottom")
        # Jonsson "不得超过" 限值点：曲线位于这些点的右侧 = 该水平超标
        ax.plot([5.0, 14.0, 70.0], [0.1, 0.5, 0.9], ls="none", marker="x",
                ms=8, mew=1.6, color="#cc0000", zorder=4,
                label="Jonsson limit (must not exceed)")
        ax.legend(fontsize=8, loc="lower right", frameon=False)
        out = Path(out_dir) / "apdf_compare.png"
        FigureCanvasAgg(fig).print_png(str(out))
        print()
        print(f"  叠加图已保存：{out}")
        print("    同一肌肉的实线 vs 虚线差异 = 条件对该肌肉负荷分布的影响；")
        print("    曲线整体**右移**（同一 P 下幅值更高）= 该肌肉负荷变重；")
        print("    曲线若跑到红色 ×（Jonsson 限值点）的右侧 = 该水平超标。")
    except Exception as exc:                       # pragma: no cover - 绘图环境
        print(f"  （曲线图渲染失败：{exc}）")


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["gui"])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
