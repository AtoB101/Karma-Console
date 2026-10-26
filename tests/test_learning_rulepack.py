# -*- coding: utf-8 -*-
"""轨迹自学习 + 配置化规则包：真实闭环测试。"""
import pathlib
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import create_all, migrate

create_all()
migrate()

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db import SessionLocal
from app.models import Execution, Lesson
from app import rulepack


def step(name, fn):
    try:
        r = fn()
        print(f"[OK] {name}: {r.status_code} {str(r.json())[:200]}")
        return r
    except Exception:
        print(f"[FAIL] {name}")
        traceback.print_exc()
        return None


c = TestClient(app)
email = f"learn_{int(time.time())}@local.krama"

u = step("POST /users", lambda: c.post("/users", json={"email": email, "display_name": "学习测试"}))
uid = u.json()["id"]
k = step("POST api-keys", lambda: c.post(f"/users/{uid}/api-keys", json={}))
key = k.json()["api_key"]
w = step("POST /workspaces", lambda: c.post("/workspaces", json={"name": "学习测试店"}, headers={"Authorization": f"Bearer {key}"}))
wid = w.json()["id"]
hdr = {"Authorization": f"Bearer {key}", "X-Workspace-Id": wid}

# 1. 规则包：内置包应存在
rp = step("GET /rules (内置 core 包)", lambda: c.get("/rules", headers=hdr))
packs = rp.json()["packs"]
core = next((p for p in packs if p["id"] == "core"), None)
assert core and core["builtin"], "内置 core 规则包必须存在"
assert len(rp.json()["active_rules"]) >= 4, "内置激活规则应 ≥4 条"
print("  内置包:", core["name"], "| 生效规则", len(rp.json()["active_rules"]), "条")

# 2. 规则包：创建 user_ 包（写真实 yaml）
cr = step("POST /rules (user_ 自定义包)", lambda: c.post("/rules", json={
    "id": "user_alfalfa", "name": "苜蓿草报价规范",
    "description": "测试规则包", "rules": ["苜蓿草报价先查产品库", "无库存则引导留资"]}, headers=hdr))
assert cr.status_code == 200, "自定义规则包创建失败"
print("  落盘:", cr.json()["path"])

# 3. 规则包：防护——id 不合法/非 user_ 前缀应 400；停用后再启用
bad = step("POST /rules (非法id 应400)", lambda: c.post("/rules", json={"id": "core_x", "rules": ["x"]}, headers=hdr))
assert bad.status_code == 400, "非法 id 必须被拒绝"
tg = step("PUT /rules/user_alfalfa/active (停用)", lambda: c.put("/rules/user_alfalfa/active", json={"active": False}, headers=hdr))
assert tg.json()["active"] is False
rp2 = step("GET /rules (停用后生效规则减少)", lambda: c.get("/rules", headers=hdr))
assert all(p["id"] != "user_alfalfa" or p["active"] is False for p in rp2.json()["packs"])
tg2 = step("PUT /rules/user_alfalfa/active (重新启用)", lambda: c.put("/rules/user_alfalfa/active", json={"active": True}, headers=hdr))
assert tg2.json()["active"] is True

# 4. 轨迹自学习：先造执行记录（成功+失败），再 learn
db = SessionLocal()
for i, (tool, status, out) in enumerate([
    ("quote_product", "SUCCEEDED", {"ok": True}),
    ("quote_product", "SUCCEEDED", {"ok": True}),
    ("quote_product", "FAILED", {"error": "credential missing"}),
    ("fulfill_order", "FAILED", {"detail": "insufficient stock"}),
    ("reply_customer", "SUCCEEDED", {"ok": True}),
]):
    db.add(Execution(workspace_id=wid, task_id=f"t{i}", tool_name=tool,
                     input={}, output=out, status=status, risk_level="R0"))
db.commit()
db.close()

learn = step("POST lessons/learn", lambda: c.post(f"/workspaces/{wid}/lessons/learn", headers=hdr))
assert learn.json()["learned"] >= 2, "至少沉淀 2 条经验"
ls = step("GET lessons", lambda: c.get(f"/workspaces/{wid}/lessons", headers=hdr))
lessons = ls.json()["lessons"]
print(f"  沉淀 {len(lessons)} 条：")
for l in lessons:
    print(f"   - {l['pattern_key']} 成功率{int(l['confidence']*100)}% ok={l['success_count']} fail={l['fail_count']}")
    print(f"     {l['lesson'][:80]}")

# 5. 采纳 → 注入提示词生效；未采纳的不注入
target = next(l for l in lessons if l["pattern_key"].startswith("quote_product"))
ap = step("POST lessons/apply (采纳)", lambda: c.post(f"/workspaces/{wid}/lessons/{target['id']}/apply", headers=hdr))
assert ap.json()["applied"] is True
db = SessionLocal()
from app.learning import brief as learning_brief
b = learning_brief(db, wid)
db.close()
assert "quote_product" in b and "已由老板确认采纳" in b, "采纳后必须注入 brief"
print("  注入规划的经验片段:", b[:120], "...")

# 6. 丢弃 → 从注入移除
dp = step("POST lessons/dismiss (丢弃)", lambda: c.post(f"/workspaces/{wid}/lessons/{target['id']}/dismiss", headers=hdr))
assert dp.json()["deleted"] is True
db = SessionLocal()
b2 = learning_brief(db, wid)
db.close()
assert "quote_product" not in b2, "丢弃后不得再注入"

# 7. 清理规则包
dl = step("DELETE /rules/user_alfalfa", lambda: c.delete("/rules/user_alfalfa", headers=hdr))
assert dl.json()["deleted"] is True
bad_del = step("DELETE /rules/core (内置不可删 应403)", lambda: c.delete("/rules/core", headers=hdr))
assert bad_del.status_code == 403, "内置包删除必须被拒绝"
print("\n全部通过：规则包读写防护 ✓ 轨迹学习沉淀 ✓ 采纳注入 ✓ 丢弃移除 ✓")
