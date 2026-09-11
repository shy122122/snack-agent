# -*- coding: utf-8 -*-
"""4 个确定性 Tool：查询商品/活动/优惠券、用代码计算价格。
涉及金额、活动、优惠券一律在此用代码处理，绝不经由大模型推算。
"""
from __future__ import annotations

from datetime import date, datetime

from . import store


def _r2(x: float) -> float:
    return round(float(x), 2)


def _in_period(start, end, today: date):
    def _d(s):
        if not s:
            return None
        try:
            return datetime.strptime(str(s), "%Y-%m-%d").date()
        except ValueError:
            return None

    s, e = _d(start), _d(end)
    if s and today < s:
        return False
    if e and today > e:
        return False
    return True


# ------------------------------------------------------------------ query_products
def query_products(params, db):
    products = [p for p in db["products"] if p.get("status") == "在售"]
    keywords = params.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    category = params.get("category")
    max_price = params.get("max_price") or params.get("budget")
    limit = int(params.get("limit") or 8)

    def _hay(p):
        parts = [p.get("name", ""), p.get("category", "")] + list(p.get("tags", []) or [])
        return " ".join(str(x).lower() for x in parts)

    out = []
    for p in products:
        if category and p.get("category") != category:
            continue
        if max_price is not None and float(p.get("price", 0)) > float(max_price):
            continue
        if keywords:
            kw = [str(k).lower() for k in keywords if str(k).strip()]
            if kw and not any(k in _hay(p) for k in kw):
                continue
        out.append(p)
    out.sort(key=lambda p: (p.get("sales", 0)), reverse=True)
    return {"products": out[:limit], "count": len(out[:limit])}


# ------------------------------------------------------------------ query_activities
def query_activities(params, db):
    today = date.today()
    cat = params.get("category") or params.get("categories")
    out = []
    for a in db["activities"]:
        if not _in_period(a.get("start"), a.get("end"), today):
            continue
        if a.get("status") == "已结束":
            continue
        if cat:
            cats = a.get("categories") or []
            if a.get("scope") != "all" and cat not in cats:
                continue
        out.append(a)
    return {"activities": out, "count": len(out)}


# ------------------------------------------------------------------ query_coupons
def query_coupons(params, db):
    today = date.today()
    category = params.get("category")
    min_amount = params.get("min_amount")
    out = []
    for c in db["coupons"]:
        if not _in_period(c.get("start"), c.get("end"), today):
            continue
        if category and c.get("scope") != "all" and category not in (c.get("categories") or []):
            continue
        if min_amount is not None and float(c.get("condition", 0)) > float(min_amount):
            continue
        out.append(c)
    return {"coupons": out, "count": len(out)}


