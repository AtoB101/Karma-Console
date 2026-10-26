"""可执行工作流图（DAG）内核：拓扑排序、环检测、校验、安全白名单。

纯函数，不写库、不联网、不发起真实动作；供图保存前校验与运行前规划。
安全原则：
- 只有 ``SAFE_ACTIONS`` 里的只读/可逆动作可被自动执行；
- ``NEED_APPROVAL`` 里的资金/不可逆/改政策动作绝不自动跑，只标记待老板拍板；
- 图必须无环、无悬空边、节点 id 唯一，否则拒绝运行。
"""
from __future__ import annotations

# 可自动执行的安全动作（只读聚合或本地可逆草稿），key=action，value=中文名
SAFE_ACTIONS: dict[str, str] = {
    "overview": "经营总览",
    "leads": "线索分级",
    "tickets": "工单看板",
    "faqs": "FAQ 库",
    "inventory": "库存只读",
    "orders": "订单只读",
    "customers": "客户只读",
    "notify": "通知聚合",
    "draft_product": "商品草稿（不发布）",
    "draft_copy": "营销文案草稿",
}

# 必须老板拍板、绝不自动执行的动作（资金/不可逆/改政策）
NEED_APPROVAL: dict[str, str] = {
    "make_payment": "收款",
    "make_refund": "退款",
    "dispute_payment": "资金争议",
    "bulk_update_prices": "批量调价",
    "set_commerce_policy": "改商业政策",
    "publish_product": "正式发布上架",
}


def _node_index(nodes: list[dict]) -> dict[str, dict]:
    return {str(n.get("id")): n for n in (nodes or []) if n.get("id") is not None}


def validate_graph(nodes: list[dict], edges: list[dict]) -> dict:
    """校验图结构：id 唯一、边两端存在、无环。返回 {valid, errors}。"""
    errors: list[str] = []
    nodes = nodes or []
    edges = edges or []
    ids = [str(n.get("id")) for n in nodes if n.get("id") is not None]
    dup = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dup):
        errors.append(f"节点 id 重复：{d}")
    idx = _node_index(nodes)
    for e in edges:
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a not in idx:
            errors.append(f"边起点不存在：{a}")
        if b not in idx:
            errors.append(f"边终点不存在：{b}")
    # 控制节点语义检查：branch 必须带可求值条件
    for n in nodes:
        if str(n.get("action")) == "branch":
            cond = (n.get("config") or {}).get("condition") or {}
            if not cond.get("source") or not cond.get("field"):
                errors.append(f"分支节点 {n.get('id')} 缺少条件 source/field")
    # 环检测（Kahn）
    order = topological_order(nodes, edges)
    if order.get("cycle"):
        errors.append("检测到环路，工作流必须是有向无环图（DAG）")
    return {"valid": not errors, "errors": errors}


def topological_order(nodes: list[dict], edges: list[dict]) -> dict:
    """Kahn 拓扑排序。返回 {order:[id...], cycle:bool}。"""
    idx = _node_index(nodes)
    indeg: dict[str, int] = {i: 0 for i in idx}
    adj: dict[str, list[str]] = {i: [] for i in idx}
    for e in edges or []:
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a in indeg and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    cycle = len(order) != len(idx)
    return {"order": order, "cycle": cycle}


def layerize(nodes: list[dict], edges: list[dict]) -> list[list[str]]:
    """按依赖深度分层：同一层内节点互相无依赖，可并行执行。

    返回 [[id,...],...]，第 0 层为无依赖起点；成环时只返回可达部分，不抛错。
    """
    idx = _node_index(nodes)
    depth = {i: 0 for i in idx}
    indeg = {i: 0 for i in idx}
    adj: dict[str, list[str]] = {i: [] for i in idx}
    for e in edges or []:
        a, b = str(e.get("from", "")), str(e.get("to", ""))
        if a in indeg and b in indeg:
            adj[a].append(b)
            indeg[b] += 1
    topo = topological_order(nodes, edges)
    for n in topo["order"]:
        for m in adj[n]:
            depth[m] = max(depth[m], depth[n] + 1)
    layers: dict[int, list[str]] = {}
    for i, d in depth.items():
        layers.setdefault(d, []).append(i)
    return [layers[k] for k in sorted(layers)]


