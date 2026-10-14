"""组合集成层测试：契约检查、插件配置生成、插件事件日志监控。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lsl_apdf.session_runner import (
    BalanceMonitor,
    IntegratedConfig,
    build_plugin_config,
    check_contract,
    port_is_open,
    session_dir_for,
)


class TestContract(unittest.TestCase):
    """契约检查要能把"静默什么都不做"变成"启动就报错"。"""

    def test_valid_config_passes(self):
        self.assertEqual(check_contract(IntegratedConfig()), [])

    def test_pair_muscle_not_in_channels(self):
        cfg = IntegratedConfig()
        cfg.pair = ["ECU", "BRACHIORADIALIS"]
        problems = check_contract(cfg)
        self.assertTrue(any("is not in the channels mapping" in p for p in problems), problems)
        self.assertTrue(any("direction mapping" in p for p in problems), problems)

    def test_missing_mvc_for_pair(self):
        cfg = IntegratedConfig(mvc={"ECU": 80.0, "UT": 83.0, "EDC": 59.0})
        problems = check_contract(cfg)
        self.assertTrue(any("Missing MVC baseline for FDS" in p for p in problems), problems)

    def test_zero_or_negative_mvc(self):
        cfg = IntegratedConfig(mvc={"ECU": 80.0, "FDS": 0.0,
                                    "UT": 83.0, "EDC": 59.0})
        self.assertTrue(any("must be positive" in p for p in check_contract(cfg)))

    def test_bad_direction(self):
        cfg = IntegratedConfig()
        cfg.direction = {"ECU": "sideways", "FDS": "down"}
        self.assertTrue(any("must be up/down" in p for p in check_contract(cfg)))

    def test_bad_port_and_send_hz(self):
        self.assertTrue(any("Invalid port" in p
                            for p in check_contract(IntegratedConfig(port=0))))
        self.assertTrue(any("send_hz" in p
                            for p in check_contract(IntegratedConfig(send_hz=0))))

    def test_plugin_dir_checked_when_launching(self):
        cfg = IntegratedConfig(plugin_dir=str(Path(tempfile.gettempdir()) / "nope-xyz"))
        self.assertTrue(any("main.py not found" in p for p in check_contract(cfg)))
        # 不自己启动插件时就不该因为目录问题报错
        cfg.launch_plugin = False
        self.assertFalse(any("main.py not found" in p for p in check_contract(cfg)))

    def test_bad_compare_mode(self):
        problems = check_contract(IntegratedConfig(compare="mean"))
        self.assertTrue(any("compare" in p for p in problems), problems)

    def test_compare_mode_goes_into_balance_block(self):
        cfg = IntegratedConfig(compare="absolute")
        block = cfg.balance_block()
        self.assertEqual(block["compare"], "absolute")
        self.assertEqual(IntegratedConfig().balance_block()["compare"], "auto")

    def test_compare_interval_in_balance_block(self):
        cfg = IntegratedConfig(compare_interval_sec=900.0)
        self.assertEqual(cfg.balance_block()["compare_interval_sec"], 900.0)
        self.assertEqual(
            IntegratedConfig().balance_block()["compare_interval_sec"], 0.0)

    def test_negative_compare_interval_rejected(self):
        problems = check_contract(IntegratedConfig(compare_interval_sec=-1.0))
        self.assertTrue(any("compare_interval" in p for p in problems), problems)


class TestPluginConfigGeneration(unittest.TestCase):
    def test_overrides_only_session_keys_and_keeps_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugin"
            plugin_dir.mkdir()
            template = {
                "ui": {"lang": "ja"},
                "mouse": {"base_dpi": 800, "levels": {"low": {}, "high": {}}},
                "rawaccel": {"install_dir": "C:/RawAccel", "dry_run": False,
                             "template_file_v17": "keep_me.json"},
                "log_dir": "logs",
                "session_cap_sec": 7200,
                "emg": {"protocol": "text", "policy": "threshold",
                        "opensignals": {"n_channels": 4}},
                "custom_field": {"keep": True},
            }
            (plugin_dir / "config.json").write_text(
                json.dumps(template), encoding="utf-8")

            cfg = IntegratedConfig(plugin_dir=str(plugin_dir), port=9123,
                                   dry_run=True, subject_id="S01")
            session_dir = Path(tmp) / "sess"
            session_dir.mkdir()
            built = build_plugin_config(cfg, session_dir)

            # 被本会话覆写的键
            self.assertEqual(built["emg"]["policy"], "balance")
            self.assertEqual(built["emg"]["port"], 9123)
            self.assertTrue(built["rawaccel"]["dry_run"])
            self.assertEqual(built["emg"]["balance"]["pair"], ["ECU", "FDS"])
            self.assertEqual(float(built["emg"]["switch_ramp_sec"]), 300.0)
            self.assertEqual(built["log_dir"], str(session_dir.resolve()))
            self.assertEqual(built["time_scale"], 1.0)
            self.assertEqual(built["ui"]["lang"], "en")
            # balance 的事件日志必须是绝对路径且落在会话目录里
            ev = Path(built["emg"]["balance"]["event_log"])
            self.assertTrue(ev.is_absolute())
            self.assertEqual(ev.parent, session_dir.resolve())

            # 模板里的其它字段必须原样保留
            self.assertEqual(built["rawaccel"]["install_dir"], "C:/RawAccel")
            self.assertEqual(built["rawaccel"]["template_file_v17"], "keep_me.json")
            self.assertEqual(built["emg"]["opensignals"], {"n_channels": 4})
            self.assertEqual(built["mouse"]["base_dpi"], 800)
            self.assertEqual(built["session_cap_sec"], 7200)
            self.assertEqual(built["custom_field"], {"keep": True})

    def test_missing_template_still_produces_usable_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "s"
            session_dir.mkdir()
            cfg = IntegratedConfig(plugin_dir=str(Path(tmp) / "empty"))
            built = build_plugin_config(cfg, session_dir)
            self.assertEqual(built["emg"]["policy"], "balance")
            self.assertEqual(built["emg"]["balance"]["pair"], ["ECU", "FDS"])

    def test_generated_config_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IntegratedConfig(plugin_dir=tmp)
            built = build_plugin_config(cfg, Path(tmp))
            text = json.dumps(built, ensure_ascii=False)
            self.assertIn("balance", text)


class TestSessionDir(unittest.TestCase):
    def test_unique_dirs_with_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IntegratedConfig(out_dir=tmp, subject_id="FU")
            a = session_dir_for(cfg, when=1700000000)
            b = session_dir_for(cfg, when=1700000000)
            self.assertNotEqual(a, b)
            self.assertIn("FU", a.name)
            self.assertTrue(a.is_dir())

    def test_subject_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IntegratedConfig(out_dir=tmp, subject_id="A/B C")
            d = session_dir_for(cfg, when=1700000000)
            self.assertNotIn("/", d.name)
            self.assertNotIn(" ", d.name)


class TestBalanceMonitor(unittest.TestCase):
    HEADER = ("t_sim,event,A_muscle,B_muscle,rate_A,rate_B,base_A,base_B,"
              "r_A,r_B,delta,deadband,high_thr,level,action,reason\n")

    def _write(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def test_parses_reason_with_commas(self):
        """reason 里有逗号（..., confirm=5s），必须用 csv 解析而不是 split(',')。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            mon = BalanceMonitor(d)
            self.assertEqual(mon.poll(), [])          # 文件还不存在 → 不崩
            log = d / "balance_events_20260101_000000.csv"
            self._write(log, self.HEADER)
            self.assertEqual(mon.poll(), [])          # 只有表头 → 不崩
            self._write(log, (
                '21.2,BASELINE_READY,ECU,FDS,,,9.72,0.00,,,,0.15,20.0,low,hold,'
                'mode=absolute base_ECU=9.72 base_FDS=0.00\n'
                '26.3,SWITCH,ECU,FDS,8.1,1.4,9.72,0.00,8.1,1.4,6.7,0.15,20.0,low,up,'
                '"balance_up(absolute: r_ECU=8.1 r_FDS=1.4 delta=+6.7 >=2, confirm=5s)"\n'
            ))
            fresh = mon.poll()
            self.assertEqual(len(fresh), 2)
            self.assertEqual(fresh[1]["event"], "SWITCH")
            self.assertIn("confirm=5s", fresh[1]["reason"])
            self.assertEqual(mon.level, "high")
            self.assertEqual(mon.switches, 1)
            self.assertEqual(mon.mode, "absolute")

            # 增量：再来一条 CROSSING
            self._write(log, (
                '36.3,CROSSING,ECU,FDS,4.8,5.1,9.72,0.00,4.8,5.1,-0.5,0.15,20.0,'
                'high,hold,"cross(B>) delta=-0.47"\n'
            ))
            self.assertEqual(len(mon.poll()), 1)
            self.assertEqual(mon.crossings, 1)
            self.assertEqual(mon.level, "high")     # CROSSING 不改档位

    def test_partial_line_waits_for_completion(self):
        """写到一半的行不能被当成数据（否则列数不符、解析出垃圾）。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            mon = BalanceMonitor(d)
            log = d / "balance_events_x.csv"
            self._write(log, self.HEADER)
            mon.poll()
            self._write(log, "12.0,SWITCH,ECU,FDS,1.0,0.5,9,0")   # 半行，无换行
            self.assertEqual(mon.poll(), [])
            self._write(log, ",1.0,0.5,0.5,0.15,20.0,low,up,ok\n")  # 补完
            fresh = mon.poll()
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]["event"], "SWITCH")

    def test_empty_and_repeated_poll_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            mon = BalanceMonitor(tmp)
            for _ in range(3):
                self.assertEqual(mon.poll(), [])
            self.assertEqual(mon.summary_line().count("switches=0"), 1)

    def test_level_follows_last_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            log = d / "balance_events_y.csv"
            self._write(log, self.HEADER)
            self._write(log,
                        "1,BASELINE_READY,,,,,,,,,,,,low,hold,mode=ratio\n"
                        "2,SWITCH,,,,,,,,,,,,low,up,balance_up\n"
                        "3,SWITCH,,,,,,,,,,,,high,down,balance_down\n")
            mon = BalanceMonitor(d)
            mon.poll()
            self.assertEqual(mon.level, "low")
            self.assertEqual(mon.switches, 2)

    def test_wrong_column_count_is_rejected(self):
        """列数不符的行必须丢弃（协议变了对齐错位比报错更危险）。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            log = d / "balance_events_z.csv"
            self._write(log, self.HEADER)
            self._write(log, "1,SWITCH,,,too,few,cols\n"
                             "2,BASELINE_READY,,,,,,,,,,,,low,hold,mode=ratio\n")
            mon = BalanceMonitor(d)
            fresh = mon.poll()
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]["event"], "BASELINE_READY")


class TestPortHelpers(unittest.TestCase):
    def test_closed_port_is_false(self):
        self.assertFalse(port_is_open("127.0.0.1", 1, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
