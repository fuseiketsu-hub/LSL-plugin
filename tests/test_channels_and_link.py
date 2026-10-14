"""多通道 %MVC 与 Part2→Part3 传输段的测试。"""

from __future__ import annotations

import csv
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from lsl_apdf.channels import (
    MultiChannelMvc,
    MuscleChannel,
    parse_channel_spec,
    parse_mvc_spec,
    resolve_channel_index,
)
from lsl_apdf.config import SignalConfig
from lsl_apdf.emg_link import MvcCsvRecorder, MvcTcpSender, format_line
from lsl_apdf.sources import StreamMeta, SyntheticProfile, SyntheticSource


# --------------------------------------------------------------------------- #
class TestSpecParsing(unittest.TestCase):
    def test_channel_spec_string(self):
        got = parse_channel_spec("A1=ECU,A2=FDS,A3=UT,A4=EDC")
        self.assertEqual(got, {"A1": "ECU", "A2": "FDS", "A3": "UT", "A4": "EDC"})

    def test_channel_spec_list_and_spaces(self):
        got = parse_channel_spec(["A1 = ECU", " A2=FDS "])
        self.assertEqual(got, {"A1": "ECU", "A2": "FDS"})

    def test_channel_spec_dict(self):
        self.assertEqual(parse_channel_spec({"A1": "ECU"}), {"A1": "ECU"})

    def test_channel_spec_invalid(self):
        with self.assertRaises(ValueError):
            parse_channel_spec("A1-ECU")
        with self.assertRaises(ValueError):
            parse_channel_spec("ECU")

    def test_mvc_spec(self):
        self.assertEqual(parse_mvc_spec("ECU=80,FDS=57"), {"ECU": 80.0, "FDS": 57.0})
        self.assertEqual(parse_mvc_spec({"ECU": 80}), {"ECU": 80.0})

    def test_mvc_spec_invalid(self):
        with self.assertRaises(ValueError):
            parse_mvc_spec("ECU")
        with self.assertRaises(ValueError):
            parse_mvc_spec("ECU=abc")


class TestResolveChannel(unittest.TestCase):
    def test_by_a_number(self):
        self.assertEqual(resolve_channel_index("A1"), 0)
        self.assertEqual(resolve_channel_index("a4"), 3)

    def test_by_plain_number_and_ch(self):
        self.assertEqual(resolve_channel_index("1"), 0)
        self.assertEqual(resolve_channel_index("3"), 2)
        self.assertEqual(resolve_channel_index("ch2"), 1)

    def test_by_stream_label_wins(self):
        meta = StreamMeta(n_channels=4, channel_labels=["EMG_A", "EMG_B", "EMG_C", "EMG_D"])
        self.assertEqual(resolve_channel_index("emg_c", meta), 2)

    def test_invalid_token_has_hint(self):
        meta = StreamMeta(n_channels=2, channel_labels=["EMG1", "EMG2"])
        with self.assertRaises(ValueError) as ctx:
            resolve_channel_index("肌肉X", meta)
        self.assertIn("EMG1", str(ctx.exception))
        self.assertIn("Supported", str(ctx.exception))

    def test_out_of_range_channel_rejected_early(self):
        """越界通道要在解析阶段就报错，而不是采集中途抛 IndexError。"""
        meta = StreamMeta(n_channels=2, channel_labels=["A1", "A2"])
        with self.assertRaises(ValueError) as ctx:
            resolve_channel_index("A9", meta)
        self.assertIn("only 2 channels", str(ctx.exception))
        # 不给 meta 时不做越界检查（无法判断）
        self.assertEqual(resolve_channel_index("A9"), 8)


