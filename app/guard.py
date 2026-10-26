"""Deterministic prompt-injection guard for customer-sourced and model-proposed text.

Two-tier heuristic net:

* ``BLOCK`` rules hard-intercept instruction override, role hijack,
  system-prompt leakage, direct sensitive-tool invocation and forged JSON tool
  calls. ``detect_injection`` returns only a BLOCK label, preserving the
  hard-block semantics used by the inbound chain.
* ``WARN`` rules flag data exfiltration and encoding obfuscation for logging or
  escalation without blocking a benign message; ``analyze_injection`` exposes
  them.

This is a heuristic net, not a proof. The gateway's policy/approval layer
remains the second, enforced layer behind it.
"""
from __future__ import annotations

import re

BLOCK = "BLOCK"
WARN = "WARN"

_BLOCK_RULES: list[tuple[str, re.Pattern]] = [
    (
        "instruction-override",
        re.compile(
            r"(忽略|无视|忘掉|跳过|别管|取消|解除)\s*"
            r"(以上|之前|此前|所有|任何|下面|上面|系统|上下文|指令|提示|规则|约束|要求|设定|权限|审批|安全|限制)"
        ),
    ),
    (
        "english-override",
        re.compile(
            r"\b(ignore|disregard|forget|bypass|override)\b.{0,40}\b"
            r"(instructions?|prompts?|rules?|system|security|approval|policy)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-hijack",
        re.compile(
            r"(你现在是|你不再是|你现在扮演|角色扮演|扮演一个|切换身份|"
            r"developer\s*mode|jailbreak|越狱|\bDAN\b|"
            r"act\s+as\s+an?\s+(ai|assistant|admin|developer))",
            re.IGNORECASE,
        ),
    ),
    (
        "system-prompt-leak",
        re.compile(r"(系统提示|系统提示词|开发者指令|隐藏规则|system\s*prompt|developer\s*message)", re.IGNORECASE),
    ),
    (
        "tool-invoke",
        re.compile(
            r"(调用|执行|运行|触发|直接|立即)\s*"
            r"(工具|make_payment|make_refund|create_order|cancel_order|"
            r"fulfill_order|bulk_update_prices|send_chat_message|reply_customer)"
            r"|(立即转账|直接退款|绕过审批|跳过审批|不用审批|把所有商品价格改成|把价格改成0)"
        ),
    ),
]

# Forged tool-call JSON only makes sense as a customer-message signal; a model
# plan is itself JSON, so this rule must not run when scanning plan structures.
_JSON_TOOL_CALL = ("json-tool-call", re.compile(r"\{\s*\"(tool|action)\"\s*:"))

_WARN_RULES: list[tuple[str, re.Pattern]] = [
    (
        "data-exfiltration",
        re.compile(
            r"(把|将)\s*(所有|全部|客户|订单|密钥|密码|token|个人信息|数据)"
            r".{0,20}(发送|转发|复制|上传|导出|贴到).{0,20}(到|给|至)"
        ),
    ),
    (
        "encoding-obfuscation",
        re.compile(r"(base64|=\?[A-Za-z-]+\?[BQ]\?|[A-Za-z0-9+/]{40,}={0,2})", re.IGNORECASE),
    ),
]


def analyze_injection(text: str, *, check_json_call: bool = True) -> dict:
    """Classify text into BLOCK / WARN / clean with every matched rule.

    ``check_json_call`` disables the forged-JSON rule when scanning a model plan
    structure (which is itself JSON) so legitimate ``{"tool": ...}`` plans pass.
    """
    if not text:
        return {"blocked": False, "severity": None, "label": None, "reason": None, "matched": []}

    block_matches = []
    for label, pattern in _BLOCK_RULES:
        m = pattern.search(text)
        if m:
            block_matches.append({"level": BLOCK, "label": label, "match": m.group(0)})
    if check_json_call:
        label, pattern = _JSON_TOOL_CALL
        m = pattern.search(text)
        if m:
            block_matches.append({"level": BLOCK, "label": label, "match": m.group(0)})
    if block_matches:
        top = block_matches[0]
        return {
            "blocked": True,
            "severity": BLOCK,
            "label": top["label"],
            "reason": top["match"],
            "matched": block_matches,
        }

    warn_matches = []
    for label, pattern in _WARN_RULES:
        m = pattern.search(text)
        if m:
            warn_matches.append({"level": WARN, "label": label, "match": m.group(0)})
    if warn_matches:
        top = warn_matches[0]
        return {
            "blocked": False,
            "severity": WARN,
            "label": top["label"],
            "reason": top["match"],
            "matched": warn_matches,
        }

    return {"blocked": False, "severity": None, "label": None, "reason": None, "matched": []}


def detect_injection(text: str) -> str | None:
    """Return a BLOCK reason label when the message must be hard-blocked.

    WARN-level signals are not returned here so benign messages are never
    blocked; use ``analyze_injection`` to surface them for logging.
    """
    result = analyze_injection(text)
    return result["label"] if result["severity"] == BLOCK else None
