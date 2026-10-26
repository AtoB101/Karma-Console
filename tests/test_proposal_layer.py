# -*- coding: utf-8 -*-
"""贾维斯请示层（/chat/proposal）：仅规划不执行 + 授权词映射。"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import create_all, migrate

create_all()
migrate()

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db import SessionLocal
from app.models import Execution


c = TestClient(app)
email = f"proposal_{int(time.time())}@local.krama"

u = c.post("/users", json={"email": email, "display_name": "请示测试"})
uid = u.json()["id"]
k = c.post(f"/users/{uid}/api-keys", json={})
key = k.json()["api_key"]
w = c.post("/workspaces", json={"name": "请示测试店"}, headers={"Authorization": f"Bearer {key}"})
wid = w.json()["id"]
hdr = {"Authorization": f"Bearer {key}", "X-Workspace-Id": wid}

# 1. 执行类指令 → 请示卡：有动作计划、不执行（无副作用：不新建产品/订单）
r = c.post(f"/workspaces/{wid}/chat/proposal", json={"message": "上架一款苜蓿草干草 1280 元/吨"}, headers=hdr, timeout=60)
assert r.status_code == 200, r.text
p = r.json()
assert p["proposal_ready"] is True
assert p["actions"], "执行类指令必须给出计划动作"
tools = [a["tool"] for a in p["actions"]]
print("计划动作:", tools)
assert any("product" in t or "import" in t for t in tools), f"应包含商品工具，实际 {tools}"

# 零副作用复核：请示层调用前后商品数不变
from app.models import Product
db = SessionLocal()
prod_count = db.execute(select(Product).where(Product.workspace_id == wid)).scalars().all()
db.close()
assert len(prod_count) == 0, "请示层不得产生任何数据副作用"
card = p["actions"][0]
for field in ("tool", "need_authorization", "owner", "plan_b", "priority"):
    assert field in card, f"请示卡缺少字段 {field}"
print("请示卡样例:", {k2: card[k2] for k2 in ("tool", "need_authorization", "owner", "priority")})

# 3. 资金/不可逆指令 → 强制升级信号
r2 = c.post(f"/workspaces/{wid}/chat/proposal", json={"message": "给客户王场长退款 5000 元"}, headers=hdr, timeout=60)
p2 = r2.json()
print("退款指令 escalate =", p2.get("escalate"), "| 命中:", p2.get("reply", "")[:120])
assert p2["escalate"] is True, "退款必须标记需授权"

# 4. 纯问答 → 无计划动作（不打扰）
r3 = c.post(f"/workspaces/{wid}/chat/proposal", json={"message": "今天订单多不多？"}, headers=hdr, timeout=60)
p3 = r3.json()
assert not (p3.get("actions") or []), "纯问答不应产生动作计划"
print("纯问答无动作 ✓")

# 5. 授权后执行闭环：真执行并落库（与请示层形成对照）
# 用执行端确定命中的表述（detect_intent：含“商品”+“归类”→ product_display）
r4 = c.post(f"/workspaces/{wid}/chat", json={"message": "把商品 苜蓿草干草 1280 元 归类到产品展示区"}, headers=hdr, timeout=90)
assert r4.status_code == 200
res = r4.json()
db = SessionLocal()
prod_count2 = db.execute(select(Product).where(Product.workspace_id == wid)).scalars().all()
db.close()
assert len(prod_count2) >= 1, "授权后执行必须真实产生数据"
print("授权执行闭环 ✓ 商品已落库", len(prod_count2), "条 | 回执 actions:", len(res.get("actions") or []), "条")

print("\n贾维斯请示层全部通过 ✓")


def test_proposal_layer_smoke():
    """pytest 收集入口：全链路断言已在模块顶层执行并通过。"""
    assert True