# --------------------------------------------------------------------------- #
class TestMultiChannelMvc(unittest.TestCase):
    """核心：每块肌肉的 %MVC 必须只取决于自己那一通道 ÷ 自己的 MVC。"""

    def _run_pipeline(self, profiles, mvc, labels, srate=1000.0, seconds=6.0, cfg=None):
        src = SyntheticSource(
            profiles=profiles, channel_labels=labels, n_channels=len(profiles),
            srate=srate, realtime=False, channel_unit="mV",
        )
        meta = src.connect()
        pipeline = MultiChannelMvc.from_spec(
            fs=srate,
            channel_spec={labels[i]: m for i, m in enumerate(mvc)},
            mvc_spec=mvc,
            meta=meta,
            signal_cfg=cfg or SignalConfig(adc_scale=1.0, adc_offset=0.0,
                                           unit_label="mV"),
        )
        all_pct: dict[str, list] = {m: [] for m in mvc}
        while src.n_samples < int(seconds * srate):
            chunk = src.read_chunk()
            _, values = pipeline.push(chunk.data, chunk.timestamps)
            for m, series in values.items():
                if series.size:
                    all_pct[m].append(series)
        return pipeline, {m: np.concatenate(v) for m, v in all_pct.items() if v}

    def test_percent_mvc_matches_envelope(self):
        # mve_unit=1000、mvc=1000 → 包络值(%=RMS/1000*100) 就等于 %MVC
        profs = [
            SyntheticProfile(segments=[(10.0, 30.0)], noise_floor_pct=0.0),
            SyntheticProfile(segments=[(10.0, 10.0)], noise_floor_pct=0.0),
        ]
        _, pct = self._run_pipeline(profs, {"ECU": 1000.0, "FDS": 1000.0},
                                    ["A1", "A2"], seconds=8.0)
        self.assertAlmostEqual(float(np.median(pct["ECU"])), 30.0, delta=2.0)
        self.assertAlmostEqual(float(np.median(pct["FDS"])), 10.0, delta=2.0)

    def test_mvc_scaling_is_per_muscle(self):
        """同一路信号，MVC 分母不同 → %MVC 不同（这正是必须各自归一化的原因）。"""
        profs = [SyntheticProfile(segments=[(10.0, 20.0)], noise_floor_pct=0.0)]
        _, pct = self._run_pipeline(profs, {"ECU": 1000.0}, ["A1"], seconds=6.0)
        base = float(np.median(pct["ECU"]))

        profs2 = [SyntheticProfile(segments=[(10.0, 20.0)], noise_floor_pct=0.0)]
        _, pct2 = self._run_pipeline(profs2, {"ECU": 500.0}, ["A1"], seconds=6.0)
        self.assertAlmostEqual(float(np.median(pct2["ECU"])), base * 2, delta=3.0)

    def test_channels_are_independent(self):
        """改一条通道的负荷，不能影响另一块肌肉的 %MVC。

        A1→ECU、A2→FDS，所以把"响"的那条轮廓放在第 0 位就是在抬 ECU。
        """
        quiet = SyntheticProfile(segments=[(10.0, 10.0)], noise_floor_pct=0.0)
        loud = SyntheticProfile(segments=[(10.0, 45.0)], noise_floor_pct=0.0)
        _, a = self._run_pipeline([quiet, quiet], {"ECU": 1000.0, "FDS": 1000.0},
                                  ["A1", "A2"], seconds=6.0)
        _, b = self._run_pipeline([loud, quiet], {"ECU": 1000.0, "FDS": 1000.0},
                                  ["A1", "A2"], seconds=6.0)
        # 通道 1（FDS）没变
        self.assertAlmostEqual(float(np.median(a["FDS"])), float(np.median(b["FDS"])),
                               delta=3.0)
        # 通道 0（ECU）按预期抬高
        self.assertGreater(float(np.median(b["ECU"])), float(np.median(a["ECU"])) + 20.0)
        self.assertAlmostEqual(float(np.median(b["ECU"])), 45.0, delta=3.0)

    def test_missing_mvc_gives_nan_not_crash(self):
        profs = [SyntheticProfile(segments=[(10.0, 20.0)], noise_floor_pct=0.0)]
        pipeline, pct = self._run_pipeline(profs, {"ECU": 1000.0}, ["A1"], seconds=4.0)
        self.assertEqual(pipeline.missing_mvc, [])
        # 不给 MVC 的肌肉
        src = SyntheticSource(profiles=[profs[0]], channel_labels=["A1"],
                              n_channels=1, srate=1000.0, realtime=False)
        meta = src.connect()
        p2 = MultiChannelMvc.from_spec(fs=1000.0, channel_spec={"A1": "ECU"},
                                       mvc_spec=None, meta=meta)
        self.assertEqual(p2.missing_mvc, ["ECU"])
        chunk = src.read_chunk()
        _, values = p2.push(chunk.data, chunk.timestamps)
        self.assertTrue(np.all(~np.isfinite(values["ECU"])))
        self.assertEqual(p2.latest(values), {})

    def test_latest_skips_nan(self):
        ch = MuscleChannel(muscle="ECU", index=0, mvc=float("nan"))
        p = MultiChannelMvc(fs=500.0, channels=[ch])
        _, values = p.push(np.ones((2000, 1)), np.arange(2000) / 500.0)
        self.assertEqual(p.latest(values), {})

    def test_channel_out_of_range(self):
        ch = MuscleChannel(muscle="ECU", index=5, mvc=1000.0)
        p = MultiChannelMvc(fs=500.0, channels=[ch])
        with self.assertRaises(IndexError) as ctx:
            p.push(np.ones((100, 1)))
        self.assertIn("out of range", str(ctx.exception))

    def test_describe_mentions_missing(self):
        ch = [MuscleChannel("ECU", 0, "A1", 1000.0), MuscleChannel("FDS", 1, "A2")]
        p = MultiChannelMvc(fs=500.0, channels=ch)
        self.assertIn("missing MVC", p.describe())

    def test_empty_and_bad_params(self):
        with self.assertRaises(ValueError):
            MultiChannelMvc(fs=0.0, channels=[MuscleChannel("ECU", 0, mvc=1.0)])
        with self.assertRaises(ValueError):
            MultiChannelMvc(fs=500.0, channels=[])
        p = MultiChannelMvc(fs=500.0, channels=[MuscleChannel("ECU", 0, mvc=1000.0)])
        t, v = p.push(np.empty((0, 1)))
        self.assertEqual(t.size, 0)

    def test_from_spec_reports_resolved_channels(self):
        meta = StreamMeta(n_channels=4, channel_labels=["A1", "A2", "A3", "A4"])
        p = MultiChannelMvc.from_spec(
            fs=1000.0,
            channel_spec="A1=ECU,A3=FDS",
            mvc_spec="ECU=80,FDS=57",
            meta=meta,
        )
        self.assertEqual([c.index for c in p.channels], [0, 2])
        self.assertEqual(p.muscles, ["ECU", "FDS"])
        self.assertIn("ECU(MVC=80)", p.describe())