# ------------------------------------------------------------------ compute_price
def compute_price(params, db):
    """按 商品原价 → 满减/折扣/第二件半价活动 → 优惠券 顺序计算最终价。"""
    products = {p["id"]: p for p in db["products"]}
    activities = {a["id"]: a for a in db["activities"]}
    coupons = {c["id"]: c for c in db["coupons"]}

    raw_items = params.get("items") or []
    items, notes = [], []
    for it in raw_items:
        if isinstance(it, (list, tuple)) and len(it) == 2:
            pid, qty = it[0], it[1]
        elif isinstance(it, dict):
            pid = it.get("product_id") or it.get("id")
            qty = it.get("qty") or it.get("quantity") or 1
        else:
            continue
        p = products.get(pid)
        if not p:
            notes.append(f"商品 {pid} 不存在于真实商品库，已忽略")
            continue
        items.append({"product_id": p["id"], "name": p["name"], "category": p["category"],
                      "price": float(p.get("price", 0)), "qty": max(1, int(qty)),
                      "line_total": _r2(float(p.get("price", 0)) * max(1, int(qty)))})

    if not items:
        return {"error": "没有任何可计算价格的真实商品。", "notes": notes, "final_total": 0}

    subtotal = _r2(sum(i["line_total"] for i in items))
    today = date.today()

    # ---- 活动计算
    act_out = None
    activity_discount = 0.0
    after_activity = subtotal
    act_id = params.get("activity_id")
    if act_id:
        a = activities.get(act_id)
        if not a:
            notes.append(f"活动 {act_id} 不存在，未使用任何活动")
        else:
            a_type = a.get("type")
            in_scope = a.get("scope") == "all" or (
                a.get("scope") == "product" and any(i["product_id"] in (a.get("product_ids") or []) for i in items)
            ) or (a.get("scope") == "category" and any(i["category"] in (a.get("categories") or []) for i in items))
            if a_type == "满减":
                need = float(a.get("threshold") or 0)
                if subtotal >= need:
                    activity_discount = _r2(min(float(a.get("value") or 0), subtotal))
                else:
                    notes.append(f"「{a['name']}」未满 {need} 元门槛，暂未生效")
            elif a_type == "折扣":
                rate = float(a.get("value") or 1)
                eligible = [i for i in items if a.get("scope") == "all" or
                            i["product_id"] in (a.get("product_ids") or []) or
                            i["category"] in (a.get("categories") or [])]
                if eligible:
                    activity_discount = _r2(sum(i["line_total"] for i in eligible) * (1 - rate))
                else:
                    notes.append(f"「{a['name']}」与所选商品不匹配")
            elif a_type == "第二件半价":
                half = float(a.get("value") or 0.5)
                for i in items:
                    if a.get("scope") == "product" and i["product_id"] in (a.get("product_ids") or []):
                        activity_discount += i["price"] * (i["qty"] // 2) * half
                activity_discount = _r2(activity_discount)
                if activity_discount <= 0:
                    notes.append("同款数量不足 2 件，「第二件半价」暂未生效")
            else:
                notes.append(f"暂不支持的活动类型: {a_type}")
            if not in_scope and a_type == "折扣" and activity_discount == 0:
                notes.append(f"「{a['name']}」不适用所选商品")
            applied = activity_discount > 0 and _in_period(a.get("start"), a.get("end"), today)
            act_out = {"id": a["id"], "name": a["name"], "type": a_type, "applied": applied,
                       "discount": activity_discount, "note": (notes[-1] if notes and "暂未" in notes[-1] else ("已生效" if applied else "未生效"))}
            if not applied:
                activity_discount = 0.0
    after_activity = _r2(subtotal - activity_discount)

    # ---- 优惠券计算
    coupon_out = None
    coupon_discount = 0.0
    coupon_id = params.get("coupon_id")
    auto = False
    chosen = None
    if coupon_id:
        chosen = coupons.get(coupon_id)
        if not chosen:
            notes.append(f"优惠券 {coupon_id} 不存在，改为自动选券")
            coupon_id = None
    if not coupon_id:
        best, best_val = None, -1
        for c in db["coupons"]:
            if not _in_period(c.get("start"), c.get("end"), today):
                continue
            if float(c.get("condition", 0)) > after_activity:
                continue
            scope_ok = c.get("scope") == "all" or any(i["category"] in (c.get("categories") or []) for i in items)
            if not scope_ok:
                continue
            if float(c.get("value", 0)) > best_val:
                best, best_val = c, float(c.get("value", 0))
        if best:
            chosen, auto = best, True
    if chosen:
        cond = float(chosen.get("condition", 0))
        if after_activity >= cond:
            coupon_discount = _r2(min(float(chosen.get("value", 0)), after_activity))
            coupon_out = {"id": chosen["id"], "name": chosen["name"], "condition": cond, "applied": True,
                          "discount": coupon_discount, "auto": auto, "note": "已自动使用最优可用券" if auto else "已使用指定券"}
        else:
            coupon_out = {"id": chosen["id"], "name": chosen["name"], "condition": cond, "applied": False,
                          "discount": 0, "auto": auto, "note": f"未满足满 {cond} 元门槛，未用券"}
    total = _r2(max(after_activity - coupon_discount, 0))

    return {
        "currency": "¥",
        "subtotal": subtotal,
        "items": items,
        "activity": act_out,
        "after_activity": after_activity,
        "coupon": coupon_out,
        "final_total": total,
        "saved": _r2(activity_discount + coupon_discount),
        "notes": notes,
    }


# ------------------------------------------------------------------ query_service
def query_service(params, db):
    """查订单/物流/售后政策的真实数据（data/service.json）。订单物流类诉求必须经此查询。"""
    svc = db.get("service") or {}
    orders = list(svc.get("orders") or [])
    policies = list(svc.get("policies") or [])
    keyword = str(params.get("keyword") or params.get("order_id") or params.get("query") or "").strip().lower()
    kind = params.get("kind") or params.get("type") or ""
    o_ids = [str(o.get("order_id", "")).lower() for o in orders]

    if keyword:
        orders = [o for o in orders if keyword in " ".join(
            str(o.get(k, "")) for k in ("order_id", "products", "logistics", "status", "eta")).lower() or keyword in str(o.get("order_id", "")).lower()]
        policies = [p for p in policies if keyword in " ".join(str(v) for v in p.values()).lower()]
    if kind in ("order", "logistics", "ship"):
        policies = []
    elif kind in ("after_sale", "refund", "service", "policy"):
        orders = []

    return {
        "orders": orders,
        "policies": policies,
        "count_orders": len(orders),
        "count_policies": len(policies),
        "note": ("按关键词/订单号过滤" if keyword else "未指定关键词，返回全部订单与售后政策"),
    }


# ------------------------------------------------------------------ 注册表
META = [
    {
        "id": "query_products",
        "name": "查询商品 Tool",
        "description": "按关键词/品类/价格上限，从本地 products.json 查询真实在售零食商品（含真实价格/库存/销量）。涉及价格信息前必须调用。",
        "params": {"keywords": "关键词数组（如 ['坚果','辣条']）", "category": "品类", "max_price": "价格上限", "limit": "返回条数(默认8)"},
        "output_desc": "返回真实商品数组 products 与数量 count",
        "artifact": "products",
    },
    {
        "id": "query_activities",
        "name": "查询优惠活动 Tool",
        "description": "查询本地 activities.json 中当前生效的活动（满减/折扣/第二件半价等）。不可由大模型自行推断活动。",
        "params": {"category": "可选，只看该品类相关活动"},
        "output_desc": "返回真实生效活动数组 activities",
        "artifact": "activities",
    },
    {
        "id": "query_coupons",
        "name": "查询优惠券 Tool",
        "description": "查询本地 coupons.json 中当前可用优惠券（满减条件/面额/适用范围）。优惠券信息必须由此查询得到。",
        "params": {"category": "可选品类", "min_amount": "可选金额下限"},
        "output_desc": "返回真实可用优惠券数组 coupons",
        "artifact": "coupons",
    },
    {
        "id": "compute_price",
        "name": "计算价格 Tool",
        "description": "用代码精确计算 原价 → 满减/折扣/第二件半价活动 → 优惠券 → 最终应付金额，并给出逐项明细。所有金额必须经过此工具，禁止大模型口算。",
        "params": {"items": "[{'product_id':'Pxx','qty':1}]", "coupon_id": "可选真实券id", "activity_id": "可选真实活动id"},
        "output_desc": "返回 subtotal/after_activity/final_total/items 等完整算价明细",
        "artifact": "price",
    },
    {
        "id": "query_service",
        "name": "查询订单物流售后 Tool",
        "description": "查询本地 service.json 中真实的订单状态/物流/售后政策（退货/退款/配送时效等）。用户询问订单、物流、发货、售后、退货、退款时必须调用，禁止模型臆测时效与承诺。",
        "params": {"keyword": "订单号或关键词", "order_id": "订单号", "kind": "order|logistics|after_sale|refund"},
        "output_desc": "返回真实 orders（订单/物流）与 policies（售后政策）",
        "artifact": "service",
    },
]


def get_meta(tool_id):
    for m in META:
        if m["id"] == tool_id:
            return m
    return None


def sample_input(tool_id: str) -> dict:
    """从真实数据构造一份能跑出非空结果的示例入参（后台 Tool 测试/示例按钮用）。
    商品类取销量前若干真实在售商品；订单物流不指定关键词返回全部。"""
    db = store.load_database()
    products = [p for p in db.get("products", []) if p.get("status") == "在售"]
    if tool_id == "query_products":
        return {"keywords": [], "category": None, "limit": 5}
    if tool_id == "query_activities":
        return {"category": None}
    if tool_id == "query_coupons":
        return {}
    if tool_id == "compute_price":
        top = sorted(products, key=lambda p: p.get("sales", 0), reverse=True)[:2]
        items = [{"product_id": p["id"], "qty": 1} for p in top if p.get("id")]
        return {"items": items}
    if tool_id == "query_service":
        return {}
    return {}


RUNNERS = {
    "query_products": query_products,
    "query_activities": query_activities,
    "query_coupons": query_coupons,
    "compute_price": compute_price,
    "query_service": query_service,
}


def run_tool(tool_id: str, params: dict, db=None):
    db = db or store.load_database()
    fn = RUNNERS.get(tool_id)
    if not fn:
        raise KeyError(f"Tool 不存在: {tool_id}")
    if store.is_tool_forced(tool_id):
        raise RuntimeError(f"人为故障注入：Tool {tool_id} 被模拟为执行异常")
    return fn(params or {}, db)
