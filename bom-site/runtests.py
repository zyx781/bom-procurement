"""统一测试入口：一条命令跑完 bom-site 下的全部 test_*.py。

为什么需要它（而不是每个文件单独 `python test_xxx.py`）：

1. **终端编码**。测试会打印 Ω / µ / ± 这类字符。Windows 默认控制台是 GBK，
   `print` 直接抛 UnicodeEncodeError，看起来像测试挂了，其实一条断言都没跑。
   这里把子进程的 stdout/stderr 固定成 UTF-8（PYTHONIOENCODING + PYTHONUTF8），
   并在本进程里 reconfigure，所以 PowerShell、cmd、Git Bash 结果一致。
2. **汇总**。二十多个脚本逐个敲太麻烦，也不容易看出到底哪套挂了；这里一次跑完，
   失败时打印该脚本的末尾输出，最后给一张表。

用法（在 bom-site 目录下）：

    python runtests.py                # 跑全部（跳过需要外部服务的 test_http.py）
    python runtests.py test_engine    # 只跑名字匹配的（可用多个子串）
    python runtests.py --include-http # 连 test_http.py 一起跑（需 8765 已启动）
    python runtests.py --list         # 只列出会被跑的脚本
    python runtests.py --fail-fast    # 第一个失败就停

退出码：全部通过 0，否则 1。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 需要外部服务（8765 已在跑）的脚本，默认不跑；README 里也说明它不进 CI。
NEEDS_SERVER = {"test_http.py"}
_RESULT_RE = re.compile(r"RESULT:\s*(\d+)\s+passed")


def _force_utf8() -> dict[str, str]:
    """把本进程与子进程的输出都钉成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 老 Python / 被重定向的流，忽略
            pass
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def discover() -> list[Path]:
    return sorted(p for p in HERE.glob("test_*.py") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑 bom-site 的全部测试脚本")
    parser.add_argument("filters", nargs="*", help="只跑路径里包含这些子串的脚本")
    parser.add_argument("--include-http", action="store_true",
                        help="连 test_http.py 一起跑（需要本地服务已启动）")
    parser.add_argument("--list", action="store_true", help="只列出会跑的脚本")
    parser.add_argument("--fail-fast", action="store_true", help="第一个失败就停")
    parser.add_argument("-v", "--verbose", action="store_true", help="透传每个脚本的输出")
    args = parser.parse_args(argv)

    env = _force_utf8()
    scripts = discover()
    if not args.include_http:
        scripts = [p for p in scripts if p.name not in NEEDS_SERVER]
    if args.filters:
        scripts = [p for p in scripts if any(f in p.name for f in args.filters)]

    if args.list:
        for p in scripts:
            print(p.name)
        return 0
    if not scripts:
        print("没有匹配的测试脚本", file=sys.stderr)
        return 1

    print(f"Python {sys.version.split()[0]} · {len(scripts)} 个脚本 · UTF-8 输出")
    results: list[tuple[str, bool, float, str]] = []
    started = time.monotonic()

    for path in scripts:
        label = path.name
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(HERE),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.monotonic() - t0
        ok = proc.returncode == 0
        out = proc.stdout or ""
        results.append((label, ok, elapsed, out))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label:<42} {elapsed:6.1f}s")
        if args.verbose or not ok:
            tail = out.strip().splitlines()
            if not args.verbose:
                tail = tail[-25:]
            for line in tail:
                print(f"        | {line}")
        if not ok and args.fail_fast:
            break

    total = time.monotonic() - started
    failed = [r for r in results if not r[1]]
    # 各脚本自己打印 `RESULT: N passed, M failed`；汇总一下，README 里的数字才不是手写的。
    assertions = 0
    for _label, _ok, _elapsed, out in results:
        m = _RESULT_RE.search(out or "")
        if m:
            assertions += int(m.group(1))
    print(
        f"\n{len(results) - len(failed)}/{len(results)} 套通过，"
        f"共 {assertions} 项断言，总耗时 {total:.1f}s"
    )
    if failed:
        print("失败的脚本：" + "、".join(r[0] for r in failed), file=sys.stderr)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