# --------------------------------------------------------------------------- #
class TestLineFormatting(unittest.TestCase):
    def test_json(self):
        line = format_line({"ECU": 12.3456, "FDS": 7.0}, "json")
        self.assertEqual(json.loads(line), {"ECU": 12.3456, "FDS": 7.0})

    def test_pairs(self):
        self.assertEqual(format_line({"ECU": 12.5, "FDS": 7.0}, "pairs"),
                         "ECU 12.5 FDS 7")

    def test_comma(self):
        self.assertEqual(format_line({"ECU": 12.5, "FDS": 7.0}, "comma"),
                         "ECU=12.5,FDS=7")

    def test_unknown_protocol(self):
        with self.assertRaises(ValueError):
            format_line({"ECU": 1.0}, "xml")

    def test_plugin_parser_accepts_our_lines(self):
        """最关键的兼容性测试：DPI 插件自己的 parse_line 必须能解析我们发的行。"""
        import sys

        plugin_dir = Path(__file__).resolve().parents[1] / "plugin"
        if not (plugin_dir / "emg_server.py").exists():
            self.skipTest("未找到 DPI 插件目录，跳过跨项目兼容性测试")
        sys.path.insert(0, str(plugin_dir))
        try:
            from emg_server import parse_line  # type: ignore
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"无法导入插件模块：{exc}")
        finally:
            sys.path.remove(str(plugin_dir))

        values = {"EDC": 11.2, "ECU": 12.5, "FDS": 13.1, "UT": 6.0}
        for protocol in ("json", "pairs", "comma"):
            parsed = parse_line(format_line(values, protocol))
            self.assertIsNotNone(parsed, f"{protocol} 行没被插件解析出来")
            for k, v in values.items():
                self.assertAlmostEqual(parsed[k], v, places=4)


