# -*- coding: utf-8 -*-
"""编码体检：找出 BOM、非法 UTF-8、以及疑似"被 GBK 误转码"的文件。

用法：python check_encoding.py <目录> [<目录> ...]
"""
import io
import os
import sys

TEXT_EXT = {".py", ".md", ".json", ".bat", ".txt", ".csv", ".ps1", ".cfg", ".ini"}
# 从 UTF-8 被当 GBK 解出来的典型乱码字符。
# 用 \u 转义写，避免本文件自己命中这些标记（否则会误报自己）。
MOJIBAKE_MARKERS = tuple(
    chr(int(code, 16)) for code in ("951b", "9428", "9225", "8def", "93bb",
                                    "935a", "dec4", "6d93", "65b3", "7f02")
)


def look(path: str) -> list[str]:
    problems = []
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("有 UTF-8 BOM（.py/.bat 会出问题）")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append(f"不是合法 UTF-8：{exc}")
        return problems
    hits = [m for m in MOJIBAKE_MARKERS if m in text]
    if len(hits) >= 3:
        problems.append(f"疑似被 GBK 误转码（命中乱码字符 {''.join(hits)}）")
    return problems


def main(dirs: list[str]) -> int:
    bad = 0
    checked = 0
    for root_dir in dirs:
        for root, dirs_, files in os.walk(root_dir):
            dirs_[:] = [d for d in dirs_ if d not in
                        ("__pycache__", ".git", "node_modules", "logs")]
            for name in files:
                if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                    continue
                path = os.path.join(root, name)
                checked += 1
                problems = look(path)
                if problems:
                    bad += 1
                    print(f"  [问题] {path}")
                    for p in problems:
                        print(f"         - {p}")
    print(f"\n检查 {checked} 个文本文件，{bad} 个有问题。")
    return 1 if bad else 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["."]
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(args))
