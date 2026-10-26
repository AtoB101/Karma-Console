"""P14 deterministic memory relevance recall unit tests."""
from app.context_assembler import (
    assemble_relevant_facts,
    format_facts,
    rank_memories,
    trim_feedback,
)


class _M:
    def __init__(self, key, value):
        self.key = key
        self.value = value


def test_relevant_memory_ranked_first():
    mems = [
        _M("shipping", "默认全国包邮"),
        _M("discount", "最大折扣不超过10%，低于成本必须审批"),
        _M("cat", "店铺主营棉麻女装"),
    ]
    r = rank_memories(mems, "折扣能给多少", k=2)
    assert r[0].key == "discount"


def test_irrelevant_memory_excluded():
    mems = [
        _M("restock", "三个月前的补货盘点记录"),
        _M("pref", "客户偏好棉麻衬衫"),
    ]
    r = rank_memories(mems, "折扣政策是什么", k=5)
    assert all(m.key != "restock" for m in r)


def test_format_facts_header():
    out = format_facts([_M("k", "v")])
    assert "相关的持久记忆" in out and "k: v" in out


def test_assemble_filters_from_db(client, workspace):
    from app.db import SessionLocal
    from app.models import AgentMemory

    ws = workspace["workspace_id"]
    db = SessionLocal()
    db.add(AgentMemory(workspace_id=ws, key="pay_method", value="收款只接受微信支付", kind="preference"))
    db.commit()
    db.close()

    db = SessionLocal()
    try:
        rows = assemble_relevant_facts(db, ws, "收款方式怎么收", k=3)
    finally:
        db.close()
    assert any(m.key == "pay_method" for m in rows)


def test_trim_feedback_keeps_most_recent():
    lines = [f"step {i}" for i in range(12)]
    out = trim_feedback(lines, max_items=5)
    assert out == ["step 7", "step 8", "step 9", "step 10", "step 11"]


def test_trim_feedback_under_limit_unchanged():
    lines = ["a", "b"]
    assert trim_feedback(lines, max_items=8) == ["a", "b"]


def test_trim_feedback_zero_returns_empty():
    assert trim_feedback(["a", "b"], max_items=0) == []