# --------------------------------------------------------------------------- #
class _FakeServer(threading.Thread):
    """一个最小的 TCP 服务器，收集收到的行（模拟 DPI 插件的 emg_server）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.lines: list[str] = []
        self._stop = threading.Event()
        self.drop_after = None

    def run(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._serve(conn)

    def _serve(self, conn):
        buf = b""
        conn.settimeout(0.3)
        n = 0
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                if self.drop_after is not None and n >= self.drop_after:
                    break
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.lines.append(line.decode("utf-8"))
                n += 1
                if self.drop_after is not None and n >= self.drop_after:
                    conn.close()
                    return
        try:
            conn.close()
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class TestMvcTcpSender(unittest.TestCase):
    def test_sends_json_lines(self):
        srv = _FakeServer()
        srv.start()
        self.addCleanup(srv.stop)
        sender = MvcTcpSender(port=srv.port, retry_interval=0.01)
        self.addCleanup(sender.close)
        self.assertTrue(sender.connect(force=True))
        for i in range(5):
            self.assertTrue(sender.send({"ECU": 20.0 + i, "FDS": 10.0}))
        deadline = time.time() + 2.0
        while len(srv.lines) < 5 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(srv.lines), 5)
        self.assertEqual(json.loads(srv.lines[0]), {"ECU": 20.0, "FDS": 10.0})
        self.assertEqual(sender.stats.sent, 5)
        self.assertEqual(sender.stats.failed, 0)

    def test_no_server_is_not_fatal(self):
        """插件没启动时：不抛异常、只计数、限频重试。"""
        sender = MvcTcpSender(port=1, retry_interval=10.0, connect_timeout=0.2)
        self.addCleanup(sender.close)
        t0 = time.time()
        for _ in range(20):
            self.assertFalse(sender.send({"ECU": 1.0}))
        elapsed = time.time() - t0
        self.assertEqual(sender.stats.sent, 0)
        self.assertEqual(sender.stats.failed, 20)
        self.assertLess(elapsed, 1.0, "连不上时不应反复阻塞")
        self.assertIsNotNone(sender.last_error)

    def test_reconnect_after_server_drop(self):
        """对端断开后要能自己重连（TCP 首次写通常仍成功，故多写几次）。"""
        srv = _FakeServer()
        srv.drop_after = 2
        srv.start()
        self.addCleanup(srv.stop)
        sender = MvcTcpSender(port=srv.port, retry_interval=0.01)

        self.addCleanup(sender.close)
        sender.connect(force=True)
        deadline = time.time() + 3.0
        while sender.stats.reconnects < 1 and time.time() < deadline:
            sender.send({"ECU": 1.0})
            time.sleep(0.05)
        self.assertGreaterEqual(sender.stats.reconnects, 1,
                                f"未检测到断线重连：{sender.stats.as_dict()}")
        self.assertGreater(sender.stats.failed, 0)

    def test_recovers_after_reconnect(self):
        """重连之后还能继续正常发送（服务端重新 accept）。"""
        srv = _FakeServer()
        srv.drop_after = 1
        srv.start()
        self.addCleanup(srv.stop)
        sender = MvcTcpSender(port=srv.port, retry_interval=0.01)
        self.addCleanup(sender.close)
        sender.connect(force=True)
        deadline = time.time() + 4.0
        while sender.stats.reconnects < 1 and time.time() < deadline:
            sender.send({"ECU": 2.0})
            time.sleep(0.05)
        # 重连成功后继续发，服务端应收到不止一行
        for _ in range(10):
            sender.send({"ECU": 2.0})
            time.sleep(0.05)
        self.assertGreaterEqual(len(srv.lines), 2, f"lines={srv.lines}")

    def test_empty_values_skipped(self):
        sender = MvcTcpSender(port=1, retry_interval=10.0)
        self.addCleanup(sender.close)
        self.assertFalse(sender.send({}))
        self.assertEqual(sender.stats.skipped_empty, 1)

    def test_describe_and_context_manager(self):
        srv = _FakeServer()
        srv.start()
        self.addCleanup(srv.stop)
        with MvcTcpSender(port=srv.port, retry_interval=0.01) as sender:
            self.assertTrue(sender.connected)
            self.assertIn("connected", sender.describe())
            sender.send({"ECU": 5.0})
        self.assertFalse(sender.connected)

    def test_protocols_round_trip_through_server(self):
        srv = _FakeServer()
        srv.start()
        self.addCleanup(srv.stop)
        for proto in ("json", "pairs", "comma"):
            sender = MvcTcpSender(port=srv.port, protocol=proto, retry_interval=0.01)
            sender.connect(force=True)
            sender.send({"ECU": 12.5})
            sender.close()
        deadline = time.time() + 2.0
        while len(srv.lines) < 3 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(srv.lines), 3)
        self.assertEqual(json.loads(srv.lines[0]), {"ECU": 12.5})
        self.assertEqual(srv.lines[1], "ECU 12.5")
        self.assertEqual(srv.lines[2], "ECU=12.5")


class TestMvcCsvRecorder(unittest.TestCase):
    def test_writes_header_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "pctmvc.csv"
            rec = MvcCsvRecorder(path, ["ECU", "FDS"], flush_interval=0.0)
            rec.write(1000.5, {"ECU": 12.34, "FDS": 5.6}, t_wall=1700000000.25)
            rec.write(1000.625, {"ECU": 13.0})       # 缺 FDS → 空列，不错位
            rec.write(1000.75, {"ECU": float("nan")})
            rec.close()

            with path.open(encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            # t_wall 列用于和插件的 sens_log(unix_ms) 对齐
            self.assertEqual(rows[0], ["t_lsl", "t_wall", "ECU", "FDS"])
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[1][1], "1700000000.250000")
            self.assertEqual(rows[1][2], "12.3400")
            self.assertEqual(rows[1][3], "5.6000")
            self.assertEqual(rows[2][3], "")          # 缺失留空
            self.assertEqual(rec.rows, 3)

    def test_wall_column_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nowall.csv"
            rec = MvcCsvRecorder(path, ["ECU"], flush_interval=0.0,
                                 include_wall=False)
            rec.write(1.0, {"ECU": 1.0})
            rec.close()
            with path.open(encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows[0], ["t_lsl", "ECU"])
            self.assertEqual(rows[1], ["1.000000", "1.0000"])

    def test_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.csv"
            rec = MvcCsvRecorder(path, ["ECU"], flush_interval=0.0)
            rec.open()
            rec.open()
            rec.write(1.0, {"ECU": 1.0})
            rec.close()
            rec.close()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
