# -*- coding: utf-8 -*-
"""
rawaccel_bridge.py — Raw Accel 驱动桥
======================================
把“sens 乘数”翻译成不同版本 Raw Accel 的写入动作：

1) RawAccelWriterV17（默认，Raw Accel 1.7.0+）
   - 接口：安装目录里的 writer.exe <settings.json 路径>
   - 新格式无 sens 字段，灵敏度是 profile 里的 "Output DPI"，
     即 effective_dpi = base_dpi × sens 直接写入该字段；
   - 写入时会强制所有 accel 的 mode 为 noaccel（实验要求加速关闭）；
   - 驱动有防作弊的 1 秒变更延迟（每次写入约 1s）。

2) RawAccelCLI（兼容 Raw Accel 1.6.x）
   - 接口：rawaccel.exe --write "<json>"，JSON 里有 sens 字段。

3) MockBackend —— 无硬件/演示用，只记录不写驱动。

没有安装 Raw Accel 或未加载驱动时自动退回模拟后端并提示。
"""

import copy
import glob
import json
import os
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class RawAccelError(Exception):
    pass


def _deep_set(obj, key, value):
    """在嵌套 JSON 中把所有同名 key 设为 value（兼容不同版本的字段层级）。"""
    if isinstance(obj, dict):
        if key in obj:
            obj[key] = value
        for v in obj.values():
            _deep_set(v, key, value)
    elif isinstance(obj, list):
        for v in obj:
            _deep_set(v, key, value)


