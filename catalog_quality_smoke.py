# -*- coding: utf-8 -*-
"""目录健康检查离线验收脚本。

通过临时替换 store.load_* 函数构造数据，不写入 data/ 下任何文件。
"""
from __future__ import annotations

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


def main():
    import app as app_mod
    from core import store

    old_products = store.load_products
    old_activities = store.load_activities
    old_coupons = store.load_coupons
    try:
        store.load_products = lambda: [
            {"id": "P_BAD", "name": "坏商品", "category": "坚果", "price": 0, "stock": 3, "status": "在售"},
            {"id": "P_BAD", "name": "重复商品", "category": "坚果", "price": 12, "stock": 0, "status": "在售"},
        ]
        store.load_activities = lambda: [
            {"id": "A_BAD", "name": "坏活动", "type": "满减", "threshold": 99, "value": 10,
             "scope": "product", "product_ids": ["P404"], "categories": [], "start": "2026-09-01", "end": "2026-08-01"},
        ]
        store.load_coupons = lambda: [
            {"id": "C_BAD", "name": "过期券", "type": "满减券", "condition": 30, "value": 5,
             "scope": "category", "categories": ["不存在品类"], "start": "2025-01-01", "end": "2025-01-31"},
        ]
        q = app_mod._catalog_quality()
    finally:
        store.load_products = old_products
        store.load_activities = old_activities
        store.load_coupons = old_coupons

    issues = q.get("issues") or []
    blob = "\n".join(x.get("message", "") for x in issues)
    check("健康检查识别为不可上线", q.get("ok") is False)
    check("识别重复 ID", "ID 重复" in blob)
    check("识别商品价格非法", "商品价格必须大于 0" in blob)
    check("识别活动引用不存在商品", "不存在的商品" in blob)
    check("识别活动日期顺序错误", "开始日期晚于结束日期" in blob)
    check("识别过期优惠券或无商品品类", ("优惠券已过期" in blob) or ("当前没有商品" in blob))
    check("每条问题带建议动作", all(x.get("action") for x in issues))

    fails = [x for x in RESULTS if not x[1]]
    print(f"\n== 结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过 ==")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
