"""社交运营 Agent 纯逻辑：中性回复草稿、热门视频热门点提炼、发布闸门（纯函数）。

安全：回复仅生成草稿，不自动发送；发布必须 content 已 approved→ready 且渠道凭证
齐备，否则一律 credential_required，绝不伪造发布成功或平台数据。
"""
from __future__ import annotations

from . import content_pipeline as cp

POSITIVE = {"很好", "喜欢", "不错", "棒", "赞", "想要", "好看", "实用", "感谢", "好评", "想买"}
NEGATIVE = {"太差", "太贵", "骗", "失望", "投诉", "退款", "假货", "垃圾", "退货", "太慢", "差评"}


def sentiment(text: str) -> str:
    """粗分情感：positive / neutral / negative（确定性关键词，仅供归档，不做承诺）。"""
    t = text or ""
    pos = sum(1 for w in POSITIVE if w in t)
    neg = sum(1 for w in NEGATIVE if w in t)
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def reply_draft(comment_text: str, sentiment_label: str = "neutral",
                brand_tone: str = "友好") -> dict:
    """生成中性回复草稿（不自动发、不承诺退款/赔偿/疗效）。"""
    t = (comment_text or "").strip()[:120]
    if sentiment_label == "negative":
        body = "感谢反馈，我们已记录你的问题，会由负责人核实后私信联系处理，请先勿重复下单。"
    elif sentiment_label == "positive":
        body = "谢谢喜欢！具体规格/价格可以私信咨询，按需为你介绍。"
    else:
        body = "你好，感谢留言，详细情况可以私信沟通，我们按需为你解答。"
    return {
        "draft": body,
        "sentiment": sentiment_label,
        "status": "draft",  # draft | approved_reply | sent（发送需老板确认+渠道凭证）
        "auto_sent": False,
    }


def publish_gate(content_status: str, channel_configured: bool) -> tuple[bool, str]:
    """发布闸门：状态必须 ready_to_publish，且渠道凭证已配置。"""
    ok, reason = cp.gate_publish(content_status)
    if not ok:
        return False, reason
    if not channel_configured:
        return False, "渠道未配置凭证（平台 token/endpoint），无法发布，待补凭证位"
    return True, "可进入发布队列"


def analyze_trending(title: str, metrics: dict, category: str = "") -> dict:
    """确定性热门点提炼（不联网、不编造数据）：基于给定指标算互动结构与标签。"""
    m = metrics or {}
    views = float(m.get("views") or 0)
    likes = float(m.get("likes") or 0)
    comments = float(m.get("comments") or 0)
    shares = float(m.get("shares") or 0)
    engagement = likes + comments + shares
    eng_rate = (engagement / views) if views > 0 else 0.0
    comment_rate = (comments / views) if views > 0 else 0.0
    share_rate = (shares / views) if views > 0 else 0.0

    hot_points: list[str] = []
    if comment_rate >= 0.02:
        hot_points.append("评论区热议（评论率高，有讨论冲突或共鸣点）")
    if share_rate >= 0.01:
        hot_points.append("强社交货币（转发率高，用户愿意扩散）")
    if views > 0 and likes / views >= 0.05:
        hot_points.append("高赞共鸣（点赞率高，情绪认同强）")
    if comment_rate > 0 and comments > likes:
        hot_points.append("争议/疑问驱动（评论多于点赞，存在待答疑点）")
    title_t = title or ""
    if any(w in title_t for w in ("怎么", "为什么", "居然", "竟然", "避坑", "推荐")):
        hot_points.append("标题钩子（疑问/反差/避坑词吸引点击）")
    if not hot_points:
        hot_points.append("指标不足，需补充播放/点赞/评论/转发后再提炼")

    return {
        "title": (title or "").strip()[:200],
        "category": (category or "").strip()[:60],
        "metrics": {"views": views, "likes": likes, "comments": comments,
                    "shares": shares, "engagement": engagement,
                    "eng_rate": round(eng_rate, 4), "comment_rate": round(comment_rate, 4),
                    "share_rate": round(share_rate, 4)},
        "hot_points": hot_points,
        "summary": f"互动量 {int(engagement)}，互动率 {eng_rate:.2%}；热门点：" + "；".join(hot_points),
    }