def _find_value(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_value(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_value(v, key)
            if r is not None:
                return r
    return None


def _startupinfo_hidden():
    """Windows 下隐藏 subprocess 控制台窗口。"""
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si
    return None


def build_v17_payload(template, output_dpi):
    """基于 v1.7 模板生成写入载荷：Output DPI=目标值，所有 mode=noaccel。"""
    payload = copy.deepcopy(template)
    payload.pop("_comment", None)
    _deep_set(payload, "Output DPI", round(float(output_dpi), 1))
    _deep_set(payload, "mode", "noaccel")
    return payload


def sens_to_output_dpi(sens, norm_dpi=1000.0):
    """Raw Accel 1.7：Sens Multiplier = Output DPI ÷ 1000（本版本固定 1000 归一化）。

    要 Raw Accel 显示 sens=1.0/1.5 → 写 Output DPI = 1000×sens = 1000/1500。
    配合鼠标硬件 DPI=800：有效 DPI = 800 × sens = 800/1200。
    """
    return round(float(norm_dpi) * float(sens), 1)


# ======================================================================
# 1) Raw Accel 1.7.0：writer.exe + settings.json
# ======================================================================
class RawAccelWriterV17:
    kind = "rawaccel_writer_v17"

    INSTALL_DIR_CANDIDATES = [
        os.path.expandvars(r"%USERPROFILE%\Desktop\RawAccel"),
        os.path.expandvars(r"%USERPROFILE%\Desktop\rawaccel"),
        r"C:\Program Files\Raw Accel",
        r"C:\Program Files (x86)\Raw Accel",
        os.path.expandvars(r"%LOCALAPPDATA%\Raw Accel"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Raw Accel"),
    ]

    READBACK_EXE = os.path.join(BASE_DIR, "tools", "driver_readback.exe")

    def __init__(self, install_dir, template_file="rawaccel_template_v17.json",
                 base_dpi=800.0, write_timeout=20, verify=False):
        self.install_dir = os.path.abspath(install_dir)
        self.writer = os.path.join(self.install_dir, "writer.exe")
        if not os.path.isfile(self.writer):
            raise RawAccelError(f"{self.install_dir} 中没有 writer.exe")
        self.template = self._load_template(template_file)
        self.base_dpi = float(base_dpi)
        self.write_timeout = write_timeout
        self.verify = verify
        self.last_applied = None
        self.settings_path = os.path.join(self.install_dir, "settings.json")
        self._write_lock = threading.Lock()
        self._last_write_ts = 0.0

    def _load_template(self, template_file):
        path = template_file
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def apply(self, sens, accel_mode="off", extra=None):
        """把 sens 乘数写成 Output DPI（= sens × 1000 → 1000/1500，
        Raw Accel GUI 显示 Sens Multiplier 1.0/1.5），原地更新安装目录的
        settings.json，再通过 writer.exe 写入驱动。

        驱动有防作弊机制：连续写入需间隔 ≥1s，否则 writer 返回 rc=1。
        这里强制最小间隔并失败重试。"""
        output_dpi = sens_to_output_dpi(sens)
        # 以现有 settings.json 为基础（保留 devices/其他设置），缺失或损坏时用模板
        base = {}
        if os.path.isfile(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    base = json.load(f)
            except Exception:
                base = {}
        if (not isinstance(base.get("profiles"), list)
                or not base.get("profiles")):
            base = copy.deepcopy(self.template)
        payload = build_v17_payload(base, output_dpi)
        if extra:
            payload.update(extra)
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        # 序列化写入 + 最小间隔 + 失败重试（防作弊窗口）
        with self._write_lock:
            since = time.time() - self._last_write_ts
            if since < 1.1:
                time.sleep(1.1 - since)
            proc = None
            for attempt in range(3):
                try:
                    proc = subprocess.run(
                        [self.writer, self.settings_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=self.write_timeout, cwd=self.install_dir,
                        startupinfo=_startupinfo_hidden())
                except FileNotFoundError:
                    raise RawAccelError(f"无法执行 {self.writer}")
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill() if proc is not None else None
                    except Exception:
                        pass
                    raise RawAccelError("writer.exe 写入超时（驱动是否已加载？）")
                self._last_write_ts = time.time()
                if proc.returncode == 0:
                    break
                if attempt < 2:
                    time.sleep(1.2)   # 等防作弊窗口过去再重试
            if proc is None or proc.returncode != 0:
                raise RawAccelError(
                    f"writer.exe 写入失败 (rc={proc.returncode if proc else '?'})")
        self.last_applied = float(sens)
        if self.verify:
            self._verify(output_dpi)
        return payload

    def _verify(self, expect_output_dpi):
        got = self.read_output_dpi()
        if got is not None and abs(float(got) - float(expect_output_dpi)) > 1.0:
            sys.stderr.write(
                f"[警告] 驱动回读 Output DPI={got}，与期望 {expect_output_dpi} 不一致\n")

    def read_output_dpi(self):
        """通过 DriverConfig.GetActive() 读回驱动当前 Output DPI；失败返回 None。"""
        try:
            cfg = read_driver_config(self.install_dir)
        except RawAccelError as e:
            sys.stderr.write(f"[警告] 驱动回读失败: {e}\n")
            return None
        v = _find_value(cfg, "Output DPI")
        return float(v) if v is not None else None

    def read(self):
        try:
            return read_driver_config(self.install_dir)
        except RawAccelError as e:
            raise RawAccelError(f"驱动回读失败: {e}")

    def reset(self):
        return self.apply(1.0, "off")


def read_driver_config(install_dir):
    """调用 tools/driver_readback.exe 读取驱动当前活动配置（返回 JSON dict）。"""
    tools_dir = os.path.join(BASE_DIR, "tools")
    exe = os.path.join(tools_dir, "driver_readback.exe")
    if not os.path.isfile(exe):
        raise RawAccelError(f"缺少 {exe}（用 tools/driver_readback.cs 重新编译）")
    # 依赖程序集与 exe 同目录（每次同步，保证与安装版本一致）
    import shutil
    for dll in ("wrapper.dll", "Newtonsoft.Json.dll"):
        src = os.path.join(install_dir, dll)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(tools_dir, dll))
        elif not os.path.isfile(os.path.join(tools_dir, dll)):
            raise RawAccelError(f"缺少 {dll}")
    try:
        proc = subprocess.run(
            [exe], capture_output=True, text=True, timeout=20,
            startupinfo=_startupinfo_hidden(), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise RawAccelError("driver_readback 超时")
    if proc.returncode != 0:
        raise RawAccelError(
            f"driver_readback 失败 (rc={proc.returncode}): "
            f"{(proc.stdout or '') + (proc.stderr or '')}"[:300])
    try:
        return json.loads(proc.stdout)
    except Exception:
        raise RawAccelError(f"driver_readback 输出无法解析: {proc.stdout[:200]}")


def find_v17_install_dir(configured=""):
    """查找含 writer.exe 的 Raw Accel 1.7 安装目录。"""
    if configured and os.path.isfile(os.path.join(configured, "writer.exe")):
        return os.path.abspath(configured)
    candidates = list(RawAccelWriterV17.INSTALL_DIR_CANDIDATES)
    # 桌面下一级目录扫描
    try:
        desktop = os.path.expandvars(r"%USERPROFILE%\Desktop")
        for d in glob.glob(os.path.join(desktop, "*")):
            candidates.append(d)
    except Exception:
        pass
    seen = set()
    for d in candidates:
        d = os.path.abspath(d)
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        if os.path.isfile(os.path.join(d, "writer.exe")):
            return d
    return None


# ======================================================================
# 2) Raw Accel 1.6.x：rawaccel.exe --write
# ======================================================================
class RawAccelCLI:
    """通过 rawaccel.exe CLI 写驱动（v1.6.x 风格，sens 字段）。"""

    kind = "rawaccel_cli_v16"

    COMMON_PATHS = [
        r"C:\Program Files\Raw Accel\rawaccel.exe",
        r"C:\Program Files (x86)\Raw Accel\rawaccel.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Raw Accel\rawaccel.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Raw Accel\rawaccel.exe"),
    ]

    def __init__(self, exe_path="", template_file="rawaccel_template.json",
                 verify=False, write_flag="--write", print_flag="--print"):
        self.write_flag = write_flag
        self.print_flag = print_flag
        self.exe = self._find_exe(exe_path)
        self.template = self._load_template(template_file)
        self.verify = verify
        self.last_applied = None

    def _find_exe(self, exe_path):
        if exe_path and os.path.isfile(exe_path):
            return exe_path
        import shutil
        for name in ("rawaccel.exe", "rawaccel"):
            hit = shutil.which(name)
            if hit:
                return hit
        for p in self.COMMON_PATHS:
            if os.path.isfile(p):
                return p
        raise RawAccelError(
            "未找到 rawaccel.exe（v1.6 版）。请在 config.json 的 "
            "rawaccel.exe_path 中填写完整路径。")

    def _load_template(self, template_file):
        path = template_file
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def apply(self, sens, accel_mode="off", extra=None):
        payload = copy.deepcopy(self.template)
        payload.pop("_comment", None)
        _deep_set(payload, "sens", round(float(sens), 4))
        if "accel_mode" in payload:
            payload["accel_mode"] = accel_mode
        if extra:
            payload.update(extra)
        json_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        cmd = [self.exe, self.write_flag, json_str]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                startupinfo=_startupinfo_hidden(),
                encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise RawAccelError(f"无法执行 {self.exe}，请检查 exe_path。")
        except subprocess.TimeoutExpired:
            raise RawAccelError("rawaccel --write 超时。")
        if proc.returncode != 0:
            out = (proc.stdout or "") + (proc.stderr or "")
            raise RawAccelError(
                f"rawaccel --write 失败 (rc={proc.returncode}): {out.strip()[:300]}")
        self.last_applied = float(sens)
        if self.verify:
            self._verify(sens)
        return payload

    def _verify(self, sens):
        try:
            cur = self.read()
        except RawAccelError:
            return
        got = _find_value(cur, "sens")
        if got is not None and abs(float(got) - float(sens)) > 0.005:
            sys.stderr.write(f"[警告] 驱动回读 sens={got}，与期望 {sens} 不一致\n")

    def read(self):
        cmd = [self.exe, self.print_flag]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                startupinfo=_startupinfo_hidden(),
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise RawAccelError("rawaccel --print 超时。")
        if proc.returncode != 0:
            raise RawAccelError(
                f"rawaccel --print 失败 (rc={proc.returncode}): "
                f"{(proc.stdout or '') + (proc.stderr or '')}"[:300])
        try:
            return json.loads(proc.stdout)
        except Exception:
            return {"raw": proc.stdout}

    def reset(self):
        return self.apply(1.0, "off")


# ======================================================================
# 3) 模拟后端
# ======================================================================
class MockBackend:
    """模拟后端：不真正写驱动，只记录最后一次写入。用于 dry-run / 演示。"""

    kind = "mock"

    def __init__(self):
        self.last_applied = None
        self.write_count = 0

    def apply(self, sens, accel_mode="off", extra=None):
        self.last_applied = round(float(sens), 4)
        self.write_count += 1
        return {"mock": True, "sens": self.last_applied, "accel_mode": accel_mode}

    def read(self):
        return {"mock": True, "sens": self.last_applied}

    def reset(self):
        return self.apply(1.0, "off")


# ======================================================================
# 工厂
# ======================================================================
def make_bridge(cfg, quiet=False):
    """根据配置创建驱动桥；找不到 Raw Accel 时退回模拟后端并给出提示。

    接口选择（rawaccel.interface）：auto（默认，自动探测）/ writer_v17 /
    cli_v16 / mock。
    """
    ra = cfg.get("rawaccel", {})
    interface = ra.get("interface", "auto")
    base_dpi = float(cfg.get("mouse", {}).get("base_dpi", 800))
    dry_run = bool(ra.get("dry_run", False))

    if dry_run or interface == "mock":
        if not quiet:
            print("[Simulation] No real driver writes; logging only.")
        return MockBackend()

    # ---- 优先 v1.7 writer.exe ----
    if interface in ("auto", "writer_v17"):
        install_dir = find_v17_install_dir(ra.get("install_dir", ""))
        if install_dir:
            try:
                return RawAccelWriterV17(
                    install_dir=install_dir,
                    template_file=ra.get("template_file_v17",
                                         "rawaccel_template_v17.json"),
                    base_dpi=base_dpi,
                    verify=bool(ra.get("verify_with_print", False)))
            except RawAccelError as e:
                if not quiet:
                    print(f"[警告] {e}")
                    print("[Warning] Falling back to the simulation backend: the program keeps running/logging, but will not actually change mouse sensitivity.")
                return MockBackend()
        if interface == "writer_v17":
            if not quiet:
                print("[Warning] No Raw Accel 1.7 install dir with writer.exe found; "
                      "set rawaccel.install_dir in config.json.")
                print("[Warning] Falling back to the simulation backend.")
            return MockBackend()

    # ---- v1.6 CLI ----
    if interface in ("auto", "cli_v16"):
        try:
            return RawAccelCLI(
                exe_path=ra.get("exe_path", ""),
                template_file=ra.get("template_file", "rawaccel_template.json"),
                verify=bool(ra.get("verify_with_print", False)))
        except RawAccelError as e:
            if not quiet:
                print(f"[警告] {e}")
                print("[警告] 未检测到可用的 Raw Accel（1.7 的 writer.exe 或 1.6 的 "
                      "rawaccel.exe）。已退回模拟后端：程序照常运行/记录，"
                      "但不会真正改变鼠标灵敏度。")
            return MockBackend()

    if not quiet:
        print(f"[Warning] Unknown interface={interface}; using the simulation backend.")
    return MockBackend()


def dump_current_settings(install_dir="", exe_path="",
                          out_file="rawaccel_template_current.json"):
    """抓取本机 Raw Accel 设置生成模板（v1.7 复制 settings.json，v1.6 用 --print）。"""
    d = find_v17_install_dir(install_dir)
    if d:
        src = os.path.join(d, "settings.json")
        if os.path.isfile(src):
            with open(src, "r", encoding="utf-8") as f:
                cur = json.load(f)
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            print(f"[OK] Copied Raw Accel 1.7 settings to {out_file}。\n"
                  f"  NOTE: for the actual experiment set Output DPI to 800 (baseline) in the template, \n"
                  f"  then point rawaccel.template_file_v17 in config.json to that file.")
            return 0
        print("[Error] No settings.json in the install dir. Open the Raw Accel GUI once to save settings.")
        return 1
    try:
        cli = RawAccelCLI(exe_path=exe_path)
    except RawAccelError as e:
        print(f"[失败] {e}")
        return 1
    try:
        cur = cli.read()
    except RawAccelError as e:
        print(f"[失败] {e}")
        return 1
    if "raw" in cur:
        print(f"[Note] --print output is not valid JSON; raw output:\n{cur['raw'][:500]}")
        return 1
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    print(f"[OK] Wrote current driver settings to {out_file}; its content can replace rawaccel_template.json.")
    return 0
