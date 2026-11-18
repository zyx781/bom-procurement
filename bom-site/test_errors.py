#!/usr/bin/env python3
"""错误分类回归（errors.py）。

守住的几条：
  - 认得的错误码必须落到正确的大类，别把「验证码」算成「可重试的网络问题」；
  - 未知异常必须算 internal 并且带 traceback（不能静默当成普通失败）；
  - 日志和结果里不许出现 cookie/token 原文；
  - 行级结果的四个字段要能被进度文件和导出表读到。
"""

import io

import errors
import taobao_runner as tr

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


class FakeSessionError(RuntimeError):
    def __init__(self, message, code="SESSION_ERROR", detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


print("== 1. 已知错误码 → 大类 ==")
cases = [
    ("验证码", FakeSessionError("需要人工完成验证", "VERIFICATION_REQUIRED"),
     errors.KIND_AUTH, False, True),
    ("命令超时", FakeSessionError("超时", "COMMAND_TIMEOUT"),
     errors.KIND_NETWORK, True, False),
    ("购物车满", FakeSessionError("购物车满了", "CART_FULL"),
     errors.KIND_INPUT, False, True),
    ("SKU 选不中", FakeSessionError("SKU 没选中", "SKU_NOT_SELECTED"),
     errors.KIND_PAGE, False, True),
    ("坏参数", FakeSessionError("qty 不是整数", "INVALID_QTY"),
     errors.KIND_INPUT, False, False),
]
for label, exc, kind, retryable, human in cases:
    info = errors.classify(exc)
    check(f"{label} → {kind}", info.kind == kind, info)
    check(f"{label} retryable={retryable}", info.retryable is retryable, info)
    check(f"{label} needs_human={human}", info.needs_human is human, info)

print("== 2. 没给码时按异常类型/文本认 ==")
check("TimeoutError → 网络可重试",
      errors.classify(TimeoutError("read timed out")).kind == errors.KIND_NETWORK)
check("ConnectionError → 网络",
      errors.classify(ConnectionError("connection reset")).kind == errors.KIND_NETWORK)
check("ValueError → 数据问题",
      errors.classify(ValueError("未识别到型号列")).kind == errors.KIND_INPUT)
_msg = errors.classify(RuntimeError("Timeout 30000ms exceeded"))
check("文本里的 timeout 也认", _msg.code == "TIMEOUT" and _msg.retryable, _msg)
_sel = errors.classify(RuntimeError("waiting for selector .item failed"))
check("文本里的 selector → 页面问题", _sel.kind == errors.KIND_PAGE, _sel)

print("== 3. 认不出来的一律 internal（不静默） ==")
_unknown = errors.classify(RuntimeError("something odd happened"))
check("未知异常 → internal", _unknown.kind == errors.KIND_INTERNAL, _unknown)
check("未知异常不可重试", _unknown.retryable is False, _unknown)
buf = io.StringIO()


def _log(line):
    buf.write(line + "\n")


errors.log_exception(RuntimeError("boom"), _log, stage="search", line_no=7, batch_id="b1")
text = buf.getvalue()
check("日志带结构化字段", "[error]" in text and "code=UNEXPECTED" in text and "line=7" in text, text)
check("internal 附带 traceback", "[error/tb]" in text, text)

print("== 4. 敏感信息脱敏 ==")
sec = errors.scrub("Cookie: SESSION=abcdef123456; token=deadbeef99")
check("cookie 值被抹掉", "abcdef123456" not in sec and "***" in sec, sec)
check("token 值被抹掉", "deadbeef99" not in sec, sec)

print("== 5. 并进结果 / 读回建议 ==")
res = {"status": "failed", "message": "x"}
errors.enrich_result(res, FakeSessionError("要验证", "VERIFICATION_REQUIRED"),
                     stage="add", line_no=3)
check("结果带错误码", res.get("error_code") == "VERIFICATION_REQUIRED", res)
check("结果带大类", res.get("error_kind") == errors.KIND_AUTH, res)
check("结果带阶段", res.get("error_stage") == "add", res)
check("建议=需要人工处理", errors.hint_for_result(res) == "需要人工处理", res)
_retry = {"error_kind": errors.KIND_NETWORK, "error_retryable": True}
check("可重试的建议", errors.hint_for_result(_retry) == "可重试", _retry)
check("没有分类就没有建议", errors.hint_for_result({}) == "")

print("== 6. 进度 payload / 导出列 带上分类 ==")
payload = tr._result_progress_payload(res)
check("进度 payload 带 error_code", payload.get("error_code") == "VERIFICATION_REQUIRED", payload)
check("进度 payload 带 error_kind", payload.get("error_kind") == errors.KIND_AUTH, payload)
rows = tr.batch_export_rows([{**res, "line_no": 3, "headline": "H"}])
check("导出多出错误码列", rows[0][-2:] == ["错误码", "处理建议"], rows[0])
check("导出写上错误码", rows[1][-2] == "VERIFICATION_REQUIRED", rows[1])
check("导出写上处理建议", rows[1][-1] == "需要人工处理", rows[1])

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
