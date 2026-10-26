"""文案写作 Agent 纯逻辑：确定性卖点骨架与合规自检（纯函数，不联网、不调模型）。

定位：无可用模型凭证时，产出结构完整、错误率低的本地骨架；模型润色由所选
模型在端点层接入（凭证位），本模块绝不虚构功效、不使用绝对化/医疗承诺用语。
"""
from __future__ import annotations

# 广告法/平台常见高风险绝对化与承诺用语，仅作提醒，不自动改写
COMPLIANCE_WATCH = [
    "最佳", "最好", "第一", "国家级", "世界级", "顶级", "绝对", "100%",
    "根治", "治愈", "包治", "无效退款", "永久", "全网最低", "史无前例",
]


def _clean(text: str, limit: int = 120) -> str:
    return (text or "").strip()[:limit]


def build_copy_brief(product: str, audience: str, selling_points: list[str],
                     tone: str = "专业", locale: str = "zh") -> dict:
    """由商品/人群/卖点生成结构化文案骨架（确定性，不编造未提供的功效）。

    返回 headlines / pain_point / points / cta / versions / compliance_notes。
    """
    product = _clean(product, 80)
    audience = _clean(audience, 80)
    points = [_clean(p, 120) for p in (selling_points or []) if _clean(p, 120)]
    tone = _clean(tone, 20) or "专业"

    headlines = [
        f"{product}：{points[0] if points else '把核心优势说清楚'}，给{audience or '目标人群'}的实在选择",
        f"为什么{audience or '你'}会需要{product}？看这几点",
        f"{product} · {tone}版卖点速览",
    ]
    pain_point = (
        f"面向{audience or '目标人群'}，先讲清使用场景里的真实痛点，"
        f"再用{product}对应解决，避免空喊口号。"
    )
    point_lines = [f"· {p}" for p in points] or ["· 待补充核心卖点（请勿虚构未验证的功效）"]
    cta = "需要了解/试用可直接咨询，按需再报价，不催促、不夸大承诺。"

    short = f"【{product}】{points[0] if points else '核心卖点待补充'}。{cta}"
    detail = "\n".join([
        f"# {product}",
        f"目标人群：{audience or '待定'}",
        f"风格：{tone}",
        "",
        "## 痛点切入",
        pain_point,
        "",
        "## 核心卖点",
        *point_lines,
        "",
        "## 行动号召",
        cta,
    ])
    social = f"{headlines[0]}\n" + "\n".join(point_lines) + f"\n{cta}"

    blob = " ".join(headlines + point_lines + [detail])
    compliance_notes = [w for w in COMPLIANCE_WATCH if w in blob]

    return {
        "product": product,
        "audience": audience,
        "tone": tone,
        "headlines": headlines,
        "pain_point": pain_point,
        "points": points,
        "point_lines": point_lines,
        "cta": cta,
        "versions": {"short": short, "detail": detail, "social": social},
        "compliance_notes": compliance_notes,
        "model_required": True,
    }


def requires_model(brief: dict) -> bool:
    """是否建议接模型润色（骨架永远可本地成稿，润色非必须）。"""
    return bool(brief.get("model_required"))
