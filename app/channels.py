"""统一渠道账号接入目录与连通性判定（纯内核，不联网、不写库）。

设计原则：
- 只收录**官方合规渠道**的官方 API；个人微信 / 个人 WhatsApp 的非官方桥接
  （web.whatsapp、itchat 等）违反平台条款、有封号风险，一律不收录。
- 本模块只做渠道元数据与"是否已填凭证"的确定性判定；真实收发消息与联网
  连通探测需要对应平台凭证与 webhook，到位前状态停在 configured（待联调），
  **绝不伪造 connected**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 账号状态
ST_CREDENTIAL_REQUIRED = "credential_required"  # 未填凭证
ST_CONFIGURED = "configured"                    # 已填凭证，待真实联调
ST_ERROR = "error"                               # 上次联调报错（由回执写入）


@dataclass(frozen=True)
class ChannelSpec:
    type: str
    name: str
    auth: str            # 需要什么凭证 / 授权方式
    needs_review: bool   # 是否需平台审核/资质
    risk: str            # low / medium / high
    risk_note: str
    direction: str       # inbound / outbound / both
    capabilities: list[str] = field(default_factory=list)


# 官方合规渠道目录（依据各平台开放平台现行接入方式）
CHANNEL_CATALOG: list[ChannelSpec] = [
    ChannelSpec(
        type="webchat", name="网站在线客服",
        auth="无需外部凭证，控制台内嵌 JS 挂件",
        needs_review=False, risk="low",
        risk_note="自带渠道，零外部依赖，最先可用。",
        direction="both", capabilities=["inbound_chat", "ticket", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="telegram", name="Telegram Bot",
        auth="@BotFather 申请 Bot Token",
        needs_review=False, risk="low",
        risk_note="免审核、最快上线；用户先发起会话，24h 窗口内可回。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff", "rich_media"],
    ),
    ChannelSpec(
        type="whatsapp_cloud", name="WhatsApp Business Cloud API",
        auth="Meta Business 账号 + 已验证业务手机号 + Access Token + Phone Number ID",
        needs_review=True, risk="low",
        risk_note="官方 Meta 托管；模板消息需审核，客服窗口外只能发审核模板。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff", "template_msg"],
    ),
    ChannelSpec(
        type="instagram", name="Instagram Messaging",
        auth="Instagram 专业账号（Business/Creator）+ Meta 应用授权",
        needs_review=True, risk="low",
        risk_note="2024 后可不绑 FB 主页；用户先 DM，遵守 24h 窗口。",
        direction="both", capabilities=["inbound_dm", "comment", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="messenger", name="Facebook Messenger",
        auth="Facebook 公共主页 + Meta 应用 Page Token",
        needs_review=True, risk="low",
        risk_note="用户先发起会话；自动消息仅限与其咨询相关内容。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="line", name="LINE Official Account",
        auth="LINE 官方账号 + Messaging API Channel Token",
        needs_review=True, risk="low",
        risk_note="企业认证后开放 Messaging API。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff", "broadcast"],
    ),
    ChannelSpec(
        type="slack", name="Slack",
        auth="Slack App OAuth 一键授权（@mention / DM）",
        needs_review=False, risk="low",
        risk_note="偏内部团队问答与机器人，非对外售后。",
        direction="both", capabilities=["mention", "dm", "summary"],
    ),
    ChannelSpec(
        type="discord", name="Discord Bot",
        auth="Discord Developer 应用 Bot Token",
        needs_review=False, risk="low",
        risk_note="社区/服务器客服场景。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="email", name="邮箱支持",
        auth="support@ 邮箱的 SMTP 发信 + IMAP 收信（复用系统 SMTP 配置）",
        needs_review=False, risk="low",
        risk_note="把 support@ 变成 AI 收件箱；回信需保留人工可接管。",
        direction="both", capabilities=["ticket", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="wecom", name="企业微信客服",
        auth="企业微信客服企业主体 + API 密钥 + 回调配置",
        needs_review=True, risk="medium",
        risk_note="需企业主体认证；按企业微信客服 API 规范接入。",
        direction="both", capabilities=["inbound_chat", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="douyin", name="抖音私信/线索",
        auth="抖音开放平台 ToB SaaS 应用 + appId/secret + Webhook，需 BRD 审核",
        needs_review=True, risk="medium",
        risk_note="仅面向线索投广商家的智能客服（私信接待），需平台准入审核。",
        direction="both", capabilities=["lead", "inbound_dm", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="kuaishou", name="快手私信",
        auth="快手开放平台网站应用 appId/appSecret + Webhook",
        needs_review=True, risk="medium",
        risk_note="需开放平台应用与能力申请。",
        direction="both", capabilities=["inbound_dm", "ai_reply", "human_handoff"],
    ),
    ChannelSpec(
        type="sms", name="短信",
        auth="第三方短信商（如 Twilio）Account SID / Auth Token",
        needs_review=False, risk="medium",
        risk_note="短信强营销易投诉；用于通知/验证码，不做骚扰。",
        direction="outbound", capabilities=["notification", "verification"],
    ),
]

_CATALOG_BY_TYPE = {c.type: c for c in CHANNEL_CATALOG}


def catalog() -> list[dict[str, Any]]:
    """只读目录，供前端渲染可选渠道与风险说明。"""
    return [
        {
            "type": c.type, "name": c.name, "auth": c.auth,
            "needs_review": c.needs_review, "risk": c.risk,
            "risk_note": c.risk_note, "direction": c.direction,
            "capabilities": list(c.capabilities),
        }
        for c in CHANNEL_CATALOG
    ]


def spec_for(channel_type: str) -> ChannelSpec | None:
    return _CATALOG_BY_TYPE.get(channel_type)


def is_known(channel_type: str) -> bool:
    return channel_type in _CATALOG_BY_TYPE


def probe_status(credential_enc: str, enabled: bool = True,
                 last_error: str = "") -> str:
    """确定性连通性判定（不联网）。

    - 停用 -> error 也不合理；停用不计连通，返回 configured 但前端显示停用。
    - 有密文凭证 -> configured（已填，待真实联调）。
    - 无凭证 -> credential_required。
    - last_error 非空 -> error。
    """
    if last_error.strip():
        return ST_ERROR
    if (credential_enc or "").strip():
        return ST_CONFIGURED
    return ST_CREDENTIAL_REQUIRED
