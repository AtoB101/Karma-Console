"""Karma Console 商业级一键回归（纯本地、不联网、不写业务库）。

阶段：
  1. AST 全量编译 app/**/*.py
  2. 核心纯函数断言：DAG 拓扑/环/白名单/分支/分层/参数、线索评分、客服路由、FAQ 命中
  3. 前端 home.html 内联脚本 node --check（找不到 node 记 WARN，不伪造）
  4. pytest tests/（环境装了 pytest 才跑，否则 WARN 跳过，不伪造通过）

用法：
    python scripts/regression.py            # 人读，有 FAIL 退出码 1
    python scripts/regression.py --json     # 机器可读
退出码：0=无 FAIL；1=存在 FAIL。WARN 不阻断。
"""
from __future__ import annotations

import ast
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, level: str = "PASS") -> None:
    CHECKS.append({"name": name, "level": "PASS" if ok else level, "detail": detail})


def check(name: str, fn) -> None:
    try:
        fn()
        record(name, True, "通过")
    except AssertionError as e:
        record(name, False, f"断言失败：{e}", "FAIL")
    except Exception as e:  # noqa: BLE001
        record(name, False, f"异常：{type(e).__name__}: {e}", "FAIL")


# ---------- 阶段 1：AST ----------
def stage_ast() -> None:
    files = glob.glob(str(ROOT / "app" / "**" / "*.py"), recursive=True)
    bad = []
    for f in files:
        try:
            ast.parse(pathlib.Path(f).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append(f"{os.path.relpath(f, ROOT)}: {e}")
    record("AST 全量编译", not bad, f"{len(files)} 个 py 文件" + ("" if not bad else "；" + "; ".join(bad[:5])))


# ---------- 阶段 2：核心纯函数 ----------
def t_dag_topology():
    from app import dag
    nodes = [{"id": "a", "action": "overview"}, {"id": "b", "action": "tickets"}]
    edges = [{"from": "a", "to": "b"}]
    p = dag.plan_run(nodes, edges)
    assert p["ok"], p["errors"]
    assert [x["id"] for x in p["auto"]] == ["a", "b"]
    cyc = dag.plan_run([{"id": "x", "action": "overview"}, {"id": "y", "action": "faqs"}],
                       [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}])
    assert not cyc["ok"], "成环必须被拒绝"


def t_dag_risk():
    from app import dag
    p = dag.plan_run([{"id": "r", "action": "make_refund"}], [])
    assert p["needs_approval"] and p["needs_approval"][0]["action"] == "make_refund"
    assert not p["auto"]


def t_dag_branch():
    from app import dag
    ctx = {"tickets": {"open": 3}}
    assert dag.eval_condition({"source": "tickets", "field": "open", "op": ">", "value": 0}, ctx)
    n = [{"id": "b", "action": "branch"}, {"id": "t1", "action": "tickets"}, {"id": "t2", "action": "faqs"}]
    e = [{"from": "b", "to": "t1", "label": "true"}, {"from": "b", "to": "t2", "label": "false"}]
    assert dag.pruned_by_branch(n, e, {"b": "true"}) == {"t2"}
    assert dag.pruned_by_branch(n, e, {"b": "false"}) == {"t1"}


def t_dag_layer_inputs():
    from app import dag
    n = [{"id": "s", "action": "overview"}, {"id": "p1", "action": "tickets"}, {"id": "p2", "action": "faqs"}]
    e = [{"from": "s", "to": "p1"}, {"from": "s", "to": "p2"}]
    L = dag.layerize(n, e)
    assert L[0] == ["s"] and set(L[1]) == {"p1", "p2"}, L
    ctx = dag.build_context([{"id": "s", "snapshot": {"open": 3}}])
    assert dag.resolve_inputs({"inputs": {"n": "s.open"}}, ctx) == {"n": 3}


def t_lead_score():
    from app.lead_score import score_customer
    cust = types.SimpleNamespace(name="王总", source="抖音", profile={"note":"已留资"})
    msgs = [{"content": "报价多少钱", "direction": "in"}, {"content": "有没有货能不能发货", "direction": "in"}]
    s = score_customer(cust, msgs)
    assert s.score >= 60, s
    cold = score_customer(types.SimpleNamespace(name="", source="", profile={}),
                          [{"content": "不需要别联系", "direction": "in"}])
    assert cold.grade == "C", cold


def t_cs_routing():
    from app.cs_routing import needs_human, match_faq
    assert needs_human("我要退款").need_human
    assert needs_human("你们这个怎么卖").need_human is False
    hit = match_faq("怎么发货运费多少", [{"question": "发货与运费", "answer": "48小时发货", "tags": ["发货", "运费"]}])
    assert hit.matched, hit


def t_cache_ttl():
    import time
    from app import cache
    cache.clear()
    cache.set("ws1:board", {"a": 1}, ttl=0.2)
    assert cache.get("ws1:board") == {"a": 1}
    assert cache.get("ws2:board") is None
    time.sleep(0.25)
    assert cache.get("ws1:board") is None
    cache.set("ws1:x", 1); cache.set("ws1:y", 2)
    assert cache.invalidate("ws1:") == 2
    cache.clear()


def t_playbook():
    from app import playbook
    allp = playbook.search_playbook("")
    assert len(allp) >= 8
    rf = playbook.search_playbook("退款")
    assert rf and rf[0]["owner"] == "boss" and rf[0]["escalate"] is True
    esc = playbook.must_escalate("我要投诉退款")
    assert any(p["pid"] == "refund_dispute" for p in esc)
    # 非敏感关键词不得误触发强制升级
    assert playbook.must_escalate("你们这个怎么卖") == []