def plan_run(nodes: list[dict], edges: list[dict]) -> dict:
    """运行规划：拓扑序 + 每个节点归类 auto / need_approval / unknown_action。

    不执行动作，只给出执行计划；危险节点进入 needs_approval，不进入 auto 队列。
    """
    v = validate_graph(nodes, edges)
    if not v["valid"]:
        return {"ok": False, "errors": v["errors"], "auto": [], "needs_approval": [], "unknown": [], "control": []}
    topo = topological_order(nodes, edges)
    idx = _node_index(nodes)
    auto: list[dict] = []
    needs_approval: list[dict] = []
    unknown: list[dict] = []
    control: list[dict] = []
    for nid in topo["order"]:
        n = idx[nid]
        action = str(n.get("action", ""))
        entry = {"id": nid, "action": action, "label": n.get("label") or action,
                 "config": n.get("config") or {}}
        if action in SAFE_ACTIONS:
            auto.append(entry)
        elif action in NEED_APPROVAL:
            entry["reason"] = NEED_APPROVAL[action]
            needs_approval.append(entry)
        elif action in CONTROL_NODES:
            control.append(entry)
        else:
            unknown.append(entry)
    return {"ok": True, "errors": [], "order": topo["order"],
            "auto": auto, "needs_approval": needs_approval,
            "unknown": unknown, "control": control}


# ===== 控制节点：条件分支 / 并行网关 / 汇合（不执行业务动作，只做路由）=====
CONTROL_NODES: dict[str, str] = {
    "branch": "条件分支",
    "parallel": "并行网关",
    "merge": "汇合",
}

_OPS = {
    ">": lambda a, b: _num(a) > _num(b),
    ">=": lambda a, b: _num(a) >= _num(b),
    "<": lambda a, b: _num(a) < _num(b),
    "<=": lambda a, b: _num(a) <= _num(b),
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "contains": lambda a, b: str(b) in str(a),
    "empty": lambda a, b: a in (None, "", [], {}),
    "nonempty": lambda a, b: a not in (None, "", [], {}),
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def eval_condition(cond: dict, context: dict) -> bool:
    """对分支条件求值。cond={source, field, op, value}；context={nodeId: snapshot}。

    source 指向某上游节点快照，field 取该快照字段，与 value 按 op 比较。
    取不到上游快照时安全返回 False（走 false 分支），绝不抛错。
    """
    cond = cond or {}
    src = context.get(cond.get("source"), {}) or {}
    actual = src.get(cond.get("field"))
    op = cond.get("op", "==")
    fn = _OPS.get(op)
    if fn is None:
        return False
    try:
        return bool(fn(actual, cond.get("value")))
    except (TypeError, ValueError):
        return False


def route_branch(cond: dict, context: dict) -> str:
    """返回分支走向：'true' 或 'false'。"""
    return "true" if eval_condition(cond, context) else "false"


def build_context(results: list[dict]) -> dict:
    """把已执行节点结果汇总成 {nodeId: snapshot}，供下游分支判断与参数引用。"""
    ctx: dict = {}
    for r in results or []:
        ctx[r.get("id")] = r.get("snapshot") or {}
    return ctx


def resolve_inputs(config: dict, context: dict) -> dict:
    """按 config.inputs={本地键: "上游节点.字段"} 从上下文取值，实现节点间参数传递。

    取不到的键给 None，不报错；不暴露未授权节点以外的数据。
    """
    inputs = (config or {}).get("inputs") or {}
    out: dict = {}
    for local_key, ref in inputs.items():
        if not isinstance(ref, str) or "." not in ref:
            out[local_key] = None
            continue
        src, _, field = ref.partition(".")
        out[local_key] = (context.get(src) or {}).get(field)
    return out


def pruned_by_branch(nodes: list[dict], edges: list[dict], decisions: dict) -> set:
    """按分支走向裁剪：给定 {branchId: "true"|"false"}，返回被未选分支屏蔽的节点 id 集合。

    从每个 branch 沿 label 与所选相反的出边 BFS，收集其全部下游；
    未标 label 的边视为无条件直通，不参与裁剪。branch 自身不会被屏蔽。
    """
    blocked: set = set()
    adj: dict = {}
    for e in edges or []:
        adj.setdefault(str(e.get("from")), []).append(e)
    for bid, taken in (decisions or {}).items():
        stack = [str(bid)]
        seen: set = set()
        while stack:
            cur = stack.pop()
            for e in adj.get(cur, []):
                lab = e.get("label")
                if lab and lab != taken:
                    nxt = e.get("to")
                    if nxt and nxt not in seen:
                        seen.add(nxt)
                        blocked.add(nxt)
                        stack.append(nxt)
    return blocked

