"""True multi-agent collaboration for compound business instructions.

One instruction is decomposed by an orchestrator into role-scoped subtasks.
Each subtask is handled by a worker agent that only proposes tools; every tool
still flows through the unified gateway (permission -> policy -> approval ->
execution -> verification -> audit). A deterministic verifier then checks the
executed results and a reporter composes the owner-facing summary.

Worker planning is I/O bound (model inference), so all worker plans are
produced concurrently and then executed sequentially in dependency order.
Concurrent workers that propose the same mutation target are de-duplicated by
a deterministic conflict detector instead of double-applying the side effect.
Long-horizon context (recent completed tasks) is injected into the
orchestrator so multi-step sessions stay coherent.

This is the Phase 4 "multi-agent collaboration" layer. It is opt-in: the chat
entry point only routes here when the workspace model is configured and the
instruction looks compound; otherwise the existing single-agent loop runs.
On any error the layer returns None so the caller can fall back safely.
"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .security import Actor

MAX_SUBTASKS = 5
MAX_WORKER_STEPS = 8
MAX_HISTORY_TASKS = 6

# Deterministic target keys used to resolve conflicts between concurrent
# workers that both try to mutate the same business object in one run.
_TARGET_PARAMS: dict[str, tuple[str, ...]] = {
    "create_product_draft": ("name",),
    "create_product": ("name",),
    "add_stock": ("product_id", "product_name"),
    "adjust_price": ("product_id", "product_name"),
    "create_order": ("customer_id", "customer_name"),
    "fulfill_order": ("order_id",),
    "mark_delivered": ("order_id",),
    "cancel_order": ("order_id",),
    "upsert_customer": ("external_id", "name"),
    "quote_product": ("product_id", "product_name"),
    "make_payment": ("order_id",),
    "refund_payment": ("order_id",),
    "add_layout_area": ("name",),
    "create_product_category": ("name",),
    "set_commerce_policy": ("currency",),
}


def _model_config(db: Session, actor: Actor):
    from .llm import workspace_model_config

    return workspace_model_config(db, actor.workspace_id)


def _chat(db: Session, actor: Actor, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 1600) -> str | None:
    from .llm import chat

    config = _model_config(db, actor)
    if not config:
        return None
    try:
        return chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model_config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
    except Exception:  # noqa: BLE001 - orchestration is best-effort
        return None


def _parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def is_compound(message: str) -> bool:
    """Cheap, conservative heuristic for a multi-task instruction."""
    connectors = ("并", "同时", "然后", "再", "还要", "加上", "以及", "另外", "顺便", "接着", "随后")
    if any(connector in message for connector in connectors):
        return True
    verbs = ("上架", "报价", "接单", "发货", "生成", "建", "添加", "查询", "统计", "回复", "跟进", "优化", "导入", "支付", "签收")
    return sum(1 for verb in verbs if verb in message) >= 2


def _recent_history(db: Session, actor: Actor) -> str:
    """Compact long-horizon context: recent completed tasks for this workspace."""
    from .models import Task, TaskStatus

    rows = db.execute(
        select(Task)
        .where(Task.workspace_id == actor.workspace_id, Task.status == TaskStatus.COMPLETED.value)
        .order_by(Task.created_at.desc())
        .limit(MAX_HISTORY_TASKS)
    ).scalars().all()
    lines = []
    for task in rows:
        intent = getattr(task, "intent", None)
        goal = ""
        if intent is not None:
            goal = getattr(intent, "goal", "") or ""
        plan = ",".join(str(p) for p in (task.plan or [])) or "无"
        lines.append(f"- [{goal or '任务'}] 执行工具：{plan}")
    return "\n".join(lines) if lines else "（暂无历史任务）"


async def _orchestrate(db: Session, actor: Actor, message: str) -> dict:
    from .chat import _business_context, _roles_context

    system = (
        "你是商家 Agent 的编排器（orchestrator）。把主人的复合指令拆成按顺序执行的子任务，"
        "每个子任务指定一个团队角色（role）和一句明确指令（instruction），并给出给主人的最终汇总（summary）。"
        "只输出 JSON，不要解释或 markdown。"
    )
    user = (
        "团队角色：\n" + _roles_context(db, actor) + "\n\n"
        "当前业务上下文：\n" + _business_context(db, actor) + "\n\n"
        "近期已完成任务（长时记忆）：\n" + _recent_history(db, actor) + "\n\n"
        "输出格式：{\"subtasks\":[{\"id\":\"简短唯一标识\",\"role\":\"store_manager\",\"instruction\":\"...\",\"depends_on\":[\"前序id\"]}],\"summary\":\"给主人的一句话汇总\"}\n"
        "规则：简单任务返回空 subtasks；最多 " + str(MAX_SUBTASKS) + " 个子任务；子任务必须按执行先后排序；"
        "仅当某子任务必须使用前序子任务的产出时，才给它配 id 和 depends_on（引用前序 id），无依赖则省略 id/depends_on；"
        "涉及资金/不可逆动作的，让相应角色只提出工具步骤，安全网关会审批。\n\n"
        "用户指令：" + message
    )
    plan = _parse_json(_chat(db, actor, system, user, temperature=0.1, max_tokens=1200))
    if not plan:
        return {"subtasks": [], "summary": ""}
    subtasks = plan.get("subtasks") or []
    if not isinstance(subtasks, list):
        subtasks = []
    return {"subtasks": subtasks[:MAX_SUBTASKS], "summary": str(plan.get("summary") or "")}


def _find_workspace_agent(db: Session, actor: Actor, key: str):
    """Match an owner-configured manager by id, name, or role id."""
    from .models import AIAgent

    agents = db.execute(
        select(AIAgent).where(
            AIAgent.workspace_id == actor.workspace_id,
            AIAgent.status.in_(["live", "test"]),
        )
    ).scalars().all()
    for agent in agents:
        if key in (agent.id, agent.name, agent.role):
            return agent
    return None


async def _plan_worker(db: Session, actor: Actor, subtask: dict, message: str) -> dict:
    """LLM-only worker planning. No DB writes, so plans can be gathered concurrently."""
    from .chat import _business_context, _tool_catalog
    from .roles import catalog

    role_id = subtask.get("role") or "store_manager"
    instruction = str(subtask.get("instruction") or message)
    role = next((r for r in catalog() if r["id"] == role_id), catalog()[0])
    agent = _find_workspace_agent(db, actor, role_id)

    role_name = agent.name if agent is not None else role["name"]
    mission = (agent.description or role["mission"]) if agent is not None else role["mission"]
    role_tools = agent.allowed_tools if (agent is not None and agent.allowed_tools) else role["tools"]

    system = (
        f"你是商家 Agent 团队角色「{role_name}」（{role_id}）。"
        "只提出工具步骤建议，是否允许执行由统一安全网关决定；永远不能绕过审批、代主人输入确认码。"
        "只输出一个 JSON 对象：{\"steps\":[{\"tool\":\"工具名\",\"params\":{...}}],\"reply\":\"一句话\",\"done\":true}"
    )
    tool_line = _tool_catalog(db, actor)
    if agent is not None and agent.allowed_tools:
        tool_line = "\n".join(
            line for line in tool_line.split("\n")
            if line.startswith("- ") and any(line.lstrip("- ").startswith(t) for t in agent.allowed_tools)
        ) or tool_line
    user = (
        f"角色使命：{mission}\n"
        f"本角色允许工具：{', '.join(role_tools)}\n\n"
        "可用工具及参数：\n" + tool_line + "\n\n"
        "当前业务上下文：\n" + _business_context(db, actor) + "\n\n"
        "子任务指令：" + instruction
    )
    plan = _parse_json(_chat(db, actor, system, user, temperature=0.2, max_tokens=1400))
    steps = (plan or {}).get("steps") or []
    if not isinstance(steps, list):
        steps = []
    return {
        "role": role_id,
        "name": role_name,
        "instruction": instruction,
        "reply": (plan or {}).get("reply") or "",
        "steps": steps[:MAX_WORKER_STEPS],
        "allowed_tools": agent.allowed_tools if (agent is not None and agent.allowed_tools) else None,
        # Optional M04 dependency contract produced by the orchestrator; when
        # absent the run falls back to plain array order (unchanged behavior).
        "id": subtask.get("id") or None,
        "depends_on": subtask.get("depends_on") or [],
    }


def _target_key(tool: str, params: dict) -> str | None:
    """Return a stable conflict key for mutating tools, else None (no check)."""
    keys = _TARGET_PARAMS.get(tool)
    if not keys:
        return None
    value = next((params.get(k) for k in keys if params.get(k)), None)
    if value is None:
        return None
    return f"{tool}|{value}"


async def _execute_plan(
    db: Session,
    actor: Actor,
    plan: dict,
    message: str,
    carry: dict,
    step_index_base: int,
    executed_targets: set,
) -> dict:
    from .chat import _PARAM_CARRY, _RESULT_CARRY, _allowed_tools, _execute

    allowed = _allowed_tools(db, actor)
    agent_allow = plan.get("allowed_tools")
    if agent_allow:
        allowed = allowed & set(agent_allow)
    actions: list[dict] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        if not tool:
            continue
        if tool not in allowed:
            actions.append({"tool": tool, "params": params, "decision": "DENIED", "result": {"error": f"unknown or not-allowed tool: {tool}"}})
            continue
        for param_key, carry_key in _PARAM_CARRY.items():
            if not params.get(param_key) and carry.get(carry_key):
                params[param_key] = carry[carry_key]

        target = _target_key(tool, params)
        if target is not None and target in executed_targets:
            actions.append({
                "tool": tool,
                "params": params,
                "decision": "CONFLICT",
                "result": {"error": f"duplicate mutation target skipped: {target}", "skipped": True},
            })
            continue

        try:
            res = await _execute(db, actor, tool, params, message, step_index_base + len(actions))
        except Exception as exc:  # noqa: BLE001 - never let a worker break the run
            actions.append({"tool": tool, "params": params, "decision": "DENIED", "result": {"error": str(exc)}})
            continue

        if target is not None and res.decision.value in ("ALLOWED", "PENDING"):
            executed_targets.add(target)
        if res.decision.value == "ALLOWED" and isinstance(res.result, dict):
            for result_key, carry_key in _RESULT_CARRY.items():
                if res.result.get(result_key):
                    carry[carry_key] = res.result[result_key]
        from . import nervous

        actions.append({
            "tool": tool,
            "params": params,
            "decision": res.decision.value,
            "result": res.result,
            **nervous.dispatch_meta(tool),
        })

    return {
        "role": plan["role"],
        "name": plan["name"],
        "instruction": plan["instruction"],
        "reply": plan["reply"],
        "actions": actions,
    }


def _verify(workers: list[dict]) -> dict:
    all_actions = [action for worker in workers for action in worker.get("actions", [])]
    denied = [a for a in all_actions if a.get("decision") == "DENIED"]
    pending = [a for a in all_actions if a.get("decision") == "PENDING"]
    conflicts = [a for a in all_actions if a.get("decision") == "CONFLICT"]
    errors = [a for a in all_actions if a.get("decision") == "ALLOWED" and isinstance(a.get("result"), dict) and a["result"].get("error")]
    unknown_receipts = [
        a for a in all_actions
        if isinstance(a.get("result"), dict) and a["result"].get("receipt_status") == "UNKNOWN"
    ]
    return {
        "all_actions": all_actions,
        "denied": denied,
        "pending": pending,
        "conflicts": conflicts,
        "errors": errors,
        "unknown_receipts": unknown_receipts,
        "healthy": not (denied or pending or errors or unknown_receipts),
    }


async def run_multiagent(db: Session, actor: Actor, message: str) -> dict | None:
    """Run the multi-agent orchestration; return None to fall back to single-agent."""
    from .chat import _record_task

    if not _model_config(db, actor):
        return None

    orchestration = await _orchestrate(db, actor, message)
    subtasks = orchestration.get("subtasks") or []
    if not subtasks:
        return None

    valid = [s for s in subtasks if isinstance(s, dict)]
    if not valid:
        return None

    # Plan every worker concurrently (model inference is I/O bound and the
    # sync prelude only reads from the DB, so this is safe on a shared session).
    try:
        plans = await asyncio.gather(*[_plan_worker(db, actor, subtask, message) for subtask in valid])
    except Exception:  # noqa: BLE001 - fall back to the proven single-agent loop
        return None

    carry: dict = {
        "last_area_id": None,
        "last_product_id": None,
        "last_order_id": None,
        "last_customer_id": None,
        "last_shipment_id": None,
        "last_provider_reference": None,
        "step_index": 0,
    }
    executed_targets: set = set()
    workers: list[dict] = []

    # Attach stable node ids and normalize the optional M04 dependency contract.
    enriched: list[dict] = []
    for i, plan in enumerate(plans):
        enriched.append({
            **plan,
            "id": plan.get("id") or f"w{i}",
            "depends_on": plan.get("depends_on") or [],
        })
    has_deps = any(e["depends_on"] for e in enriched)

    async def _run_one(plan: dict) -> dict:
        worker = await _execute_plan(db, actor, plan, message, carry, carry["step_index"], executed_targets)
        carry["step_index"] += len(worker["actions"])
        workers.append(worker)
        return worker

    try:
        if has_deps:
            from .collab import STATUS_OK, run_collaboration

            async def _collab_executor(node, prepared):
                await _run_one(prepared)
                # PENDING (awaiting owner approval) is not a hard failure; the
                # node finished its own steps, so dependents may proceed.
                return {"status": STATUS_OK, "result": {}, "actions": []}

            collab_out = await run_collaboration(enriched, _collab_executor)
            if not collab_out["ok"]:
                # Invalid DAG (cycle / unknown dependency) -> safe plain order.
                for plan in enriched:
                    await _run_one(plan)
        else:
            for plan in enriched:
                await _run_one(plan)
    except Exception:  # noqa: BLE001 - fall back to the proven single-agent loop
        return None

    if not workers:
        return None

    verification = _verify(workers)
    all_actions = verification["all_actions"]

    summary = orchestration.get("summary") or "；".join(w.get("reply") for w in workers if w.get("reply"))
    reply = summary or "已完成。"

    if not verification["healthy"]:
        lines = ["已完成可自动执行的部分，以下需要你确认："]
        if verification["pending"]:
            lines.append(f"- 待审批：{', '.join(a['tool'] for a in verification['pending'])}（已进待办，确认码邮件发送）")
        if verification["denied"]:
            lines.append(f"- 已拒绝：{', '.join(a['tool'] for a in verification['denied'])}（工具不存在或被安全策略拦截）")
        if verification["errors"]:
            lines.append(f"- 执行失败：{', '.join(a['tool'] for a in verification['errors'])}")
        if verification["unknown_receipts"]:
            lines.append(f"- 回执未知：{', '.join(a['tool'] for a in verification['unknown_receipts'])}（已标记待核验）")
        if verification["conflicts"]:
            lines.append(f"- 冲突已自动化解：{len(verification['conflicts'])} 个重复动作被跳过")
        reply = reply + "\n\n" + "\n".join(lines)

    result = {
        "reply": reply,
        "intent": "multiagent",
        "plan_source": "multiagent",
        "actions": all_actions,
        "plan": [a["tool"] for a in all_actions],
        "iterations": 1,
        "conflicts_resolved": len(verification["conflicts"]),
        "subtasks": [{"role": w["role"], "name": w["name"], "actions": [a["tool"] for a in w["actions"]]} for w in workers],
    }
    _record_task(db, actor, message, result)
    return result
