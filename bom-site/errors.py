#!/usr/bin/env python3
"""错误分类：把「except Exception 一把抓」的结果变成可辨认的三类问题。

为什么需要它
------------
这个项目的失败大多发生在别人家的网页上：超时、元素没渲染出来、账号被要求验证、
购物车满了。以前这些全都落成一行 `status=failed` + 一句中文，用户看到的只有
「失败」，没法判断该**重试**、该**去点一下验证码**，还是该**报 bug**。

现在所有行级失败都带四个字段（随结果落盘、进导出 CSV、进前端）：

    error_code       稳定的短码，如 COMMAND_TIMEOUT / VERIFICATION_REQUIRED
    error_kind       input / network / page / auth / internal
    error_retryable  原样再跑一次有没有意义
    error_needs_human 是否必须人工介入（登录、验证码、页面改版）

分类原则是「宁可保守」：认不出来的一律算 internal —— 它会保留 traceback，
让程序缺陷暴露出来，而不是伪装成网页波动被重试掩盖。
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass

# ── 错误大类 ────────────────────────────────────────────────
KIND_INPUT = "input"        # BOM/参数不对，重试无意义，要改数据
KIND_NETWORK = "network"    # 超时/连接断了，等一会儿重试有意义
KIND_PAGE = "page"          # 页面结构/元素变了，通常要人看一眼或改选择器
KIND_AUTH = "auth"          # 登录失效/验证码，必须人工
KIND_INTERNAL = "internal"  # 程序缺陷，必须留 traceback 给人查

KIND_LABELS = {
    KIND_INPUT: "数据问题",
    KIND_NETWORK: "网络问题",
    KIND_PAGE: "页面问题",
    KIND_AUTH: "需要人工验证",
    KIND_INTERNAL: "程序异常",
}


@dataclass(frozen=True)
class ErrorInfo:
    """一次失败的分类结论。"""

    code: str
    kind: str
    retryable: bool
    needs_human: bool
    message: str

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def hint(self) -> str:
        """给用户看的一句话处理建议（不含错误码，前端另行展示）。"""
        if self.needs_human:
            return "需要人工处理"
        if self.retryable:
            return "可重试"
        if self.kind == KIND_INTERNAL:
            return "程序异常，请保留日志反馈"
        return ""

    def as_result_fields(self) -> dict:
        """并进行级结果/进度文件的那几个字段。"""
        return {
            "error_code": self.code,
            "error_kind": self.kind,
            "error_retryable": self.retryable,
            "error_needs_human": self.needs_human,
        }

    def as_dict(self) -> dict:
        out = {
            "code": self.code,
            "kind": self.kind,
            "label": self.label,
            "retryable": self.retryable,
            "needs_human": self.needs_human,
            "hint": self.hint,
            "message": self.message,
        }
        return out


# 已知错误码 → (大类, 可否重试, 是否要人工)
# 码表来自执行层（skills/taobao-search）与 runner 自己抛的 SessionError。
_KNOWN_CODES: dict[str, tuple[str, bool, bool]] = {
    # 会话/进程
    "SESSION_START_FAILED": (KIND_INTERNAL, True, False),
    "SESSION_WRITE_FAILED": (KIND_INTERNAL, True, False),
    "SESSION_DESYNCED": (KIND_INTERNAL, False, True),
    "SESSION_NOT_STARTED": (KIND_INPUT, False, False),
    "CDP_LAUNCH_FAILED": (KIND_INTERNAL, True, False),
    "BROWSER_GONE": (KIND_PAGE, False, True),
    # 网络/超时
    "COMMAND_TIMEOUT": (KIND_NETWORK, True, False),
    "SEARCH_NO_RESPONSE": (KIND_NETWORK, True, False),
    # 验证/登录
    "VERIFICATION_REQUIRED": (KIND_AUTH, False, True),
    "LOGIN_REQUIRED": (KIND_AUTH, False, True),
    # 输入数据
    "NO_QUERY": (KIND_INPUT, False, False),
    "INVALID_SELECTIONS": (KIND_INPUT, False, False),
    "INVALID_QTY": (KIND_INPUT, False, False),
    "LINE_SKIPPED": (KIND_INPUT, False, False),
    "NO_LINE": (KIND_INPUT, False, False),
    "NO_LINES": (KIND_INPUT, False, False),
    "BATCH_EMPTY": (KIND_INPUT, False, False),
    # 任务状态冲突（等一等再点就行）
    "BATCH_RUNNING": (KIND_INPUT, True, False),
    # 页面/SKU/购物车
    "OPEN_FAILED": (KIND_PAGE, True, False),
    "SEARCH_FAILED": (KIND_PAGE, True, False),
    "SKU_NOT_SELECTED": (KIND_PAGE, False, True),
    "SKU_CONFIRM_FAILED": (KIND_PAGE, False, True),
    "SKU_REPLAY_FAILED": (KIND_PAGE, False, True),
    "QTY_SET_FAILED": (KIND_PAGE, False, True),
    "QTY_CONFIRM_FAILED": (KIND_PAGE, False, True),
    "CART_CLICK_BLOCKED": (KIND_PAGE, True, False),
    "CART_ADD_REJECTED": (KIND_PAGE, True, False),
    "CART_UNCONFIRMED": (KIND_PAGE, True, False),
    "CART_FULL": (KIND_INPUT, False, True),
    "CART_ROWS_NOT_RENDERED": (KIND_PAGE, True, False),
    "CART_NO_MATCH": (KIND_PAGE, False, True),
    "CART_REMOVE_AMBIGUOUS": (KIND_INPUT, False, False),
}

# 没给错误码时，从异常文本里认领的线索（顺序敏感：先具体后笼统）
_TEXT_RULES: list[tuple[str, str, tuple[str, bool, bool]]] = [
    ("verification", "VERIFICATION_REQUIRED", (KIND_AUTH, False, True)),
    ("captcha", "VERIFICATION_REQUIRED", (KIND_AUTH, False, True)),
    ("安全验证", "VERIFICATION_REQUIRED", (KIND_AUTH, False, True)),
    ("login", "LOGIN_REQUIRED", (KIND_AUTH, False, True)),
    ("登录", "LOGIN_REQUIRED", (KIND_AUTH, False, True)),
    ("cart is full", "CART_FULL", (KIND_INPUT, False, True)),
    ("购物车已满", "CART_FULL", (KIND_INPUT, False, True)),
    ("net::err", "NETWORK_ERROR", (KIND_NETWORK, True, False)),
    ("connection", "NETWORK_ERROR", (KIND_NETWORK, True, False)),
    ("timed out", "TIMEOUT", (KIND_NETWORK, True, False)),
    ("timeout", "TIMEOUT", (KIND_NETWORK, True, False)),
    ("超时", "TIMEOUT", (KIND_NETWORK, True, False)),
    ("selector", "SELECTOR_MISS", (KIND_PAGE, True, False)),
    ("等待元素", "SELECTOR_MISS", (KIND_PAGE, True, False)),
    ("strict mode violation", "SELECTOR_MISS", (KIND_PAGE, True, False)),
]

# 日志里不该出现的东西（即使异常文本里带了，也先抹掉）
_SECRET_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|x5secdata|token|password|passwd)\b"
    r"(\s*[:=]\s*)([^\s,;]{4,})"
)


def scrub(text: object, limit: int = 400) -> str:
    """抹掉敏感键值并截断 —— 日志和错误信息会进文件、进导出表。"""
    s = str(text or "")
    s = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", s)
    return s[:limit]


def classify(exc: BaseException) -> ErrorInfo:
    """把异常归到五类之一。永远有返回值，自身不抛异常。"""
    message = scrub(exc)

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        kind, retryable, needs_human = _KNOWN_CODES.get(
            code, (KIND_PAGE, False, False)
        )
        return ErrorInfo(str(code), kind, retryable, needs_human, message)

    if isinstance(exc, TimeoutError):
        return ErrorInfo("TIMEOUT", KIND_NETWORK, True, False, message)
    if isinstance(exc, ConnectionError):
        return ErrorInfo("NETWORK_ERROR", KIND_NETWORK, True, False, message)
    if isinstance(exc, (ValueError, KeyError)):
        # BOM 解析、参数校验都走 ValueError；这类重试一万次也不会变好。
        return ErrorInfo("BAD_INPUT", KIND_INPUT, False, False, message)

    lower = message.lower()
    for needle, hit_code, (kind, retryable, needs_human) in _TEXT_RULES:
        if needle in lower or needle in message:
            return ErrorInfo(hit_code, kind, retryable, needs_human, message)

    return ErrorInfo("UNEXPECTED", KIND_INTERNAL, False, False, message)


def hint_for_result(result: dict) -> str:
    """从行级结果里读分类字段，给出一句处理建议（导出表/界面的「怎么处理」列）。"""
    if not isinstance(result, dict):
        return ""
    if result.get("error_needs_human"):
        return "需要人工处理"
    if result.get("error_retryable"):
        return "可重试"
    kind = str(result.get("error_kind") or "")
    if kind == KIND_INTERNAL:
        return "程序异常，请保留日志反馈"
    if kind:
        return KIND_LABELS.get(kind, "")
    return ""


def enrich_result(result: dict, exc: BaseException, *,
                  stage: str = "", line_no: int | str | None = None) -> dict:
    """把分类结果并进行级结果，返回同一个 dict（方便链式用）。

    `stage` 只进日志，不覆盖结果里的 message —— 用户看到的还是原始原因，
    但同一行日志里能定位到「搜索 / 开页 / 选 SKU / 加购」哪一步。
    """
    info = classify(exc)
    result.update(info.as_result_fields())
    result["error_stage"] = stage or result.get("error_stage") or ""
    if line_no is not None:
        result["line_no"] = line_no
    return result


def log_exception(exc: BaseException, log, *,
                  stage: str = "", line_no: int | str | None = None,
                  batch_id: str | None = None) -> ErrorInfo:
    """打一条结构化错误日志；internal 类额外附 traceback。

    `log` 是 runner 的 `_append_log` / 模块级 `_log`。日志格式刻意保持一行：
    批量跑时读日志的人靠 `grep '\\[error\\]'` 就能捞出所有失败及其分类。
    """
    info = classify(exc)
    parts = [
        "[error]",
        f"code={info.code}",
        f"kind={info.kind}",
    ]
    if batch_id:
        parts.append(f"batch={batch_id}")
    if line_no is not None:
        parts.append(f"line={line_no}")
    if stage:
        parts.append(f"stage={stage}")
    parts.append(f"retryable={int(info.retryable)}")
    parts.append(f"human={int(info.needs_human)}")
    parts.append(f"msg={info.message!r}")
    log(" ".join(parts))
    if info.kind == KIND_INTERNAL:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # 只留最后 15 层，避免一个深递归把日志刷爆
        lines = tb.strip().splitlines()[-15:]
        for ln in lines:
            log(f"[error/tb] {scrub(ln, 300)}")
    return info
