"""渠道统一执行骨架（skeleton）：为微信客服 / 抖音私信 / 呼叫中心定义统一调用接口。

安全与诚实约束：
- 本层**不发起任何网络请求**，不伪造成功回执。
- 凭证未配置 → ``unavailable``；凭证已填但方法尚未实现 → ``skeleton_pending``。
- 真实 HTTP 调用待凭证到位后，在各适配器方法内实现，结果以对端回执为准。
- 与 ``integrations.py`` 的声明层配合：声明层回答"还差什么凭证"，本层回答"该用哪个方法、现在能不能跑"。
"""
from __future__ import annotations

from . import integrations

# 渠道 key → 已声明的可执行方法（与 integrations.REGISTRY 中 methods 对应，真实实现待补）。
# value 为方法名列表；当前全部标记为未实现 skeleton，绝不返回成功。
_CHANNEL_METHODS: dict[str, list[str]] = {
    "wechat_cs": ["pull_messages", "send_reply", "assign_session", "sync_contact"],
    "douyin_im": ["poll_dm", "send_dm", "bind_lead", "assign_agent"],
    "call_center": ["place_call", "transcribe", "virtual_number", "route_to_human"],
}

# 真实实现占位：当前为空，待凭证到位后填入 HTTP 调用；保持空即未实现。
_IMPLEMENTED: dict[str, set[str]] = {
    "wechat_cs": set(),
    "douyin_im": set(),
    "call_center": set(),
}


def readiness(channel_key: str, env_overrides: dict | None = None) -> dict:
    """只读：某渠道凭证是否在位、方法是否已实现；不联网、不声称已连通。"""
    spec = next((s for s in integrations.REGISTRY if s["key"] == channel_key), None)
    if spec is None:
        return {"channel": channel_key, "status": "unknown_channel", "ready": False,
                "reason": "未登记的渠道 key"}
    probe = integrations.probe(spec, env_overrides)
    implemented = _IMPLEMENTED.get(channel_key, set())
    methods = [
        {"method": m, "implemented": m in implemented}
        for m in _CHANNEL_METHODS.get(channel_key, [])
    ]
    if not probe["configured"]:
        status = "unavailable"
        reason = "缺少必填凭证位，不可联调"
    elif not implemented:
        status = "skeleton_pending"
        reason = "凭证在位，但真实方法尚未实现，系统不会伪造连接成功"
    else:
        status = "ready_to_probe"
        reason = "可发起真实连通测试，真实状态以对端回执为准"
    return {
        "channel": channel_key,
        "label": spec["label"],
        "status": status,
        "ready": status == "ready_to_probe",
        "configured": probe["configured"],
        "missing": probe["missing"],
        "methods": methods,
        "reason": reason,
    }


def dispatch(channel_key: str, method: str, payload: dict | None = None,
             env_overrides: dict | None = None) -> dict:
    """统一派发入口（骨架）。未配置/未实现直接返回结构化拒绝，不联网。"""
    info = readiness(channel_key, env_overrides)
    if info["status"] == "unknown_channel":
        return {"ok": False, "status": "unknown_channel", "reason": info["reason"]}
    if info["status"] == "unavailable":
        return {"ok": False, "status": "unavailable", "reason": info["reason"],
                "missing": info["missing"]}
    method_names = {m["method"] for m in info["methods"]}
    if method not in method_names:
        return {"ok": False, "status": "unknown_method",
                "reason": f"渠道 {channel_key} 未声明方法 {method}"}
    if method not in _IMPLEMENTED.get(channel_key, set()):
        return {"ok": False, "status": "skeleton_pending",
                "reason": f"方法 {method} 真实实现待补（凭证到位后接入对端 HTTP），不伪造回执",
                "expected_payload": payload or {}}
    # 真实实现分支预留：凭证到位且方法实现后，在此调用对端并以回执为准。
    return {"ok": False, "status": "skeleton_pending", "reason": "实现分支未启用"}


def all_readiness(env_overrides: dict | None = None) -> dict:
    """汇总所有 support 渠道就绪状态（只读）。"""
    rows = [readiness(k, env_overrides)
            for k in ("wechat_cs", "douyin_im", "call_center")]
    return {
        "channels": rows,
        "ready_count": sum(1 for r in rows if r["ready"]),
        "total": len(rows),
    }
