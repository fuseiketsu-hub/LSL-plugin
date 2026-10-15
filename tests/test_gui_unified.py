"""统一界面（单窗口）冒烟测试：真正建窗、跑引擎、刷新若干帧。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from lsl_apdf.session_runner import IntegratedConfig


class TestUnifiedGui(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter

            self.root = tkinter.Tk()
        except Exception as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"无法创建 Tk 窗口：{exc}")
        self.root.withdraw()          # 不弹窗打扰
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _cfg(self) -> IntegratedConfig:
        return IntegratedConfig(
            dry_run=True, out_dir=self.tmp, report_sec=1.0,
            roll_window_sec=8.0, baseline_sec=8.0, confirm_sec=2.0,
            settle_sec=0.0, switch_ramp_sec=4.0,
        )

    def test_window_builds_with_both_panes(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        app = UnifiedGuiApp(self.root, self._cfg())
        # 左侧一张图（精简版：只保留 APDF 曲线，%MVC 时序实时图已去除）
        self.assertIsNotNone(app.ax_apdf)
        # 右侧读数区
        for attr in ("dpi_label", "level_var", "apdf_tree", "log_text",
                     "mvc_row", "stream_combo", "start_btn"):
            self.assertTrue(hasattr(app, attr), attr)
        # 每块肌肉一个 MVC 输入框
        self.assertEqual(list(app._mvc_vars), ["ECU", "FDS", "UT", "EDC"])
        # 默认必须是 DRY-RUN
        self.assertFalse(app.real_var.get())
        self.assertIn("Start", app.status_var.get() + app.start_btn.cget("text"))

    def test_advanced_panel_hidden_by_default(self):
        """高级设置默认折叠 —— 主界面保持简单可读。"""
        from lsl_apdf.gui_unified import UnifiedGuiApp

        app = UnifiedGuiApp(self.root, self._cfg())
        self.assertFalse(app._adv_open)
        self.assertEqual(app.adv.winfo_manager(), "")   # 未 pack
        app._toggle_advanced()
        self.assertTrue(app._adv_open)
        self.assertEqual(app.adv.winfo_manager(), "pack")
        app._toggle_advanced()
        self.assertFalse(app._adv_open)
        self.assertEqual(app.adv.winfo_manager(), "")

    def test_mvc_boxes_rebuild_with_channel_mapping(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        app = UnifiedGuiApp(self.root, self._cfg())
        app.ch_var.set("1=FDS,2=UT")
        app._rebuild_mvc_boxes()
        self.assertEqual(list(app._mvc_vars), ["FDS", "UT"])

    def test_read_form_takes_mvc_from_boxes(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        cfg = self._cfg()
        app = UnifiedGuiApp(self.root, cfg)
        app._mvc_vars["ECU"].set("123.5")
        app._mvc_vars["FDS"].set("77")
        self.assertTrue(app._read_form())
        self.assertAlmostEqual(cfg.mvc["ECU"], 123.5)
        self.assertAlmostEqual(cfg.mvc["FDS"], 77.0)

        # 非数字 → 拒绝启动并解释
        app._mvc_vars["UT"].set("abc")
        import tkinter.messagebox as mb

        captured = {}
        orig = mb.showerror
        mb.showerror = lambda title, msg, **k: captured.update(msg=msg)
        try:
            self.assertFalse(app._read_form())
        finally:
            mb.showerror = orig
        self.assertIn("UT", captured.get("msg", ""))

    def test_engine_runs_and_snapshot_fills(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp
        import tkinter.messagebox as mb

        # 引擎异常时 _poll 会弹模态框，测试里改成记录，避免卡住
        shown = {}
        orig = mb.showerror
        mb.showerror = lambda title, msg, **k: shown.update(title=title, msg=msg)
        try:
            cfg = self._cfg()
            app = UnifiedGuiApp(self.root, cfg)
            app._start()
            engine = app.engine
            self.assertIsNotNone(engine, f"引擎未启动：{shown}")
            deadline = time.time() + 30.0
            while time.time() < deadline:
                self.root.update()
                if engine.error:
                    break
                snap = engine.snapshot()
                if snap.elapsed > 12.0 and snap.apdf:
                    break
                time.sleep(0.05)

            self.assertIsNone(engine.error, f"引擎异常：{engine.error}")
            self.assertNotIn("msg", shown, f"界面弹错：{shown}")
            snap = engine.snapshot()
            self.assertGreater(snap.elapsed, 5.0)
            self.assertIn("ECU", snap.latest)
            self.assertTrue(snap.curves, "APDF 曲线应由工作线程算好放进快照")
            self.assertGreater(snap.effective_dpi, 0)
            app._render(snap)             # 渲染一帧不应抛异常
            app._stop()
            engine.join(timeout=15.0)
            # 收尾阶段（finally）也要检查：之前这里有个 NameError（漏 import json），
            # 只在 stop **之后**才发生，所以循环里查 engine.error 抓不到。
            self.assertIsNone(engine.error, f"收尾阶段异常：{engine.error}")
            # 界面会话必须写出与命令行会话**相同**的产物，
            # 否则 compare-direction 拿到界面跑的目录会说"找不到 load_disparity.csv"。
            for name in ("apdf_summary.csv", "apdf_curve.csv",
                         "load_disparity.csv", "session_summary.json",
                         "pctmvc.csv"):
                self.assertTrue((engine.session_dir / name).exists(),
                                f"缺少产物 {name}")
        finally:
            mb.showerror = orig

    def test_form_validation_rejects_same_muscle_pair(self):
        """对比的两块必须不同 —— 选成同一块要拒绝并说明。"""
        from lsl_apdf.gui_unified import UnifiedGuiApp

        app = UnifiedGuiApp(self.root, self._cfg())
        app.pair_b_var.set(app.pair_a_var.get())
        import tkinter.messagebox as mb

        captured = {}
        orig = mb.showerror
        mb.showerror = lambda title, msg, **k: captured.update(title=title, msg=msg)
        try:
            ok = app._read_form()
        finally:
            mb.showerror = orig
        self.assertFalse(ok)
        self.assertIn("different", captured.get("msg", ""))

    def test_pair_selection_is_read_into_config(self):
        """主界面选的 A/B 两块必须写进 cfg.pair（判定只用这两块）。"""
        from lsl_apdf.gui_unified import UnifiedGuiApp

        cfg = self._cfg()
        app = UnifiedGuiApp(self.root, cfg)
        app.pair_a_var.set("FDS")
        app.pair_b_var.set("UT")
        self.assertTrue(app._read_form())
        self.assertEqual(cfg.pair, ["FDS", "UT"])

    def test_pair_combos_list_all_muscles_and_default_from_config(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        cfg = self._cfg()
        cfg.pair = ["ECU", "FDS"]
        app = UnifiedGuiApp(self.root, cfg)
        self.assertEqual(list(app.pair_a_combo["values"]),
                         ["ECU", "FDS", "UT", "EDC"])
        self.assertEqual(app.pair_a_var.get(), "ECU")
        self.assertEqual(app.pair_b_var.get(), "FDS")
        # A 与 B 不会相同
        app.pair_a_var.set("ECU")
        app._rebuild_pair_boxes()
        self.assertNotEqual(app.pair_a_var.get(), app.pair_b_var.get())

    def test_pair_rebuilds_when_mapping_changes(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        cfg = self._cfg()
        app = UnifiedGuiApp(self.root, cfg)
        app.ch_var.set("1=FDS,2=UT")
        app._apply_mapping()
        self.assertEqual(list(app.pair_a_combo["values"]), ["FDS", "UT"])
        self.assertIn(app.pair_a_var.get(), ["FDS", "UT"])
        self.assertIn(app.pair_b_var.get(), ["FDS", "UT"])
        self.assertNotEqual(app.pair_a_var.get(), app.pair_b_var.get())

    def test_form_reads_values(self):
        from lsl_apdf.gui_unified import UnifiedGuiApp

        cfg = self._cfg()
        app = UnifiedGuiApp(self.root, cfg)
        app.thr_var.set("25")
        app.db_var.set("0.2")
        app.ramp_var.set("30")
        app.real_var.set(False)
        self.assertTrue(app._read_form())
        self.assertAlmostEqual(cfg.high_thr, 25.0)
        self.assertAlmostEqual(cfg.deadband, 0.2)
        self.assertAlmostEqual(cfg.switch_ramp_sec, 30.0)
        self.assertTrue(cfg.dry_run)


class TestSourceSelection(unittest.TestCase):
    """数据源选择：默认合成；选中真实流时建 LSLSource。"""

    def setUp(self):
        try:
            import tkinter

            self.root = tkinter.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"无法创建 Tk 窗口：{exc}")
        self.root.withdraw()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_defaults_to_synthetic_when_no_stream(self):
        from lsl_apdf.gui_unified import SYNTHETIC_ENTRY, UnifiedGuiApp

        app = UnifiedGuiApp(self.root, self._cfg())
        # 无硬件、无 pylsl 流可发现时，退回合成自检
        app._refresh_streams()
        if not app._streams:
            self.assertEqual(app.stream_var.get(), SYNTHETIC_ENTRY)
            self.assertIsNone(app._selected_stream())
            self.assertIn("Synthetic", app._build_source().__class__.__name__)

    def test_real_stream_is_listed_first_and_autoselected(self):
        """正常用法：接上 OpenSignals → 刷新 → 直接就能点开始（少一步点击）。"""
        from lsl_apdf.gui_unified import (IMPORT_ENTRY, SYNTHETIC_ENTRY,
                                          UnifiedGuiApp)
        from lsl_apdf.sources import StreamMeta

        app = UnifiedGuiApp(self.root, self._cfg())
        exported = [StreamMeta(name="OpenSignals", type="00:07:80:AA:BB:CC",
                               hostname="MyPC", n_channels=4, nominal_srate=1000.0)]

        import lsl_apdf.gui_unified as gui_mod

        exported = [StreamMeta(name="OpenSignals", type="00:07:80:AA:BB:CC",
                               hostname="MyPC", n_channels=4, nominal_srate=1000.0)]
        orig_discover = gui_mod.discover_streams
        orig_flag = gui_mod.PYLSL_AVAILABLE
        gui_mod.discover_streams = lambda wait_time=1.0: exported
        gui_mod.PYLSL_AVAILABLE = True
        try:
            app._refresh_streams()
        finally:
            gui_mod.discover_streams = orig_discover
            gui_mod.PYLSL_AVAILABLE = orig_flag

        values = list(app.stream_combo["values"])
        self.assertEqual(len(values), 3)
        self.assertIn("OpenSignals", values[0])          # 真实流在前
        self.assertEqual(values[-2], IMPORT_ENTRY)       # 导入录制在中间
        self.assertEqual(values[-1], SYNTHETIC_ENTRY)    # 合成信号在最后兜底
        self.assertIn("OpenSignals", app.stream_var.get())  # 自动选中
        meta = app._selected_stream()
        self.assertIsNotNone(meta)
        self.assertEqual(meta.type, "00:07:80:AA:BB:CC")
        self.assertEqual(app._build_source().cfg.resolve_property(),
                         ("type", "00:07:80:AA:BB:CC"))

    def test_selected_stream_builds_lsl_source(self):
        from lsl_apdf.gui_unified import SYNTHETIC_ENTRY, UnifiedGuiApp
        from lsl_apdf.sources import StreamMeta

        app = UnifiedGuiApp(self.root, self._cfg())
        mac = "00:07:80:AA:BB:CC"
        app._streams = [StreamMeta(name="OpenSignals", type=mac,
                                   n_channels=4, nominal_srate=1000.0)]
        app.stream_combo["values"] = [f"OpenSignals | {mac}", SYNTHETIC_ENTRY]
        app.stream_combo.current(0)
        meta = app._selected_stream()
        self.assertIsNotNone(meta)
        self.assertEqual(meta.type, mac)
        src = app._build_source()
        self.assertIn("LSL", src.__class__.__name__)
        # 回归测试：必须按 MAC(type) 重新解析，而不是把自己的 StreamMeta 当成
        # pylsl.StreamInfo 传进去（那会 AttributeError: 'StreamMeta' object has no attribute 'obj'）
        self.assertEqual(src.cfg.resolve_property(), ("type", mac))

    def test_selected_stream_falls_back_to_hostname_or_name(self):
        from lsl_apdf.gui_unified import SYNTHETIC_ENTRY, UnifiedGuiApp
        from lsl_apdf.sources import StreamMeta

        app = UnifiedGuiApp(self.root, self._cfg())
        app._streams = [StreamMeta(name="OS", type="", hostname="MyPC",
                                   n_channels=4, nominal_srate=1000.0)]
        app.stream_combo["values"] = ["y", SYNTHETIC_ENTRY]
        app.stream_combo.current(0)
        self.assertEqual(app._build_source().cfg.resolve_property(),
                         ("hostname", "MyPC"))

        app._streams = [StreamMeta(name="OpenSignals", type="", hostname="",
                                   n_channels=4, nominal_srate=1000.0)]
        app.stream_combo.current(0)
        self.assertEqual(app._build_source().cfg.resolve_property(),
                         ("name", "OpenSignals"))

    def test_synthetic_selected_explicitly(self):
        from lsl_apdf.gui_unified import SYNTHETIC_ENTRY, UnifiedGuiApp
        from lsl_apdf.sources import StreamMeta

        app = UnifiedGuiApp(self.root, self._cfg())
        app._streams = [StreamMeta(name="OS", type="AA", n_channels=4)]
        app.stream_combo["values"] = ["OS | AA", SYNTHETIC_ENTRY]
        app.stream_combo.current(1)                    # 手动选合成
        self.assertIsNone(app._selected_stream())
        self.assertIn("Synthetic", app._build_source().__class__.__name__)

    def test_channel_bounds_checked_against_selected_stream(self):
        """选了只有 2 路的流却映射 4 路 → 必须报错而不是静默跑错数据。"""
        from lsl_apdf.channels import resolve_channel_index
        from lsl_apdf.sources import StreamMeta

        meta = StreamMeta(name="OS", n_channels=2,
                          channel_labels=["A1", "A2"], nominal_srate=1000.0)
        with self.assertRaises(ValueError) as ctx:
            resolve_channel_index("A3", meta)
        self.assertIn("only 2 channels", str(ctx.exception))

    def test_stream_config_from_ignores_unknown_keys(self):
        from lsl_apdf.gui_unified import stream_config_from

        cfg = self._cfg()
        cfg.stream = {"name": "OpenSignals", "type": "AA:BB", "未来字段": 1}
        sc = stream_config_from(cfg)
        self.assertEqual(sc.name, "OpenSignals")
        self.assertEqual(sc.resolve_property(), ("type", "AA:BB"))

    def _os_fixture(self, tmp, n: int = 3000) -> Path:
        path = Path(tmp) / "opensignals_rec.txt"
        rows = [
            "# OpenSignals Text File Format. Version 1",
            '# {"AA:BB": {"sampling rate": 1000, "device name": "AA:BB", '
            '"column": ["nSeq", "A1", "A2", "A3", "A4"], '
            '"label": ["A1", "A2", "A3", "A4"]}}',
            "# EndOfHeader",
        ]
        for i in range(n):
            rows.append(
                f"{i % 16}\t{500 + (i % 7)}\t{510 + (i % 5)}"
                f"\t{495 + (i % 3)}\t{505 + (i % 11)}")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_import_entry_builds_file_source(self):
        from lsl_apdf.gui_unified import IMPORT_ENTRY, UnifiedGuiApp
        from lsl_apdf.sources import OpenSignalsSource

        app = UnifiedGuiApp(self.root, self._cfg())
        app._refresh_streams()
        self.assertIn(IMPORT_ENTRY, list(app.stream_combo["values"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = self._os_fixture(tmp, n=10)
            app.stream_combo.current(len(app._streams))   # 选中导入项
            app._import_path = path
            src = app._build_source()
            self.assertIsInstance(src, OpenSignalsSource)
            self.assertEqual(src.path, path)

    def test_engine_import_file_auto_finishes(self):
        """导入录制：数据读完自动收尾，最终 APDF 曲线可用于渲染。"""
        from lsl_apdf.gui_unified import UnifiedGuiApp
        import tkinter.messagebox as mb

        shown = {}
        orig = mb.showerror
        mb.showerror = lambda title, msg, **k: shown.update(title=title, msg=msg)
        try:
            cfg = self._cfg()
            app = UnifiedGuiApp(self.root, cfg)
            with tempfile.TemporaryDirectory() as tmp:
                path = self._os_fixture(tmp)
                app._refresh_streams()
                app.stream_combo.current(len(app._streams))
                app._import_path = path
                app._start()
                engine = app.engine
                self.assertIsNotNone(engine, f"引擎未启动：{shown}")
                deadline = time.time() + 40.0
                while time.time() < deadline and engine.is_alive():
                    self.root.update()
                    time.sleep(0.05)
                self.assertFalse(engine.is_alive(),
                                 "导入回放应在读完文件后自动结束")
                self.assertTrue(engine.summary)
                snap = engine.snapshot()
                self.assertTrue(snap.curves, "最终 APDF 曲线应写进快照")
                app._render(snap, force=True)       # 收尾帧必须能画出来
                self.assertNotIn("msg", shown, f"界面弹错：{shown}")
        finally:
            mb.showerror = orig

    def _cfg(self) -> IntegratedConfig:
        return IntegratedConfig(dry_run=True, out_dir=self.tmp)


if __name__ == "__main__":
    unittest.main()
