"""Karma Console 本机一键自检 CLI（纯本地、不联网、不写业务数据）。

检查逻辑在 ``app/ops_check.py``（与只读端点共用），本脚本只负责打印与退出码。

用法：
    python scripts/selfcheck.py            # 人读输出，全部通过退出码 0
    python scripts/selfcheck.py --json      # 机器可读 JSON，便于脚本/开机项调用

退出码：0=无 FAIL；1=存在 FAIL。外部渠道凭证缺失只记 WARN，不阻断启动，
系统绝不伪造"已连通"。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ops_check import run_checks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run_checks(ROOT)
    summary = report["summary"]
    checks = report["checks"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        icon = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
        for c in checks:
            print(f"{icon[c['level']]:7} {c['name']} — {c['detail']}")
        print("-" * 48)
        print(f"通过 {summary['pass']} · 警告 {summary['warn']} · 失败 {summary['fail']}")

    return 0 if summary["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
