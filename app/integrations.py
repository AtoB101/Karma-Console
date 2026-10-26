"""Integration readiness registry: declared adapters, credential slots, and probes.

联调准备层（不伪造、不联网）：

- 这里只**声明**每个外部系统接进来需要哪些凭证位、要实现哪些真实方法，
  以及"是否已填凭证"。它**绝不**在缺凭证时返回成功/已连接。
- 真实 HTTP 调用仍走 ``connectors.py`` / ``karma.py``；本层不发起任何网络请求，
  也不编造回执，只回答"离能联调还差什么"。
- 已填凭证只代表 ``ready_to_probe``（可发起真实连通测试），不等于已经连通；
  真实连通必须等凭证到位后做一次手动/自动 ping，结果以对端为准。

设计原则：missing 逐项列出；任何未配置项 status 一律 ``unavailable``。
"""
from __future__ import annotations

import os

# A credential slot: how it is read (env var) and its human label.
# secret=True means it must never be echoed back in plain text by the UI.
CRED = tuple[dict, ...]


def _slot(env: str, label: str, *, secret: bool = False, optional: bool = False) -> dict:
    return {"env": env, "label": label, "secret": secret, "optional": optional}


# Declared adapters. `required` = mandatory credential slots; `methods` = the real
# calls this adapter must implement before go-live (skeleton only for now).
REGISTRY: list[dict] = [
    {
        "key": "karma_payment",
        "label": "Karma 资金记账 / 结算",
        "category": "payment",
        "connector_type": "karma_payment",
        "required": (
            _slot("KRAMA_API_KEY", "Karma API Key", secret=True),
            _slot("KRAMA_AGENT_ID", "Agent 身份 ID"),
            _slot("KRAMA_IDENTITY_ID", "Identity / 托管身份 ID"),
        ),
        "optional": (_slot("KRAMA_API_BASE_URL", "Karma API Base URL", optional=True),),
        "methods": [
            "create_contract 建托管合约",
            "lock_capacity 锁定 USDC 托管",
            "verify_delivery 交付验证",
            "create_settlement 结算 / 释放",
            "regret 退款反悔",
            "dispute 争议上报",
            "reconcile 拉取结算流水并对账",
        ],
    },
    {
        "key": "ecommerce_independent",
        "label": "独立站商城",
        "category": "ecommerce",
        "connector_type": "ecommerce_independent",
        "required": (
            _slot("ECOM_INDEP_ENDPOINT", "订单/回传接口地址 endpoint_url"),
        ),
        "optional": (
            _slot("ECOM_INDEP_API_KEY", "独立站 API Key", secret=True, optional=True),
        ),
        "methods": [
            "create_order 推送/创建订单",
            "cancel_order 取消订单回传",
            "sync_inventory 库存同步",
            "push_shipment 发货状态回传",
        ],
    },
    {
        "key": "ecommerce_social",
        "label": "社交网店",
        "category": "ecommerce",
        "connector_type": "ecommerce_social",
        "required": (_slot("ECOM_SOCIAL_ENDPOINT", "订单/回传接口地址 endpoint_url"),),
        "optional": (_slot("ECOM_SOCIAL_API_KEY", "社交网店 API Key", secret=True, optional=True),),
        "methods": [
            "create_order 创建订单",
            "cancel_order 取消回传",
            "push_shipment 发货回传",
            "sync_inventory 库存同步",
        ],
    },
    {
        "key": "taobao",
        "label": "淘宝开放平台（TOP）",
        "category": "marketplace",
        "connector_type": "ecommerce_marketplace",
        "signer": "taobao",
        "required": (
            _slot("TAOBAO_APP_KEY", "App Key"),
            _slot("TAOBAO_APP_SECRET", "App Secret", secret=True),
            _slot("TAOBAO_ACCESS_TOKEN", "用户授权 session / access_token", secret=True),
        ),
        "optional": (),
        "methods": [
            "trades_sold_get 拉取已售订单",
            "logistics_send 发货回传",
            "refund_query 退款单查询",
        ],
    },
    {
        "key": "douyin",
        "label": "抖音电商（抖店）",
        "category": "marketplace",
        "connector_type": "ecommerce_marketplace",
        "signer": "douyin",
        "required": (
            _slot("DOUYIN_APP_ID", "App ID"),
            _slot("DOUYIN_APP_SECRET", "App Secret", secret=True),
            _slot("DOUYIN_ACCESS_TOKEN", "access_token", secret=True),
        ),
        "optional": (),
        "methods": [
            "order_list 拉取订单",
            "ship_order 发货回传",
            "aftersale_list 售后单查询",
        ],
    },
    {
        "key": "sfexpress",
        "label": "顺丰速运（物流）",
        "category": "logistics",
        "connector_type": "logistics",
        "signer": "sfexpress",
        "required": (
            _slot("SF_PARTNER_ID", "partnerID 客户编码"),
            _slot("SF_CHECKWORD", "校验码 checkword", secret=True),
            _slot("SF_SERVICE_CODE", "serviceCode 产品/服务编码"),
            _slot("SF_ENDPOINT", "接口地址 endpoint_url"),
        ),
        "optional": (),
        "methods": [
            "create_order 下单取号",
            "query_route 运单轨迹追踪",
            "sign_back 签收回传",
        ],
    },
    {
        "key": "logistics_generic",
        "label": "通用物流（自定义 HTTP）",
        "category": "logistics",
        "connector_type": "logistics",
        "required": (_slot("LOGISTICS_ENDPOINT", "物流轨迹接口 endpoint_url"),),
        "optional": (_slot("LOGISTICS_API_KEY", "物流 API Key", secret=True, optional=True),),
        "methods": [
            "track 运单轨迹查询",
            "push_waybill 运单号回写",
        ],
    },
    {
        "key": "wechat_cs",
        "label": "微信客服 / 企业微信（获客接待）",
        "category": "support",
        "connector_type": "im_channel",
        "required": (
            _slot("WX_CORP_ID", "企业 CorpID"),
            _slot("WX_AGENT_ID", "接待应用 AgentID"),
            _slot("WX_CORP_SECRET", "应用 Secret", secret=True),
        ),
        "optional": (
            _slot("WX_CALLBACK_TOKEN", "回调校验 Token", secret=True, optional=True),
            _slot("WX_CALLBACK_AES_KEY", "回调 EncodingAESKey", secret=True, optional=True),
        ),
        "methods": [
            "pull_messages 拉取入站私信",
            "send_reply 回发标准答复",
            "assign_session 转接人工 / 会话分配",
            "sync_contact 客户资料回传（E2EE 密文）",
        ],
    },
    {
        "key": "douyin_im",
        "label": "抖音私信 / 客服（获客接待）",
        "category": "support",
        "connector_type": "im_channel",
        "signer": "douyin",
        "required": (
            _slot("DOUYIN_IM_APP_ID", "App ID"),
            _slot("DOUYIN_IM_APP_SECRET", "App Secret", secret=True),
            _slot("DOUYIN_IM_ACCESS_TOKEN", "access_token", secret=True),
        ),
        "optional": (),
        "methods": [
            "poll_dm 拉取私信入站",
            "send_dm 回发私信",
            "bind_lead 私信转线索",
            "assign_agent 转接人工客服",
        ],
    },
    {
        "key": "call_center",
        "label": "电话 / 呼叫中心（AI 虚拟号接待）",
        "category": "support",
        "connector_type": "voice_channel",
        "required": (
            _slot("CALL_ENDPOINT", "呼叫中心接口 endpoint_url"),
            _slot("CALL_API_KEY", "呼叫中心 API Key", secret=True),
        ),
        "optional": (
            _slot("VIRTUAL_PHONE_POOL", "AI 专用虚拟号池配置", optional=True),
        ),
        "methods": [
            "place_call 外呼 / 接听",
            "transcribe 通话转写（密文落库）",
            "virtual_number 分配 AI 专用虚拟号，隐藏真实号",
            "route_to_human 坐席转接",
        ],
    },
]