def t_devops():
    from app import devops_agent as d
    s = d.assess_safety("给客户面板加一个导出按钮")
    assert s.forbidden_hits == [] and s.risk_level == "R1" and s.requires_manual
    s2 = d.assess_safety("pip install 新依赖，然后自动重启服务并上线")
    assert s2.forbidden_hits and s2.risk_level == "R3"
    dec = d.decompose_requirement("做一个库存补货提醒面板")
    assert dec["test_plan"] and dec["rollback"] and dec["domains"]
    assert d.can_approve("pass")[0] is True
    assert d.can_approve("fail")[0] is False
    assert d.can_approve("pending", waive=False)[0] is False
    assert d.can_approve("pending", waive=True, waive_note="老板豁免")[0] is True
    assert d.can_transition("draft", "proposed")
    assert d.can_transition("approved", "applied")
    assert not d.can_transition("applied", "approved")
    # 回归门槛汇总：fail 阻断、pass 就绪、pending 待补，存在 fail 整体门槛不过
    batch = [
        {"id": "a", "title": "通过单", "regression_status": "pass"},
        {"id": "b", "title": "失败单", "regression_status": "fail"},
        {"id": "c", "title": "未跑单", "regression_status": "pending"},
    ]
    summ = d.regression_gate_summary(batch)
    assert summ["counts"] == {"pass": 1, "fail": 1, "pending": 1}, summ
    assert summ["gate_ok"] is False
    assert [x["id"] for x in summ["blocked"]] == ["b"]
    assert [x["id"] for x in summ["ready"]] == ["a"]
    assert [x["id"] for x in summ["pending"]] == ["c"]
    clean = d.regression_gate_summary([{"id": "x", "regression_status": "pass"}])
    assert clean["gate_ok"] is True and clean["counts"]["pass"] == 1
    # 待批滞留：proposed 超阈值标 stale，新鲜单等待，已批准不计入
    from datetime import datetime as _dt, timedelta as _td
    old = _dt.utcnow() - _td(hours=30)
    fresh = _dt.utcnow()
    blk = d.stale_changes([
        {"id": "x", "status": "proposed", "submitted_at": old},
        {"id": "y", "status": "proposed", "submitted_at": fresh},
        {"id": "z", "status": "approved", "submitted_at": old},
    ], _dt.utcnow(), threshold_hours=24)
    assert blk["stale_count"] == 1, blk
    assert [i["id"] for i in blk["stale"]] == ["x"]
    assert [i["id"] for i in blk["waiting"]] == ["y"]
    # 系统工程面板必须登记为需老板拍板（不自动落地）
    from app.nervous import panel_owner
    pc = panel_owner("devops")
    assert pc["domain"] == "devops" and pc["escalate"] is True


# ---------- 阶段 3：前端语法 ----------
def stage_frontend() -> None:
    html = ROOT / "app" / "static" / "home.html"
    if not html.exists():
        record("前端脚本语法", False, "home.html 缺失", "FAIL")
        return
    import re
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html.read_text(encoding="utf-8"), re.S)
    node = shutil.which("node")
    if not node:
        record("前端脚本语法", True, "未找到 node，跳过 JS 语法检查（WARN）", "WARN")
        return
    tmp = ROOT / "_reg_check.js"
    tmp.write_text("\n;\n".join(blocks), encoding="utf-8")
    r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    record("前端脚本语法", r.returncode == 0, r.stderr.strip()[:300] or f"{len(blocks)} 段脚本 OK")


# ---------- 阶段 4：pytest ----------
def stage_pytest() -> None:
    try:
        import pytest  # noqa: F401
    except ImportError:
        record("pytest 套件", True, "环境未安装 pytest，跳过（建议 pip install pytest 后复跑）", "WARN")
        return
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q",
                        "--tb=line", "-p", "no:cacheprovider", "-p", "no:warnings"],
                        cwd=ROOT, capture_output=True, text=True)
    blob = (r.stdout or "") + (r.stderr or "")
    import re as _re
    m = _re.search(r"(\d+) passed", blob)
    summary = f"{m.group(1)} passed" if m else "退出码 0"
    record("pytest 套件", r.returncode == 0,
           summary if r.returncode == 0 else blob[-400:])


def main() -> int:
    stage_ast()
    check("DAG 拓扑/环", t_dag_topology)
    check("DAG 危险动作待拍板", t_dag_risk)
    check("DAG 条件分支/裁剪", t_dag_branch)
    check("DAG 并行分层/参数传递", t_dag_layer_inputs)
    check("线索评分分级", t_lead_score)
    check("客服路由/FAQ 命中", t_cs_routing)
    check("只读 TTL 缓存", t_cache_ttl)
    check("决策预案库/强制升级", t_playbook)
    check("受控开发Agent安全/门禁", t_devops)
    stage_frontend()
    stage_pytest()

    fails = [c for c in CHECKS if c["level"] == "FAIL"]
    warns = [c for c in CHECKS if c["level"] == "WARN"]
    if "--json" in sys.argv:
        print(json.dumps({"checks": CHECKS, "fail": len(fails), "warn": len(warns)}, ensure_ascii=False, indent=2))
    else:
        icon = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
        for c in CHECKS:
            print(f"{icon[c['level']]:7} {c['name']} — {c['detail']}")
        print("-" * 52)
        print(f"通过 {len(CHECKS)-len(fails)-len(warns)} · 警告 {len(warns)} · 失败 {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
