"""Karma 结算接入向导：认证状态机 + 步骤推导（纯函数，可测）。

真实用户要跑通 Karma 非托管结算，需要经过完整闭环：
  实名认证(KYC, Karma 官网) → 创建 Agent/Identity → 生成 API Key
  → 绑定到 Console（手动填入或授权回调） → 连通自检 → 激活使用。

Console 负责：引导、跳转、保存凭证(加密落库)、连通自检回执、状态展示、解除绑定。
实名认证与 KYC 本身必须发生在 Karma 官网（法务职责在 Karma 服务端），
Console 只做"用户确认已认证"的登记，绝不代替或伪造认证结果。

原则：不伪造联调。凭证缺失/自检失败时明确返回 not_connected 与具体原因。
"""
from __future__ import annotations

# 向导步骤（顺序固定，前端按此渲染进度）
STEPS = [
    {
        "id": "kyc",
        "title": "实名认证",
        "desc": "在 Karma 官网完成个人 / 企业实名认证（KYC）",
    },
    {
        "id": "identity",
        "title": "创建身份",
        "desc": "创建 Agent 身份与 Identity，获得 AGENT_ID / IDENTITY_ID",
    },
    {
        "id": "api_key",
        "title": "生成密钥",
        "desc": "在 Karma 官网生成 API Key",
    },
    {
        "id": "bind",
        "title": "绑定控制台",
        "desc": "把凭证填入本控制台（或通过授权回调自动绑定）",
    },
    {
        "id": "activate",
        "title": "激活",
        "desc": "连通自检通过后，结算中心进入已激活状态",
    },
]

OFFICIAL_SITE = "https://karma.xyz"  # 占位：以 Karma 官网实际地址为准


def derive_state(karma: dict | None) -> dict:
    """根据 settings["karma"] 推导向导状态（纯函数，可单测）。"""
    karma = dict(karma or {})
    has_key = bool(karma.get("api_key_enc") or karma.get("api_key"))
    has_agent = bool(karma.get("agent_id"))
    has_identity = bool(karma.get("identity_id"))
    kyc = bool(karma.get("kyc_confirmed"))
    tested = bool(karma.get("test_ok"))
    base_url = karma.get("base_url") or ""

    steps: list[dict] = []
    for s in STEPS:
        sid = s["id"]
        if sid == "kyc":
            done, current = kyc, not kyc
        elif sid == "identity":
            done = has_identity and has_agent
            current = kyc and not done
        elif sid == "api_key":
            done = has_key
            current = kyc and (has_identity and has_agent) and not done
        elif sid == "bind":
            done = has_key and has_agent
            current = kyc and has_key and not done
        else:  # activate
            done = has_key and has_agent and tested
            current = kyc and has_key and has_agent and not done
        steps.append({**s, "done": done, "current": current})

    if has_key and has_agent and tested:
        overall = "active"
    elif has_key and has_agent:
        overall = "bound"
    elif has_key:
        overall = "configured"
    elif kyc:
        overall = "kyc"
    else:
        overall = "not_started"

    return {
        "overall": overall,
        "official_site": OFFICIAL_SITE,
        "steps": steps,
        "summary": {
            "kyc_confirmed": kyc,
            "agent_id": has_agent,
            "identity_id": has_identity,
            "api_key": has_key,
            "tested": tested,
            "base_url": base_url,
        },
    }
