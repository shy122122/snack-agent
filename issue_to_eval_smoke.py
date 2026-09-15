# -*- coding: utf-8 -*-
"""运营问题沉淀为评测用例的离线验收脚本。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["SNACK_LLM_PROVIDER"] = "demo-fixture"
sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOUCHED = ["runs.json", "eval_cases.json", "ops_ratings.json", "ops_improvements.json"]
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


def backup():
    return {n: (DATA / n).read_bytes() if (DATA / n).exists() else None for n in TOUCHED}


def restore(saved):
    for n, b in saved.items():
        p = DATA / n
        if b is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(b)


def json_of(resp):
    try:
        return json.loads(resp.get_data(as_text=True))
    except Exception:
        return {}


def main():
    saved = backup()
    try:
        from app import app
        from core import runner

        c = app.test_client()
        rec = runner.run_run("100元以内买坚果礼盒送人，帮我算一下到手价", source="smoke_issue")
        rid = rec.get("id")
        check("准备真实 Run", rec.get("status") == "ok" and bool(rid), f"runId={rid}")

        rt = json_of(c.post("/api/ratings", json={"runId": rid, "score": 2, "problemType": "价格金额",
                                                  "comment": "价格展示需要回归验证"})).get("rating") or {}
        check("准备低分评分", bool(rt.get("id")) and rt.get("badcase") is True)

        j = json_of(c.post("/api/eval/cases/from-issue", json={"sourceType": "rating", "sourceId": rt.get("id")}))
        case = j.get("case") or {}
        check("评分可沉淀为评测用例", j.get("ok") is True and bool(case.get("id")))
        check("用例绑定真实 Run", case.get("sourceRunId") == rid)
        check("用例带 ops_issue 标签", "ops_issue" in (case.get("tags") or []))
        check("价格问题默认进入价格核验维度", case.get("category") == "价格核验" and "price_honesty" in (case.get("evalDimension") or []))

        bad = json_of(c.post("/api/eval/cases/from-issue", json={"sourceType": "rating", "sourceId": "missing"}))
        check("不存在来源返回结构化错误", bad.get("ok") is False)
    finally:
        restore(saved)

    fails = [x for x in RESULTS if not x[1]]
    print(f"\n== 结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过 ==")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
