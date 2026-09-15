# -*- coding: utf-8 -*-
"""总控台上线决策看板离线验收脚本。

不联网、不占端口，使用 Flask test_client 验证 /api/console/summary 的关键字段。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["SNACK_LLM_PROVIDER"] = "demo-fixture"
sys.path.insert(0, ".")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


def json_of(resp):
    try:
        return json.loads(resp.get_data(as_text=True))
    except Exception:
        return {}


def main():
    from app import app

    c = app.test_client()
    j = json_of(c.get("/api/console/summary"))
    check("summary 返回 ok", j.get("ok") is True, j.get("error", ""))
    check("包含上线决策", (j.get("decision") or {}).get("state") in ("ready", "review", "blocked"))
    check("包含 Provider 状态", bool((j.get("provider") or {}).get("provider")))
    check("包含目录健康", isinstance((j.get("catalogQuality") or {}).get("issues"), list))
    check("包含评测摘要", "latest" in (j.get("eval") or {}))
    check("包含运营指标", isinstance(((j.get("ops") or {}).get("metrics") or {}), dict))
    check("包含下一步动作", len(j.get("nextActions") or []) >= 1)

    fails = [x for x in RESULTS if not x[1]]
    print(f"\n== 结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过 ==")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
