"""动脑决策快速方案与预案库（纯本地、可被 CEO/主脑检索调用）。

每条预案：触发信号 → 首选快速方案 → B 计划 → 成本代价 → 最坏结果 →
轻重缓急 → 拍板人（auto 主脑自决 / ceo 经理人 / boss 老板拍板）→ 是否强制升级。

原则：固定重复、低错误率的事自动做；涉及钱、不可逆、对外承诺、政策的事
一律草案 + 老板拍板，AI 不替老板承诺。本模块只给确定性方案，不联网、不写库。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Playbook:
    pid: str
    title: str
    trigger: str          # 触发信号（人读描述）
    keywords: tuple[str, ...]  # 用于文本命中的敏感/特征词
    priority: str         # P0 紧急 / P1 高 / P2 常规
    primary: str          # 首选快速方案
    plan_b: str          # 预备方案
    cost: str            # 成本代价
    worst: str           # 最坏结果
    owner: str           # auto / ceo / boss
    escalate: bool       # 是否强制升级老板


PLAYBOOKS: list[Playbook] = [
    Playbook(
        pid="refund_dispute",
        title="客户要求退款/赔偿/资金争议",
        trigger="消息命中 退款/赔偿/纠纷/钱没到账/12315/起诉/律师",
        keywords=("退款", "赔偿", "纠纷", "钱没到账", "12315", "起诉", "律师", "退钱"),
        priority="P0",
        primary="AI 不承诺金额，先共情+记录事实，立即建工单并升级老板拍板",
        plan_b="老板离线时：冻结自动回复，保留完整会话与订单证据，排队待老板处理",
        cost="一次人工介入；延迟响应可能影响评价，但避免错误赔付",
        worst="未经拍板承诺赔付造成资金损失或二次纠纷",
        owner="boss", escalate=True,
    ),
    Playbook(
        pid="complaint_review",
        title="差评/投诉/监管词",
        trigger="命中 投诉/差评/曝光/举报/315/骗人/骗子",
        keywords=("投诉", "差评", "曝光", "举报", "315", "骗人", "骗子", "维权"),
        priority="P0",
        primary="立即转人工 P0 工单，AI 只做安抚话术，不争辩、不删评承诺",
        plan_b="无人在线时：自动发致歉+稍后专人联系，记录并推送老板通知",
        cost="需专人跟进，可能需要补偿券（仍需批）",
        worst="AI 生硬回复激化舆情或对外失实承诺",
        owner="boss", escalate=True,
    ),
    Playbook(
        pid="low_stock",
        title="低库存/缺货预警",
        trigger="可用库存低于安全线或商品被问有无货",
        keywords=("缺货", "没货", "库存", "补货", "断货"),
        priority="P1",
        primary="生成补货草稿（数量/供应商建议）待老板批，前台显示‘补货中’而非断联",
        plan_b="暂不下架：限购/登记到货提醒；或临时隐藏该 SKU 避免超卖",
        cost="补货占用现金流；限购可能损失即时成交",
        worst="超卖导致发不出货、违约与差评",
        owner="ceo", escalate=False,
    ),
    Playbook(
        pid="hot_lead",
        title="热线索 H 级需快速首触",
        trigger="评分≥80：主动问价+要联系方式+多次互动",
        keywords=("报价", "多少钱", "价格", "怎么卖", "联系"),
        priority="P1",
        primary="15-30 分钟内 AI 按 SOP 首触，发报价/预约，记录回复",
        plan_b="AI 答不准或客户要深度方案：转人工并带全上下文交接",
        cost="少量自动外发话术成本",
        worst="响应过慢线索流失；过度推销引起反感",
        owner="ceo", escalate=False,
    ),
    Playbook(
        pid="llm_down",
        title="主模型不可用/超时",
        trigger="LLM 调用失败、超时、鉴权错误",
        keywords=("模型", "超时", "不可用", "报错"),
        priority="P1",
        primary="降级到 FAQ 标准应答+模板话术，明确告知稍后专人，不编造答案",
        plan_b="排队待恢复，期间把客户消息沉淀为待办，恢复后自动续接",
        cost="智能回复质量下降，但保证不胡编",
        worst="模型故障时编造参数/承诺，误导客户",
        owner="auto", escalate=False,
    ),
    Playbook(
        pid="channel_down",
        title="外部渠道断连/未配置",
        trigger="渠道未授权、凭证失效、超时；状态 unavailable",
        keywords=("渠道", "未配置", "断连", "凭证失效"),
        priority="P2",
        primary="标记 unavailable，本地消息先沉淀，不伪造‘已送达/已连通’",
        plan_b="切换到备用渠道（邮件/站内）触达，通知老板补凭证",
        cost="该渠道暂时不能收发",
        worst="假装已发送导致客户侧无回应、信息丢失",
        owner="auto", escalate=False,
    ),
    Playbook(
        pid="security_alert",
        title="安全告警/注入/异常登录",
        trigger="命中注入规则、高频失败鉴权、异常设备、越权尝试",
        keywords=("注入", "越狱", "异常登录", "越权", "攻击"),
        priority="P0",
        primary="确定性拦截+写审计链+通知老板，必要时临时禁用该通道/吊销设备",
        plan_b="老板离线：保持拦截态并锁定敏感操作，仅放行只读",
        cost="短时锁定影响该入口使用",
        worst="放行注入或越权导致数据泄露/被操控",
        owner="boss", escalate=True,
    ),
    Playbook(
        pid="backup_fail",
        title="备份失败/介质异常",
        trigger="定时备份报错、磁盘不可写、校验失败",
        keywords=("备份失败", "磁盘", "校验失败"),
        priority="P1",
        primary="立即告警并触发手动补备份，业务转只读优先保护现有数据",
        plan_b="更换备用目录/云盘重存，恢复后做一次完整性抽测",
        cost="一次人工补备；短暂只读",
        worst="备份缺失导致误删后不可恢复",
        owner="ceo", escalate=False,
    ),
    Playbook(
        pid="sla_breach",
        title="工单 SLA 超时",
        trigger="P0/P1 工单超过响应或处理时限未闭环",
        keywords=("超时", "SLA", "未响应"),
        priority="P1",
        primary="自动提醒负责人并升级优先级，附带工单全时间线",
        plan_b="二次超时升级老板介入，重新分派",
        cost="管理跟进成本",
        worst="客户长期无响应升级为投诉",
        owner="ceo", escalate=False,
    ),
    Playbook(
        pid="price_policy",
        title="批量调价/改商业政策/正式发布",
        trigger="批量改价、退换政策、上架发布等难逆动作",
        keywords=("批量改价", "调价", "改政策", "正式发布"),
        priority="P0",
        primary="AI 只生成草案与影响面测算，必须老板逐项拍板后执行",
        plan_b="老板未批：维持现状，草案留存待批",
        cost="决策需老板时间，但避免价格事故",
        worst="自动批量改价/发布造成营收或合规事故",
        owner="boss", escalate=True,
    ),
]


def _match(p: Playbook, text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in p.keywords) or p.title.lower() in t


def search_playbook(keyword: str) -> list[dict]:
    """按关键词检索预案（标题/触发/关键词/首选方案模糊匹配），返回字典列表。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return [asdict(p) for p in PLAYBOOKS]
    hits = []
    for p in PLAYBOOKS:
        blob = f"{p.title} {p.trigger} {p.primary} {p.plan_b} {' '.join(p.keywords)}".lower()
        if kw in blob:
            hits.append(asdict(p))
    return hits


def must_escalate(trigger_text: str) -> list[dict]:
    """文本命中任一‘强制升级老板’预案的敏感词即返回，供 CEO 分派闸兜底。"""
    return [asdict(p) for p in PLAYBOOKS if p.escalate and _match(p, trigger_text or "")]
