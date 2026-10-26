"""T10: live model business regression (DeepSeek, OpenAI-compatible).

Skipped when DEEPSEEK_KEY is not set, so the offline suite stays green in CI.
Run the full battery with:  DEEPSEEK_KEY=... python e2e_scenarios.py
"""
import os

import pytest


@pytest.mark.skipif(not os.getenv("DEEPSEEK_KEY"), reason="DEEPSEEK_KEY not set; live regression is opt-in")
def test_live_deepseek_business_chain():
    from e2e_scenarios import run

    assert run() == 0
