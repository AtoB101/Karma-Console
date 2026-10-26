"""Generalist capability layer: sandboxed compute and workspace data files."""
import base64
import json


def _gw(client, headers, tool, params):
    return client.post("/gateway/execute", json={"tool": tool, "params": params}, headers=headers).json()


def test_run_code_computes_json(client, workspace):
    r = _gw(client, workspace["headers"], "run_code", {
        "code": "print(json.dumps({'total': sum(__input__['nums']), 'avg': statistics.mean(__input__['nums'])}))",
        "inputs": {"nums": [1, 2, 3, 4]},
    })
    assert r["decision"] == "ALLOWED"
    assert r["result"]["json"]["total"] == 10
    assert r["result"]["json"]["avg"] == 2.5


def test_run_code_sandbox_blocks_file_and_import(client, workspace):
    r1 = _gw(client, workspace["headers"], "run_code", {"code": "print(open('x.txt').read())"})
    assert "open" in (r1["result"].get("stderr") or "")
    r2 = _gw(client, workspace["headers"], "run_code", {"code": "import os; print(os.getcwd())"})
    assert "not allowed" in (r2["result"].get("stderr") or "")


def test_upload_read_and_report(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    csv_text = "name,price\n衬衫,99\n牛仔裤,159\n"
    up = client.post(
        f"/workspaces/{ws}/files",
        json={"name": "products.csv", "content_base64": base64.b64encode(csv_text.encode()).decode()},
        headers=h,
    )
    assert up.status_code == 201

    read = client.get(f"/workspaces/{ws}/files/products.csv", headers=h)
    assert read.status_code == 200
    body = read.json()
    assert body["kind"] == "csv"
    assert body["row_count"] == 2
    assert body["rows"][0]["name"] == "衬衫"

    report = _gw(client, h, "write_report", {"file_name": "summary.json", "content": {"total": 258}})
    assert report["decision"] == "ALLOWED"
    assert report["result"]["file_name"] == "summary.json"
    back = client.get(f"/workspaces/{ws}/files/summary.json", headers=h).json()
    assert back["data"]["total"] == 258

    # 文件库列表：只列本工作区直属文件，含已上传与报告文件
    lst = client.get(f"/workspaces/{ws}/files", headers=h)
    assert lst.status_code == 200
    names = {f["file_name"] for f in lst.json()["files"]}
    assert {"products.csv", "summary.json"} <= names


def test_generalist_tools_listed_in_catalog(client, workspace):
    tools = client.get(f"/workspaces/{workspace['workspace_id']}/tools", headers=workspace["headers"]).json()["tools"]
    names = {t["name"] for t in tools}
    assert {"run_code", "read_data", "write_report", "summarize_data"} <= names


def test_summarize_data_groups_and_writes(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    csv_text = "date,category,amount\n2026-10-01,服饰,1200\n2026-10-02,食品,800\n2026-10-03,服饰,1500\n"
    client.post(
        f"/workspaces/{ws}/files",
        json={"name": "sales.csv", "content_base64": base64.b64encode(csv_text.encode()).decode()},
        headers=h,
    )
    r = _gw(client, h, "summarize_data", {"file_name": "sales.csv", "group_by": "category", "value_col": "amount", "agg": "sum", "output_file": "sales_summary.json"})
    assert r["decision"] == "ALLOWED"
    assert r["result"]["by_group"]["服饰"] == 2700.0
    assert r["result"]["by_group"]["食品"] == 800.0
    assert r["result"]["total"] == 3500.0
    assert r["result"]["written"]["file_name"] == "sales_summary.json"
