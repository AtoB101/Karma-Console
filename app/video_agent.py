"""视频 Agent 纯逻辑：分镜脚本与制作参数单（纯函数，不联网、不生成真实视频）。

真实视频生成（文生视频/图生视频 API）需外部凭证，本模块只产出可执行的脚本、
分镜表与参数单，并把生成器标为 credential_required，绝不伪造成片 URL 或成功状态。
"""
from __future__ import annotations

ASPECTS = {"9:16": "竖屏（抖音/视频号/Reels）", "16:9": "横屏（B站/YouTube）", "1:1": "方形"}
DEFAULT_ASPECT = "9:16"


def _clean(text: str, limit: int = 160) -> str:
    return (text or "").strip()[:limit]


def build_video_plan(topic: str, audience: str, duration_sec: int = 30,
                     aspect: str = DEFAULT_ASPECT, style: str = "纪实感",
                     shots: list[dict] | None = None) -> dict:
    """由选题/人群生成分镜脚本与参数单（确定性，不虚构画面效果承诺）。"""
    topic = _clean(topic, 120)
    audience = _clean(audience, 80)
    aspect = aspect if aspect in ASPECTS else DEFAULT_ASPECT
    try:
        duration = max(5, min(int(duration_sec), 180))
    except (TypeError, ValueError):
        duration = 30

    raw_shots = shots or []
    storyboard = []
    for i, s in enumerate(raw_shots, start=1):
        storyboard.append({
            "seq": i,
            "shot": _clean(s.get("shot") or s.get("visual") or f"镜头{i}", 80),
            "visual": _clean(s.get("visual") or "", 200),
            "voiceover": _clean(s.get("voiceover") or s.get("line") or "", 200),
            "duration_sec": _clean(str(s.get("duration_sec") or 0), 12) or None,
        })
    if not storyboard:
        storyboard = [
            {"seq": 1, "shot": "开场钩子", "visual": f"用一个画面点出{topic}的核心看点",
             "voiceover": f"一句话抓住{audience or '目标人群'}注意", "duration_sec": 3},
            {"seq": 2, "shot": "主体展示", "visual": "产品/服务主体清晰展示，卖点逐条出现",
             "voiceover": "逐条讲清核心卖点，不夸大", "duration_sec": max(5, duration - 8)},
            {"seq": 3, "shot": "行动号召", "visual": "结尾引导咨询，不催促",
             "voiceover": "需要了解可直接咨询，按需报价", "duration_sec": 5},
        ]

    script_lines = [f"# 视频脚本：{topic}", f"目标人群：{audience or '待定'}",
                    f"时长：{duration}s · 画幅：{aspect}（{ASPECTS[aspect]}）· 风格：{style}", ""]
    for s in storyboard:
        script_lines.append(f"镜头{s['seq']}｜{s['shot']}：{s['visual']}｜旁白：{s['voiceover']}")
    script = "\n".join(script_lines)

    return {
        "topic": topic,
        "audience": audience,
        "params": {
            "duration_sec": duration,
            "aspect": aspect,
            "aspect_label": ASPECTS[aspect],
            "style": _clean(style, 40),
        },
        "storyboard": storyboard,
        "script": script,
        "generation": {
            "status": "credential_required",
            "provider": "",
            "note": "需配置文生视频/图生视频 API 凭证后才能真实生成，本步不伪造视频",
        },
    }