def _env_get(env: str, env_overrides: dict | None) -> str:
    if env_overrides and env in env_overrides:
        return str(env_overrides[env] or "")
    return os.environ.get(env, "")


def probe(spec: dict, env_overrides: dict | None = None) -> dict:
    """Local-only credential presence check. Never contacts the network.

    Returns configured/missing slots and a status of either
    ``ready_to_probe`` (mandatory slots filled; real ping still required) or
    ``unavailable`` (something mandatory is missing). It never claims success.
    """
    missing: list[dict] = []
    present: list[str] = []
    for slot in spec["required"]:
        value = _env_get(slot["env"], env_overrides)
        if value.strip():
            present.append(slot["env"])
        else:
            missing.append({"env": slot["env"], "label": slot["label"], "secret": slot["secret"]})
    optional_filled = [
        slot["env"]
        for slot in spec.get("optional", ())
        if _env_get(slot["env"], env_overrides).strip()
    ]
    configured = not missing
    return {
        "key": spec["key"],
        "label": spec["label"],
        "category": spec["category"],
        "connector_type": spec.get("connector_type", ""),
        "signer": spec.get("signer", ""),
        "status": "ready_to_probe" if configured else "unavailable",
        "configured": configured,
        "missing": missing,
        "present": present,
        "optional_present": optional_filled,
        "methods": list(spec["methods"]),
        "note": "已填凭证仅代表可发起真实连通测试，不等于已连通；真实状态以对端回执为准。"
        if configured
        else "缺少必填凭证位，当前不可联调；系统不会伪造连接成功。",
    }


def describe_all(env_overrides: dict | None = None) -> dict:
    """Summarize every declared adapter for the integration status panel."""
    rows = [probe(spec, env_overrides) for spec in REGISTRY]
    ready = sum(1 for r in rows if r["configured"])
    return {
        "total": len(rows),
        "ready_to_probe": ready,
        "unavailable": len(rows) - ready,
        "adapters": rows,
    }
