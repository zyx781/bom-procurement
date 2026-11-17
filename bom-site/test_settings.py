#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载：settings.json 不进仓库，缺它时退回 settings.example.json。

背景：settings.json 以前是入库的，别人 clone 会继承我的本机调参 ——
调试端口、把拟人停顿压到 0.2 的节奏（比默认更容易撞风控）、单行金额上限。
改成「example 入库 + 本地文件忽略」之后，两条路径必须成立：
  1. 新人 clone 下来没有 settings.json，也要拿到能直接跑的那套默认；
  2. 自己把 settings.json 写坏一个逗号，不能让服务起不来、也不能让金额闸门失效。
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile

# RUNS 在 import 时就定死，先隔离，别让测试碰真实 runs/
_TMP_RUNS = pathlib.Path(tempfile.mkdtemp(prefix="bom-settings-runs-"))
os.environ["TAOBAO_RUNS_DIR"] = str(_TMP_RUNS)

import taobao_runner as tr  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")
        if extra is not None and extra != "":
            print(f"       {extra}")


_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bom-settings-"))
_BAK_FILES = (tr.SETTINGS_FILE, tr.SETTINGS_EXAMPLE_FILE)
_BAK_WARNED = tr._settings_warned


def use(settings="keep", example="keep"):
    """把两个配置路径指到临时目录；None = 该文件不存在，dict = 写入该内容。"""
    f1 = _TMP / "settings.json"
    f2 = _TMP / "settings.example.json"
    for path, val in ((f1, settings), (f2, example)):
        if val == "keep":
            continue
        if val is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")
    tr.SETTINGS_FILE, tr.SETTINGS_EXAMPLE_FILE = f1, f2
    return f1, f2


try:
    print("== 仓库里的 example：新人拿到的是稳的那套 ==")
    ex = json.loads(_BAK_FILES[1].read_text(encoding="utf-8"))
    check("example 是合法 JSON 对象", isinstance(ex, dict), ex)
    check(
        "带 CDP 端口（README 第 1 步「打开调试版 Edge」靠它）",
        ex.get("cdp_url") == "http://127.0.0.1:9222",
        ex.get("cdp_url"),
    )
    check("节奏用默认 0.35，不是本机调激进的 0.2", float(ex.get("speed")) == 0.35, ex.get("speed"))
    check(
        "单价上限与代码兜底逐项一致",
        {k: float(v) for k, v in (ex.get("max_line_cost") or {}).items()}
        == {k: float(v) for k, v in tr._DEFAULT_LINE_CAP.items()},
        ex.get("max_line_cost"),
    )

    print()
    print("== 有 settings.json：听本机的 ==")
    use(settings={"speed": 0.5, "cdp_url": "http://127.0.0.1:9333"}, example={"speed": 0.9})
    s = tr.load_settings()
    check("读到 settings.json 而不是 example", s.get("speed") == 0.5, s)
    check("端口也用本机的", s.get("cdp_url") == "http://127.0.0.1:9333", s)

    print()
    print("== 没有 settings.json：退回 example ==")
    use(settings=None, example={"speed": 0.35, "cdp_url": "http://127.0.0.1:9222"})
    s = tr.load_settings()
    check("从 example 读到节奏", s.get("speed") == 0.35, s)
    check("从 example 读到端口", s.get("cdp_url") == "http://127.0.0.1:9222", s)

    print()
    print("== 两个都没有：返回空 dict，不抛异常 ==")
    use(settings=None, example=None)
    check("空配置不炸", tr.load_settings() == {}, tr.load_settings())

    print()
    print("== settings.json 写坏：回落 example，只警告一次 ==")
    tr._settings_warned = False
    f1, f2 = use(settings=None, example={"speed": 0.35})
    f1.write_text('{ "speed": 0.5,, }', encoding="utf-8")
    s = tr.load_settings()
    check("坏文件不抛异常", isinstance(s, dict), s)
    check("用 example 顶上", s.get("speed") == 0.35, s)
    check("第一次解析失败会置警告位", tr._settings_warned is True, tr._settings_warned)
    s2 = tr.load_settings()
    check("再读仍是 example（不因警告过就返回空）", s2.get("speed") == 0.35, s2)

    print()
    print("== 金额闸门：靠 example 也要拿到同一组上限 ==")
    use(settings=None, example=dict(json.loads(_BAK_FILES[1].read_text(encoding="utf-8"))))
    check("阻容感 200", tr._line_cost_cap("passive") == 200.0, tr._line_cost_cap("passive"))
    check("连接器 300", tr._line_cost_cap("connector") == 300.0, tr._line_cost_cap("connector"))
    check("芯片走 default 5000", tr._line_cost_cap("ic") == 5000.0, tr._line_cost_cap("ic"))
    use(settings=None, example=None)
    check("连 example 都没有也兜住 200", tr._line_cost_cap("passive") == 200.0, tr._line_cost_cap("passive"))
    check("连 example 都没有也兜住 5000", tr._line_cost_cap("ic") == 5000.0, tr._line_cost_cap("ic"))

    print()
    print("== 登录态目录：默认项目内，环境变量能覆盖 ==")
    os.environ.pop("TAOBAO_PROFILE_DIR", None)
    env = tr.session_env()
    check(
        "默认落在项目内 .browser-profile",
        env.get("TAOBAO_PROFILE_DIR") == str(tr.BASE / ".browser-profile"),
        env.get("TAOBAO_PROFILE_DIR"),
    )
    _outer = str(_TMP / "外部profile")
    os.environ["TAOBAO_PROFILE_DIR"] = _outer
    try:
        env2 = tr.session_env()
        check("设了环境变量就用它（以前被硬赋值盖掉）", env2.get("TAOBAO_PROFILE_DIR") == _outer, env2.get("TAOBAO_PROFILE_DIR"))
    finally:
        os.environ.pop("TAOBAO_PROFILE_DIR", None)
    check("不改动父进程环境", "TAOBAO_PROFILE_DIR" not in os.environ, os.environ.get("TAOBAO_PROFILE_DIR"))
    check(
        "UTF-8 三个变量齐全",
        all(env.get(k) for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED")),
        {k: env.get(k) for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED")},
    )
    check("不会把 PATH 之类丢掉", env.get("PATH") == os.environ.get("PATH"), env.get("PATH"))
finally:
    tr.SETTINGS_FILE, tr.SETTINGS_EXAMPLE_FILE = _BAK_FILES
    tr._settings_warned = _BAK_WARNED
    shutil.rmtree(_TMP, ignore_errors=True)
    shutil.rmtree(_TMP_RUNS, ignore_errors=True)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
