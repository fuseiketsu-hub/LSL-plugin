"""Part 2 → Part 3 的传输层：把每肌肉 %MVC 推给 DPI 插件。

DPI 插件（``插件\\plugin\\emg_server.py``）内置一个 TCP 服务器，等待**已处理好的
%MVC**（不是 ADC 原始值），每行一种格式：

.. code-block:: text

    {"EDC":11.2,"ECU":12.5,"FDS":13.1,"UT":6.0}      # JSON（默认用这个）
    EDC 11.2 ECU 12.5 FDS 13.1 UT 6.0                # 空格键值对
    EDC=11.2,ECU=12.5                                # 逗号键值对（可只发部分肌肉）

本模块只负责**发**（以及可选地把 %MVC 记成 CSV 供事后核对）：

* 连不上（插件没启动）不抛异常、不阻塞采集，只计数并限频重连；
* 发送失败（对端断开）自动重连，不丢采集主循环；
* 记录 ``sent / failed / reconnects``，结束时能说明"到底发出去了多少"。
"""

from __future__ import annotations

import csv
import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

__all__ = ["MvcTcpSender", "MvcCsvRecorder", "format_line", "PROTOCOLS"]

PROTOCOLS = ("json", "pairs", "comma")


def format_line(values: Mapping[str, float], protocol: str = "json") -> str:
    """把 ``{肌肉: %MVC}`` 格式化成一行（不含换行符）。"""
    clean = {str(k): round(float(v), 4) for k, v in values.items()}
    if protocol == "json":
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    if protocol == "pairs":
        return " ".join(f"{k} {v:g}" for k, v in clean.items())
    if protocol == "comma":
        return ",".join(f"{k}={v:g}" for k, v in clean.items())
    raise ValueError(f"Unknown protocol {protocol!r}; options: {PROTOCOLS}")


@dataclass
class _Stats:
    sent: int = 0
    failed: int = 0
    reconnects: int = 0
    skipped_empty: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "reconnects": self.reconnects,
            "skipped_empty": self.skipped_empty,
        }


class MvcTcpSender:
    """把每肌肉 %MVC 按行推给 DPI 插件的 TCP 服务器。

    Parameters
    ----------
    host, port:
        插件 ``config.json → emg.host / emg.port``（默认 ``127.0.0.1:9000``）。
    protocol:
        ``json``（默认，插件的 text 协议支持）／``pairs``／``comma``。
    connect_timeout:
        单次连接尝试的超时。本机被拒绝是立即返回，所以这里很小也行。
    retry_interval:
        连不上时的重连间隔（秒）。避免每次采集循环都去 connect 拖慢主循环。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        protocol: str = "json",
        connect_timeout: float = 1.0,
        send_timeout: float = 1.0,
        retry_interval: float = 2.0,
        keepalive: bool = True,
    ):
        if protocol not in PROTOCOLS:
            raise ValueError(f"Unknown protocol {protocol!r}; options: {PROTOCOLS}")
        self.host = host
        self.port = int(port)
        self.protocol = protocol
        self.connect_timeout = float(connect_timeout)
        self.send_timeout = float(send_timeout)
        self.retry_interval = float(retry_interval)
        self.keepalive = bool(keepalive)

        self._sock: socket.socket | None = None
        self._last_attempt = 0.0
        self.stats = _Stats()
        self.last_error: str | None = None
        self._last_line = ""

    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def describe(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return (f"{self.host}:{self.port} ({self.protocol}, {state}, "
                f"sent {self.stats.sent} rows, failed {self.stats.failed}, "
                f"reconnected {self.stats.reconnects})")

    # ------------------------------------------------------------------ #
    def connect(self, force: bool = False) -> bool:
        """尝试连接；已连接则直接成功。失败返回 False（不抛异常）。"""
        if self._sock is not None:
            return True
        now = time.time()
        if not force and (now - self._last_attempt) < self.retry_interval:
            return False
        self._last_attempt = now
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            sock.settimeout(self.send_timeout)
            if self.keepalive:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except OSError:  # pragma: no cover - 平台相关
                    pass
            self._sock = sock
            self.last_error = None
            return True
        except OSError as exc:
            self._sock = None
            self.last_error = f"Failed to connect to {self.host}:{self.port}: {exc}"
            return False

    def send(self, values: Mapping[str, float]) -> bool:
        """发送一行 %MVC。返回是否成功（失败不影响调用方继续采集）。"""
        if not values:
            self.stats.skipped_empty += 1
            return False
        line = format_line(values, self.protocol)
        if not self.connect():
            self.stats.failed += 1
            return False
        assert self._sock is not None
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
            self.stats.sent += 1
            self._last_line = line
            return True
        except OSError as exc:
            self.last_error = f"Send failed: {exc}"
            self._drop()
            self.stats.failed += 1
            return False

    def _drop(self) -> None:
        """丢弃当前连接，下次 send 会重连。"""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover
                pass
            self._sock = None
            self.stats.reconnects += 1

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "MvcTcpSender":
        self.connect(force=True)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
class MvcCsvRecorder:
    """把每肌肉 %MVC 记成 CSV（``t_lsl, t_wall, ECU, FDS, ...``）。

    * 列名固定为**配置时的肌肉顺序**，即使某次推送缺了某块肌肉，列也不会错位；
    * 同时记 **LSL 时间**与**墙钟（unix 秒）**：DPI 插件的 ``sens_log_*.csv`` 用的是
      ``unix_ms/iso``，有了墙钟列才能把 %MVC、感度、任务日志三方对齐。
    """

    def __init__(
        self,
        path: str | Path,
        muscles: Sequence[str],
        flush_interval: float = 2.0,
        include_wall: bool = True,
    ):
        self.path = Path(path)
        self.muscles = list(muscles)
        self.flush_interval = float(flush_interval)
        self.include_wall = bool(include_wall)
        self.rows = 0
        self._fh = None
        self._writer = None
        self._last_flush = 0.0

    def _header(self) -> list[str]:
        cols = ["t_lsl"]
        if self.include_wall:
            cols.append("t_wall")
        return cols + self.muscles

    def open(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(self._header())

    def write(self, t_lsl: float, values: Mapping[str, float],
              t_wall: float | None = None) -> None:
        if self._fh is None:
            self.open()
        assert self._writer is not None
        row = [f"{float(t_lsl):.6f}"]
        if self.include_wall:
            row.append(f"{float(t_wall if t_wall is not None else time.time()):.6f}")
        for m in self.muscles:
            v = values.get(m)
            row.append("" if v is None else f"{float(v):.4f}")
        self._writer.writerow(row)
        self.rows += 1
        now = time.time()
        if (now - self._last_flush) >= self.flush_interval:
            self._last_flush = now
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:  # pragma: no cover
                pass
            self._fh = None
            self._writer = None

    def __enter__(self) -> "MvcCsvRecorder":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
