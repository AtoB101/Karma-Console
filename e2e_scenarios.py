"""A-phase live acceptance battery: run real business instructions through /chat.

Requires a DeepSeek API key (OpenAI-compatible). Uses a throwaway SQLite DB so
the developer DB is never touched. Each scenario asserts an observable effect.
"""
import os
import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "")

if not DEEPSEEK_KEY:
    print("请设置环境变量 DEEPSEEK_KEY 后再运行本脚本", file=sys.stderr)
    raise SystemExit(2)

_tmp = tempfile.mkdtemp(prefix="krama_scenarios_")
os.environ["DATABASE_URL"] = f"sqlite:///{pathlib.Path(_tmp) / 'scenarios.db'}"
os.environ["ENABLE_BACKGROUND_SCHEDULER"] = "0"
os.environ["DEFAULT_MODEL_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def run():
    results = []
    with TestClient(app) as c:
        r = c.post("/users", json={"email": "owner@example.com", "display_name": "Owner"})
        uid = r.json()["id"]
        r = c.post(f"/users/{uid}/api-keys")
        apikey = r.json()["api_key"]
        h = {"Authorization": f"Bearer {apikey}"}
        r = c.post("/workspaces", json={"name": "服装店", "region": "CN", "business_category": "clothing"}, headers=h)
        ws = r.json()["id"]
        h["X-Workspace-Id"] = ws
        c.put(f"/workspaces/{ws}/settings", json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": DEEPSEEK_KEY}}, headers=h)

        def chat(msg):
            return c.post(f"/workspaces/{ws}/chat", json={"message": msg}, headers=h).json()

        def products():
            return c.get(f"/workspaces/{ws}/products", headers=h).json()["products"]

        def orders():
            return c.get(f"/workspaces/{ws}/orders", headers=h).json()["orders"]

        def customers():
            return c.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]

        # 1 商家画像 onboarding
        b = chat("我是一家川菜馆的老板，你能帮我做什么")
        dash = c.get(f"/workspaces/{ws}/dashboard", headers=h).json()
        merchant = dash.get("merchant") or {}
        ok = bool(merchant.get("shop_name")) and merchant.get("category") in ("food", "general")
        results.append(("商家画像", ok, b.get("reply"), [], merchant))

        # 2 上架归类
        b = chat("在产品展示区加入 棉麻衬衫 99元、牛仔裤 159元")
        ps = products()
        ok = {p["name"] for p in ps} >= {"棉麻衬衫", "牛仔裤"} and all(p["layout_area_id"] for p in ps)
        results.append(("上架归类", ok, b.get("reply"), [a["tool"] for a in b.get("actions", [])], [p["name"] for p in ps]))

        # 3 加库存
        b = chat("给棉麻衬衫加 50 件库存")
        ps = products()
        shirt = next((p for p in ps if p["name"] == "棉麻衬衫"), {})
        results.append(("加库存", shirt.get("stock") == 50, b.get("reply"), [a["tool"] for a in b.get("actions", [])], {"stock": shirt.get("stock")}))

        # 4 开启自动接单
        c.put(f"/workspaces/{ws}/commerce", json={"auto_accept_orders": True, "approval_threshold_amount": 1000000}, headers=h)

        # 5 接单
        b = chat("帮我接一单：棉麻衬衫 2件，客户张三")
        os_ = orders()
        cs = customers()
        ok = len(os_) >= 1 and any(x.get("name") == "张三" for x in cs)
        results.append(("接单", ok, b.get("reply"), [a["tool"] for a in b.get("actions", [])], [{"total": o.get("total_amount")} for o in os_]))

        # 6 库存查询
        b = chat("现在库存是多少")
        results.append(("库存查询", b.get("plan_source") == "model", b.get("reply"), [a["tool"] for a in b.get("actions", [])], {}))

        # 7 报价给客户
        b = chat("客户李四问棉麻衬衫多少钱，帮我报价")
        ok = any(a["tool"] in ("quote_product", "introduce_product", "upsert_customer", "reply_customer") for a in b.get("actions", []))
        results.append(("报价", ok, b.get("reply"), [a["tool"] for a in b.get("actions", [])], {}))

        def shipments():
            return c.get(f"/workspaces/{ws}/shipments", headers=h).json().get("shipments", [])

        # 8 支付（R3 审批链路：先 PENDING，主人批准后执行）
        order0 = orders()[0]
        pay_key = f"e2e_pay_{order0['id']}"
        pay_amount = float(order0.get("total_amount") or 0)
        pr = c.post(
            "/gateway/execute",
            json={"tool": "make_payment", "params": {"amount": pay_amount, "currency": "CNY", "order_id": order0["id"]}, "idempotency_key": pay_key},
            headers=h,
        ).json()
        approval_id = pr.get("approval_id")
        ok = pr.get("decision") == "PENDING" and bool(approval_id)
        if approval_id:
            c.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)
            pr2 = c.post(
                "/gateway/execute",
                json={"tool": "make_payment", "params": {"amount": pay_amount, "currency": "CNY", "order_id": order0["id"]}, "idempotency_key": pay_key},
                headers=h,
            ).json()
            order_now = next((o for o in orders() if o["id"] == order0["id"]), {})
            ok = ok and pr2.get("decision") == "ALLOWED" and order_now.get("status") == "PAID"
        results.append(("支付审批", ok, f"decision={pr.get('decision')}", [pr.get("decision"), approval_id or ""], {"order_status": next((o['status'] for o in orders() if o['id'] == order0['id']), None)}))

        # 9 发货（扣库存 + 生成运单）
        b = chat(f"把订单 {order0['id']} 发货：承运商 顺丰，单号 SF1234567890")
        ship = shipments()
        order_now = next((o for o in orders() if o["id"] == order0["id"]), {})
        ok = any(a["tool"] == "fulfill_order" and a.get("decision") == "ALLOWED" for a in b.get("actions", [])) and bool(ship) and order_now.get("status") == "SHIPPED"
        results.append(("发货", ok, b.get("reply"), [a["tool"] for a in b.get("actions", [])], {"order_status": order_now.get("status"), "shipments": len(ship)}))

        # 10 确认收货
        b = chat(f"订单 {order0['id']} 已由顺丰确认送达，帮我确认收货并完成订单")
        order_now = next((o for o in orders() if o["id"] == order0["id"]), {})
        ship = shipments()
        ok = any(a["tool"] == "mark_delivered" and a.get("decision") == "ALLOWED" for a in b.get("actions", [])) and order_now.get("status") == "DELIVERED" and any(s.get("status") == "DELIVERED" for s in ship)
        results.append(("确认收货", ok, b.get("reply"), [a["tool"] for a in b.get("actions", [])], {"order_status": order_now.get("status"), "shipment_status": ship[0].get("status") if ship else None}))

        # 11 库存扣减（50 -> 48）
        ps = products()
        shirt = next((p for p in ps if p["name"] == "棉麻衬衫"), {})
        results.append(("库存扣减", shirt.get("stock") == 48, "", [], {"stock": shirt.get("stock")}))

    print("=== A-phase 场景验收 ===")
    passed = 0
    for name, ok, reply, actions, detail in results:
        passed += int(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print("      reply:", str(reply)[:140])
        print("      tools:", actions)
        print("      state:", detail)
    print("")
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(run())
