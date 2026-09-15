# -*- coding: utf-8 -*-
"""零食电商客服 Plan+Skill 可配置执行平台 —— Flask 入口。

运行： python app.py   （或在项目根目录双击 run.bat）
默认地址： http://127.0.0.1:8000/agent （前台）/ /admin （后台）
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

from core import engine, eval as eval_mod, eval_batch as eval_batch_mod, ops as ops_mod, providers, reset as reset_mod, runner, store, tools, validator as validator_mod  # noqa: E402

app = Flask(__name__, static_folder=None)

eval_mod.ensure_seed()  # 首次启动自动补种 9 个方向种子（幂等，不覆盖既有编辑）


def _fail(msg, code=200):
    return jsonify({"ok": False, "error": str(msg)}), code


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _run_json_payload(rec):
    """把 RunRecord 转成兼容旧前端的 JSON 形状（ok/plan/trace/final_reply/... 顶层可见）。"""
    steps = rec.get("steps") or []
    fr = rec.get("finalReply")
    return {
        "ok": rec.get("status") != "error",
        "status": rec.get("status"),
        "run": rec,
        "runId": rec.get("id"),
        "question": rec.get("question"),
        "plan": rec.get("plan"),
        "steps": steps,
        "trace": steps,
        "steps_count": len(steps),
        "final_reply": fr,
        "finalReply": fr,
        "risk": rec.get("risk"),
        "riskResult": rec.get("riskResult"),
        "moderation": rec.get("moderation"),
        "error": rec.get("error"),
        "blocked_reason": rec.get("blockedReason"),
        "provider": rec.get("provider"),
        "model": rec.get("model"),
        "cost_yuan": rec.get("costYuan", 0),
        "latency_ms": rec.get("latencyMs", 0),
        "usage": rec.get("usage"),
    }


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    # 工作台（原 /agent 前台改造而来）
    return send_from_directory(ROOT, "static/agent.html")


@app.get("/agent")
def agent_page():
    # 别名：/agent 与 / 指向同一工作台
    return send_from_directory(ROOT, "static/agent.html")


@app.get("/demo")
def demo_page():
    return send_from_directory(ROOT, "static/demo.html")


@app.get("/runs/<rid>")
def runs_page(rid):
    return send_from_directory(ROOT, "static/runs.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(ROOT, "static/admin.html")


@app.get("/skills")
def skills_page():
    return send_from_directory(ROOT, "static/skills.html")


@app.get("/tools")
def tools_page():
    return send_from_directory(ROOT, "static/tools.html")


@app.get("/planner")
def planner_page():
    return send_from_directory(ROOT, "static/planner.html")


@app.get("/models")
def models_page():
    return send_from_directory(ROOT, "static/models.html")


@app.get("/mgmt")
def mgmt_page():
    """四个管理页(Skill/Tool/Planner/模型)的合并视图：单页顶部页签切换，各页以 ?embed=1 嵌入。"""
    return send_from_directory(ROOT, "static/mgmt.html")


@app.get("/console")
def console_page():
    """总控台：工作台 / 演示 / 管理后台(技能·工具·规划器·模型) 合一，顶部按钮切换，各页以 ?embed=1 嵌入。"""
    return send_from_directory(ROOT, "static/console.html")


@app.get("/ops")
def ops_page():
    """运营中心：运行监控 / 服务评分 / 数据标注 / 改进建议中心 / 其他运营能力。"""
    return send_from_directory(ROOT, "static/ops.html")


@app.get("/eval")
def eval_page():
    """评测中心：评测集管理 + 单条测试(走真实主链路) + 结果展开 + 回归批跑。"""
    return send_from_directory(ROOT, "static/eval.html")


@app.get("/catalog")
def catalog_page():
    """商品目录：本地业务数据(商品/满减活动/优惠券)只读浏览，与 Agent Tool 同源。"""
    return send_from_directory(ROOT, "static/catalog.html")


@app.get("/static/<path:filename>")
def static_asset(filename):
    """共享前端资产（品牌样式等）。页面本身仍通过显式路由返回。"""
    return send_from_directory(ROOT / "static", filename)


# ---------------------------------------------------------------- 健康 / 配置
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "name": "零食电商客服 Agent 平台", "ts": store.now_iso()})


@app.get("/api/config")
def get_config():
    cfg = store.load_config()
    llm = cfg["llm"]
    key = llm.get("api_key", "") or ""
    masked = (key[:5] + "****" + key[-3:]) if len(key) > 10 else ("已设置" if key else "")
    return jsonify({
        "ok": True,
        "llm": {
            "base_url": llm.get("base_url"),
            "model": llm.get("model"),
            "timeout": llm.get("timeout"),
            "default_temperature": llm.get("default_temperature"),
            "default_max_tokens": llm.get("default_max_tokens"),
            "api_key_set": bool(key),
            "api_key_masked": masked,
        },
        "app": cfg.get("app", {}),
    })


@app.put("/api/config")
def put_config():
    b = _body()
    llm = b.get("llm") or {}
    store.save_llm_config(llm)
    return get_config()


@app.post("/api/config/test")
def test_config():
    from core import llm as llm_mod
    cfg = store.load_config()["llm"]
    if not (cfg.get("api_key") or "").strip():
        return _fail("尚未配置 API Key，请先在模型设置中填入并保存。")
    try:
        import time
        t0 = time.time()
        resp = llm_mod.call_llm([{"role": "user", "content": "回复 OK 两个字母即可"}],
                                model=cfg["model"], temperature=0, max_tokens=8)
        return jsonify({"ok": True, "reply": resp["content"][:80],
                        "latency_ms": int((time.time() - t0) * 1000),
                        "model": resp["model"]})
    except Exception as e:
        return _fail(f"连接失败：{e}")


# ---------------------------------------------------------------- Skills 管理
@app.get("/api/skills")
def list_skills():
    items = store.load_skills()
    for s in items:
        s["dep_tools"] = engine.SKILL_DEP_TOOLS.get(s.get("id"), [])
        s["versions_count"] = len(store.skill_versions(s.get("id")))
    return jsonify({"ok": True, "skills": items})


@app.put("/api/skills/<sid>")
def update_skill(sid):
    """编辑 Skill 并做版本快照：内容性字段(prompt/model_params/名称/描述)确实变化时，
    保存前先落一条旧版快照（data/skill_versions.json），skills.json 写盘失败时旧值不回退。
    body: {name?, description?, prompt?, enabled?, model_params?, note?, actor?}"""
    b = _body()
    try:
        s, version = store.update_skill_versioned(
            sid, b, note=b.get("note", ""), actor=b.get("actor", "") or "admin")
        return jsonify({"ok": True, "skill": s, "version": version,
                        "message": "已保存" + ("，并归档为 " + version["vid"] if version else "（内容无变化，未产生新版本）")})
    except KeyError as e:
        return _fail(str(e))


@app.get("/api/skills/<sid>/versions")
def skill_versions_list(sid):
    cur = store.get_skill(sid)
    if not cur:
        return _fail("Skill 不存在", 404)
    vers = store.skill_versions(sid)
    return jsonify({
        "ok": True,
        "skill": {k: cur.get(k) for k in ("id", "name", "description", "prompt", "model_params", "enabled")},
        "versions": vers,
    })


@app.get("/api/skills/<sid>/versions/<vid>")
def skill_version_detail(sid, vid):
    if not store.get_skill(sid):
        return _fail("Skill 不存在", 404)
    v = store.get_skill_version(sid, vid)
    if not v:
        return _fail("版本不存在", 404)
    return jsonify({"ok": True, "version": v})


@app.post("/api/admin/reset-skills")
def reset_skills():
    store.save_skills(store.DEFAULT_SKILLS)
    return jsonify({"ok": True, "skills": store.load_skills()})


@app.post("/api/skills/<sid>/test")
def test_skill(sid):
    b = _body()
    inputs = b.get("inputs") or b.get("input") or {}
    try:
        return jsonify({"ok": True, "result": engine.test_skill(sid, inputs)})
    except Exception as e:
        return _fail(f"测试失败：{e}")


# ---------------------------------------------------------------- Tools 管理
@app.get("/api/tools")
def list_tools():
    items = []
    for m in tools.META:
        items.append({
            **m,
            "enabled": store.tool_enabled(m["id"]),
            "forced": store.is_tool_forced(m["id"]),
            "dep_skills": engine._TOOL_FED_SKILLS.get(m["id"], []),
            "sample_input": tools.sample_input(m["id"]),
        })
    return jsonify({"ok": True, "tools": items})


@app.put("/api/tools/<tid>")
def set_tool(tid):
    if not tools.get_meta(tid):
        return _fail(f"Tool 不存在: {tid}")
    b = _body()
    if "enabled" in b:
        store.set_tool_enabled(tid, bool(b["enabled"]))
    if "forced" in b:
        store.set_tool_force_error(tid, bool(b["forced"]))
    return list_tools()


def _run_tool_once(tid, params):
    """真实执行一次 Tool 并计时。返回 (ok, output_or_error)。Tool 自身以 {'error':..} 返回视为失败。"""
    import time
    params = params or {}
    if not tools.get_meta(tid):
        raise KeyError(f"Tool 不存在: {tid}")
    t0 = time.perf_counter()
    try:
        out = tools.run_tool(tid, params)
    except Exception as e:
        return False, {"ok": False, "output": None, "error": str(e),
                       "latency_ms": int((time.perf_counter() - t0) * 1000)}
    has_err = isinstance(out, dict) and bool(out.get("error"))
    return True, {"ok": not has_err, "output": out,
                  "error": (out.get("error") if has_err else None),
                  "latency_ms": int((time.perf_counter() - t0) * 1000)}


@app.post("/api/tools/<tid>/test")
def test_tool(tid):
    """真实调用服务端 Tool（非客户端伪造）：返回真实 output / error / latency。
    output 里的 error 键来自 Tool 内部数据校验，同样如实回传。"""
    b = _body()
    try:
        _, payload = _run_tool_once(tid, b.get("inputs") or b.get("params") or {})
        return jsonify({"ok": True, **payload})
    except Exception as e:
        return _fail(f"测试失败：{e}")


@app.get("/api/tools/<tid>/example")
def example_tool(tid):
    """用真实数据构造示例入参并真实运行一次，返回 input/output/error。"""
    meta = tools.get_meta(tid)
    if not meta:
        return _fail("Tool 不存在", 404)
    try:
        params = tools.sample_input(tid)
        ok, payload = _run_tool_once(tid, params)
        return jsonify({"ok": True, "tool_id": tid, "name": meta["name"],
                        "input": params, **payload})
    except Exception as e:
        return _fail(f"示例运行失败：{e}")


@app.post("/api/tools/<tid>/run")
def run_tool_alias(tid):
    """兼容旧后台(/admin)的 Tool 运行入口，等价于 <tid>/test 的真实调用。"""
    b = _body()
    params = b.get("inputs") or b.get("params") or {}
    try:
        out = tools.run_tool(tid, params)
        return jsonify({"ok": True, "output": out})
    except Exception as e:
        return _fail(f"运行失败：{e}")


# ---------------------------------------------------------------- 数据预览
def _data_repo(kind):
    repos = {
        "products": (store.load_products, store.save_products, "P"),
        "activities": (store.load_activities, store.save_activities, "A"),
        "coupons": (store.load_coupons, store.save_coupons, "C"),
    }
    return repos.get(kind)


def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if v is None:
        return []
    return [x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()]


def _num(v, default=None, integer=False):
    if v in (None, ""):
        return default
    try:
        n = float(v)
        return int(n) if integer else n
    except (TypeError, ValueError):
        raise ValueError(f"数字字段格式不正确：{v}")


def _next_id(rows, prefix):
    nums = []
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith(prefix):
            try:
                nums.append(int(rid[len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{(max(nums) if nums else 0) + 1:02d}"


def _normalize_data_row(kind, row, *, rows=None, existing_id=None):
    rows = rows or []
    row = row if isinstance(row, dict) else {}
    rid = (existing_id or row.get("id") or "").strip()
    if not rid:
        repo = _data_repo(kind)
        rid = _next_id(rows, repo[2] if repo else "X")
    if kind == "products":
        name = (row.get("name") or "").strip()
        if not name:
            raise ValueError("商品名称不能为空")
        price = _num(row.get("price"), 0)
        if price < 0:
            raise ValueError("商品价格不能为负数")
        return {
            "id": rid,
            "name": name,
            "category": (row.get("category") or "未分类").strip(),
            "price": round(price, 2),
            "spec": (row.get("spec") or "").strip(),
            "tags": _as_list(row.get("tags")),
            "sales": _num(row.get("sales"), 0, integer=True),
            "stock": _num(row.get("stock"), 0, integer=True),
            "rating": round(_num(row.get("rating"), 4.8), 1),
            "status": (row.get("status") or "在售").strip(),
        }
    if kind == "activities":
        name = (row.get("name") or "").strip()
        if not name:
            raise ValueError("活动名称不能为空")
        return {
            "id": rid,
            "name": name,
            "type": (row.get("type") or "满减").strip(),
            "threshold": _num(row.get("threshold"), None),
            "value": _num(row.get("value"), 0),
            "scope": (row.get("scope") or "all").strip(),
            "product_ids": _as_list(row.get("product_ids")),
            "categories": _as_list(row.get("categories")),
            "start": (row.get("start") or "").strip(),
            "end": (row.get("end") or "").strip(),
            "status": (row.get("status") or "进行中").strip(),
            "description": (row.get("description") or "").strip(),
        }
    if kind == "coupons":
        name = (row.get("name") or "").strip()
        if not name:
            raise ValueError("优惠券名称不能为空")
        return {
            "id": rid,
            "name": name,
            "type": (row.get("type") or "满减券").strip(),
            "condition": _num(row.get("condition"), 0),
            "value": _num(row.get("value"), 0),
            "scope": (row.get("scope") or "all").strip(),
            "categories": _as_list(row.get("categories")),
            "start": (row.get("start") or "").strip(),
            "end": (row.get("end") or "").strip(),
            "description": (row.get("description") or "").strip(),
        }
    raise ValueError(f"未知数据: {kind}")


def _catalog_change_summary(before, after):
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    keys = sorted(set(before.keys()) | set(after.keys()))
    out = []
    for k in keys:
        if before.get(k) != after.get(k):
            out.append({"field": k, "before": before.get(k), "after": after.get(k)})
    return out


def _log_catalog_change(kind, action, *, before=None, after=None):
    item = after or before or {}
    name = item.get("name") or item.get("id") or ""
    return store.append_catalog_change({
        "kind": kind,
        "action": action,
        "itemId": item.get("id"),
        "itemName": name,
        "actor": (_body().get("actor") or "catalog-admin") if request.method in ("POST", "PUT", "DELETE") else "catalog-admin",
        "changes": _catalog_change_summary(before, after),
    })


def _parse_date(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except ValueError:
        return "invalid"


def _catalog_quality():
    products, activities, coupons = store.load_products(), store.load_activities(), store.load_coupons()
    issues = []

    def add(level, kind, item_id, message, field="", action=""):
        issues.append({
            "level": level,
            "kind": kind,
            "itemId": item_id,
            "field": field,
            "message": message,
            "action": action or "请在商品目录中复核并保存。",
        })

    def dup_check(kind, rows):
        seen = set()
        for r in rows:
            rid = str((r or {}).get("id") or "").strip()
            if not rid:
                add("error", kind, "", "缺少 ID", "id", "补齐唯一 ID 后再保存。")
            elif rid in seen:
                add("error", kind, rid, "ID 重复，会导致编辑或引用歧义", "id", "为重复记录改成唯一 ID。")
            seen.add(rid)

    dup_check("products", products)
    dup_check("activities", activities)
    dup_check("coupons", coupons)

    product_ids = {p.get("id") for p in products}
    live_categories = {p.get("category") for p in products if p.get("status") == "在售"}
    all_categories = {p.get("category") for p in products if p.get("category")}
    for p in products:
        pid = p.get("id")
        if not (p.get("name") or "").strip():
            add("error", "products", pid, "商品名称为空", "name", "补充商品名称，避免推荐话术缺失主体。")
        if float(p.get("price") or 0) <= 0:
            add("error", "products", pid, "商品价格必须大于 0", "price", "填写大于 0 的销售价。")
        if int(p.get("stock") or 0) <= 0 and p.get("status") == "在售":
            add("warning", "products", pid, "在售商品库存为 0，推荐可能不可履约", "stock", "补库存或将商品改为下架。")
        if p.get("status") not in ("在售", "下架"):
            add("warning", "products", pid, "商品状态建议使用「在售」或「下架」", "status", "统一状态枚举，避免筛选异常。")
        if not p.get("category"):
            add("warning", "products", pid, "商品未设置品类，会影响活动/券匹配", "category", "补充品类，方便活动与优惠券匹配。")

    for a in activities:
        aid = a.get("id")
        if a.get("type") == "满减":
            if float(a.get("threshold") or 0) <= 0:
                add("error", "activities", aid, "满减活动门槛必须大于 0", "threshold", "填写有效满减门槛。")
            if float(a.get("value") or 0) <= 0:
                add("error", "activities", aid, "满减活动优惠力度必须大于 0", "value", "填写有效优惠金额。")
        if a.get("type") == "折扣" and not (0 < float(a.get("value") or 0) < 1):
            add("error", "activities", aid, "折扣活动 value 应为 0~1 之间的小数", "value", "折扣建议填写 0.8 这类小数。")
        for pid in a.get("product_ids") or []:
            if pid not in product_ids:
                add("error", "activities", aid, f"活动引用了不存在的商品 {pid}", "product_ids", "删除无效商品 ID，或先补齐对应商品。")
        for cat in a.get("categories") or []:
            if cat not in all_categories:
                add("warning", "activities", aid, f"活动品类「{cat}」当前没有商品", "categories", "调整活动品类或新增对应商品。")
            elif cat not in live_categories:
                add("warning", "activities", aid, f"活动品类「{cat}」当前没有在售商品", "categories", "确认是否需要恢复在售商品。")
        sd, ed = _parse_date(a.get("start")), _parse_date(a.get("end"))
        if sd == "invalid" or ed == "invalid":
            add("error", "activities", aid, "活动日期格式应为 YYYY-MM-DD", "date", "按 YYYY-MM-DD 修正日期。")
        elif sd and ed and sd > ed:
            add("error", "activities", aid, "活动开始日期晚于结束日期", "date", "调整开始/结束日期顺序。")
        elif ed and ed != "invalid" and ed < date.today():
            add("warning", "activities", aid, "活动已过期，当前不会影响算价", "end", "如仍需演示，请延长活动结束日期。")

    for c in coupons:
        cid = c.get("id")
        if float(c.get("condition") or 0) < 0:
            add("error", "coupons", cid, "优惠券门槛不能为负数", "condition", "将门槛改为 0 或正数。")
        if float(c.get("value") or 0) <= 0:
            add("error", "coupons", cid, "优惠券面额必须大于 0", "value", "填写大于 0 的优惠金额。")
        if float(c.get("value") or 0) > float(c.get("condition") or 0) and float(c.get("condition") or 0) > 0:
            add("warning", "coupons", cid, "优惠券面额大于使用门槛，请确认是否为运营策略", "value", "确认是否为补贴券，否则调低面额。")
        for cat in c.get("categories") or []:
            if cat not in all_categories:
                add("warning", "coupons", cid, f"优惠券品类「{cat}」当前没有商品", "categories", "调整券适用品类或新增对应商品。")
            elif cat not in live_categories:
                add("warning", "coupons", cid, f"优惠券品类「{cat}」当前没有在售商品", "categories", "确认是否需要恢复在售商品。")
        sd, ed = _parse_date(c.get("start")), _parse_date(c.get("end"))
        if sd == "invalid" or ed == "invalid":
            add("error", "coupons", cid, "优惠券日期格式应为 YYYY-MM-DD", "date", "按 YYYY-MM-DD 修正日期。")
        elif sd and ed and sd > ed:
            add("error", "coupons", cid, "优惠券开始日期晚于结束日期", "date", "调整开始/结束日期顺序。")
        elif ed and ed != "invalid" and ed < date.today():
            add("warning", "coupons", cid, "优惠券已过期，当前不会参与推荐算价", "end", "如仍需演示，请延长优惠券结束日期。")

    counts = {
        "error": sum(1 for x in issues if x["level"] == "error"),
        "warning": sum(1 for x in issues if x["level"] == "warning"),
    }
    affected = {}
    for x in issues:
        affected.setdefault(x["kind"], set()).add(x.get("itemId") or "")
    return {
        "ok": counts["error"] == 0,
        "counts": counts,
        "issues": issues,
        "affected": {k: sorted(v) for k, v in affected.items()},
        "recommendations": [x["action"] for x in issues[:6]],
        "checkedAt": store.now_iso(),
    }


@app.get("/api/data/<kind>")
def data_kind(kind):
    repo = _data_repo(kind)
    if not repo:
        return _fail(f"未知数据: {kind}")
    return jsonify({"ok": True, "kind": kind, "rows": repo[0]()})


@app.get("/api/data/changes")
def data_changes():
    limit = request.args.get("limit", default=60, type=int)
    kind = (request.args.get("kind") or "").strip()
    rows = store.load_catalog_changes(limit=max(1, min(limit, 200)))
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return jsonify({"ok": True, "changes": rows[:limit]})


@app.get("/api/data/quality")
def data_quality():
    return jsonify({"ok": True, "quality": _catalog_quality()})


@app.post("/api/data/<kind>")
def data_create(kind):
    repo = _data_repo(kind)
    if not repo:
        return _fail(f"未知数据: {kind}")
    load, save, _prefix = repo
    rows = load()
    try:
        item = _normalize_data_row(kind, _body(), rows=rows)
        if any(str(r.get("id")) == item["id"] for r in rows):
            return _fail(f"id 已存在：{item['id']}")
        rows.append(item)
        save(rows)
        change = _log_catalog_change(kind, "create", after=item)
        return jsonify({"ok": True, "kind": kind, "item": item, "rows": rows, "change": change})
    except ValueError as e:
        return _fail(str(e))


@app.put("/api/data/<kind>/<rid>")
def data_update(kind, rid):
    repo = _data_repo(kind)
    if not repo:
        return _fail(f"未知数据: {kind}")
    load, save, _prefix = repo
    rows = load()
    try:
        item = _normalize_data_row(kind, {**_body(), "id": rid}, rows=rows, existing_id=rid)
        for i, old in enumerate(rows):
            if str(old.get("id")) == rid:
                rows[i] = item
                save(rows)
                change = _log_catalog_change(kind, "update", before=old, after=item)
                return jsonify({"ok": True, "kind": kind, "item": item, "rows": rows, "change": change})
        return _fail(f"未找到 id：{rid}", 404)
    except ValueError as e:
        return _fail(str(e))


@app.delete("/api/data/<kind>/<rid>")
def data_delete(kind, rid):
    repo = _data_repo(kind)
    if not repo:
        return _fail(f"未知数据: {kind}")
    load, save, _prefix = repo
    rows = load()
    kept = [r for r in rows if str(r.get("id")) != rid]
    if len(kept) == len(rows):
        return _fail(f"未找到 id：{rid}", 404)
    old = next((r for r in rows if str(r.get("id")) == rid), None)
    save(kept)
    change = _log_catalog_change(kind, "delete", before=old)
    return jsonify({"ok": True, "kind": kind, "deleted": rid, "rows": kept, "change": change})


# ---------------------------------------------------------------- Agent 执行
@app.get("/api/agent/abilities")
def agent_abilities():
    ab = engine.build_abilities()
    all_skills = store.load_skills()
    disabled = [s["id"] for s in all_skills if not s.get("enabled")]
    disabled_tools = [m["id"] for m in tools.META if not store.tool_enabled(m["id"])]
    eff = providers.effective_provider_name()
    cfgp = providers.config_for(eff)
    model = ("fixture-demo" if eff == "demo-fixture"
             else (cfgp.get("bot_id") or cfgp.get("model")) if eff == "coze" else cfgp.get("model"))
    return jsonify({
        "ok": True,
        "skills": ab["skills"],
        "tools": ab["tools"],
        "disabled_skills": disabled,
        "disabled_tools": disabled_tools,
        "provider": eff,
        "llm_ready": providers._provider_ready(eff),
        "model": model,
        "rule_hints": [
            "Planner 只能调用存在且启用的 Skill / Tool",
            "价格 / 活动 / 优惠券一律调用 Tool，禁止大模型编造",
            "最终回复必须经过「风控审核」Skill",
        ],
    })


def _provider_status_payload():
    name = providers.effective_provider_name()
    demo = name == "demo-fixture"
    ready = providers._provider_ready(name)
    labels = {"openai-compatible": "真实模型（OpenAI 兼容）",
              "coze": "真实模型（Coze Bot）",
              "demo-fixture": "演示模式（离线确定性剧本）"}
    if demo:
        model = "fixture-demo"
    else:
        cfgp = providers.config_for(name)
        model = (cfgp.get("bot_id") or cfgp.get("model") or "deepseek-chat") if name == "coze" else (cfgp.get("model") or "deepseek-chat")
    hint = None
    if not ready:
        if name == "coze":
            hint = "未配置 COZE_API_KEY / COZE_BOT_ID，请在「模型设置」填写或设置 SNACK_LLM_PROVIDER=demo-fixture 切演示"
        else:
            hint = "未配置 API Key：请在「模型设置」填写，或设置 SNACK_LLM_PROVIDER=demo-fixture 切演示"
    return {"provider": name, "label": labels.get(name, name),
            "model": model, "demo": demo, "llm_ready": ready, "hint": hint,
            "stable_demo": demo}


@app.get("/api/provider/status")
def provider_status():
    """当前生效的 LLM Provider 状态（页面顶部徽标 / 冒烟脚本用）。
    跟随 env SNACK_LLM_PROVIDER 权威选择；未设时反映后台 /models 已切换的 provider(llm_state)。"""
    return jsonify({"ok": True, **_provider_status_payload()})


def _batch_public_stats(batch):
    if not batch:
        return None
    res = batch.get("caseResults") or []
    counts = {k: sum(1 for r in res if r.get("status") == k)
              for k in ("PASS", "FAIL", "REVIEW", "ERROR")}
    denom = counts["PASS"] + counts["FAIL"] + counts["REVIEW"]
    return {
        "id": batch.get("id"),
        "name": batch.get("name") or "",
        "versionLabel": batch.get("versionLabel") or "",
        "changeNote": batch.get("changeNote") or "",
        "status": batch.get("status"),
        "createdAt": batch.get("createdAt"),
        "finishedAt": batch.get("finishedAt"),
        "total": len(res) or batch.get("total") or len(batch.get("caseIds") or []),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "review": counts["REVIEW"],
        "errors": counts["ERROR"],
        "passRate": round(100.0 * counts["PASS"] / denom, 1) if denom else None,
    }


def _console_decision(provider, quality, ops, latest_batch, comparison, active_batch_id):
    blockers = []
    warnings = []
    if not provider.get("llm_ready") and not provider.get("demo"):
        blockers.append("真实 Provider 未配置完成")
    if (quality.get("counts") or {}).get("error", 0):
        blockers.append(f"目录存在 {quality['counts']['error']} 个阻断问题")
    if active_batch_id:
        warnings.append("评测批次正在运行")
    if latest_batch:
        if latest_batch.get("failed", 0) or latest_batch.get("errors", 0):
            warnings.append(f"最近评测仍有 {latest_batch.get('failed', 0)} 个失败、{latest_batch.get('errors', 0)} 个异常")
        if latest_batch.get("review", 0):
            warnings.append(f"最近评测有 {latest_batch.get('review', 0)} 个需复核")
    else:
        warnings.append("还没有可用于上线判断的评测批次")
    metrics = ops.get("metrics") or {}
    if metrics.get("failedSteps", 0):
        warnings.append(f"运行记录中有 {metrics.get('failedSteps')} 个失败步骤")
    if metrics.get("ratings", {}).get("low", 0):
        warnings.append(f"存在 {metrics['ratings']['low']} 条低分反馈")
    if comparison and (comparison.get("counts") or {}).get("regressed", 0):
        warnings.append(f"版本对比新增失败 {comparison['counts']['regressed']} 例")

    if blockers:
        state, label, reason = "blocked", "不建议上线", "；".join(blockers[:3])
    elif warnings:
        state, label, reason = "review", "建议复核", "；".join(warnings[:3])
    else:
        state, label, reason = "ready", "可演示", "目录、模型、评测与运营记录未发现明显阻断项"
    return {"state": state, "label": label, "reason": reason,
            "blockers": blockers, "warnings": warnings}


@app.get("/api/console/summary")
def console_summary():
    """总控台首页：聚合目录健康、Provider、运营问题与评测批次，输出上线决策。"""
    try:
        provider = _provider_status_payload()
        quality = _catalog_quality()
        ops = ops_mod.build_overview(recent=8)
        changes = store.load_catalog_changes(limit=8)
        batches = eval_batch_mod.list_batches(limit=8)
        done = [b for b in batches if b.get("status") == "done"]
        latest = _batch_public_stats(done[0] if done else (batches[0] if batches else None))
        previous = _batch_public_stats(done[1] if len(done) > 1 else None)
        comparison = None
        if len(done) > 1:
            try:
                comparison = eval_batch_mod.compare_batches(done[1]["id"], done[0]["id"])
            except Exception:
                comparison = None
        active = eval_batch_mod.active_batch_id()
        decision = _console_decision(provider, quality, ops, latest, comparison, active)
        clusters = [c for c in (ops.get("clusters") or []) if c.get("key") != "empty"]
        next_actions = []
        if decision["state"] == "blocked":
            next_actions.append({"label": "先修复目录或模型配置", "target": "/console#catalog"})
        if latest and (latest.get("failed") or latest.get("review") or latest.get("errors")):
            next_actions.append({"label": "查看评测失败并沉淀改进", "target": "/console#eval"})
        if clusters:
            next_actions.append({"label": "处理运营问题样本", "target": "/console#ops"})
        if not next_actions:
            next_actions.append({"label": "复跑默认评测集确认稳定", "target": "/console#eval"})
        return jsonify({
            "ok": True,
            "decision": decision,
            "provider": provider,
            "catalogQuality": quality,
            "catalogChanges": changes,
            "ops": {
                "metrics": ops.get("metrics") or {},
                "riskByType": ops.get("riskByType") or [],
                "clusters": clusters[:4],
                "recentRuns": ops.get("recentRuns") or [],
            },
            "eval": {
                "latest": latest,
                "previous": previous,
                "comparison": comparison,
                "activeBatchId": active,
                "lastCaseIds": eval_batch_mod.last_case_ids(),
                "evaluator": eval_mod.evaluator_snapshot(),
            },
            "nextActions": next_actions,
        })
    except Exception as e:
        return _fail(f"总控台摘要生成失败：{e}")


@app.post("/api/agent/plan")
def agent_plan():
    question = _body().get("question", "")
    try:
        pl = engine.plan(question)
        return jsonify({"ok": True, "plan": pl})
    except Exception as e:
        return _fail(f"生成计划失败：{e}")


@app.post("/api/agent/execute")
def agent_execute():
    b = _body()
    question = b.get("question", "")
    plan_obj = b.get("plan")
    if not plan_obj:
        return _fail("缺少计划 plan，请先生成计划。")
    try:
        result = engine.execute(question, plan_obj)
        result["ok"] = True if result.get("ok") is None else result.get("ok")
        if result.get("ok"):
            engine._log_run(question, plan_obj, result)
        return jsonify(result)
    except Exception as e:
        return _fail(f"执行失败：{e}")


@app.post("/api/agent/run")
def agent_run():
    """主链路执行：stream=true（默认）返回 SSE 事件流；stream=false 返回完整 JSON。

    SSE 事件：status / plan / step / reply / risk / error / done。
    底层统一走 runner.run_run（plan→validate→execute→风险审计→RunRecord）。
    """
    b = _body()
    question = (b.get("question") or "").strip()
    if not question:
        return _fail("question 不能为空")
    source = b.get("source") or "api"
    conversation_id = b.get("conversationId") or b.get("conversation_id")
    force = b.get("forceErrorTool") or b.get("force_error_tool")
    mandatory = b.get("mandatoryCapabilities")
    stream = bool(b.get("stream", True))

    if not stream:
        try:
            rec = runner.run_run(question, source=source, conversation_id=conversation_id,
                                 mandatory_capabilities=mandatory, force_error_tool=force)
            return jsonify(_run_json_payload(rec))
        except Exception as e:
            return _fail(f"执行失败：{e}")

    # ---- SSE：后台线程跑主链路，queue 桥接到流式响应 ----
    out_q = queue.Queue()

    def emit(etype, data):
        try:
            out_q.put_nowait({"event": etype, "data": data})
        except Exception:
            pass

    def worker():
        try:
            runner.run_run(question, source=source, conversation_id=conversation_id,
                           mandatory_capabilities=mandatory, force_error_tool=force,
                           emit=emit)
        finally:
            try:
                out_q.put_nowait(None)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            item = out_q.get()
            if item is None:
                break
            try:
                yield "event: {ev}\ndata: {data}\n\n".format(
                    ev=item["event"], data=json.dumps(item["data"], ensure_ascii=False))
            except Exception:
                break

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- Planner 配置与预览
def _planner_payload():
    pc = store.load_planner_config()
    skills_all = store.load_skills()
    tools_all = tools.META
    return {
        "ok": True,
        "planner": {
            "version": pc.get("version", 0),
            "prompt": pc.get("prompt") or "",
            "is_custom": bool((pc.get("prompt") or "").strip()),
            "mandatoryCapabilities": pc.get("mandatoryCapabilities", []),
            "note": pc.get("note", ""),
            "actor": pc.get("actor", ""),
            "updated_at": pc.get("updated_at"),
            "history": pc.get("history", []),
            "builtin": engine.PLAN_SYSTEM,
        },
        "skills": [{"id": s["id"], "name": s["name"], "description": s.get("description", ""),
                    "role": s.get("role", ""), "enabled": bool(s.get("enabled", True))} for s in skills_all],
        "tools": [{"id": m["id"], "name": m["name"], "description": m["description"],
                   "artifact": m.get("artifact"), "enabled": store.tool_enabled(m["id"])} for m in tools_all],
    }


@app.get("/api/planner")
def planner_get():
    return jsonify(_planner_payload())


@app.put("/api/planner")
def planner_put():
    b = _body()
    try:
        if "prompt" in b:
            store.save_planner_prompt(b.get("prompt") or "", note=b.get("note", ""),
                                      actor=b.get("actor", "") or "admin")
        if "mandatoryCapabilities" in b:
            store.save_planner_capabilities(b.get("mandatoryCapabilities") or [],
                                            actor=b.get("actor", "") or "admin")
        return jsonify(_planner_payload())
    except Exception as e:
        return _fail(f"保存失败：{e}")


@app.post("/api/planner/preview")
def planner_preview():
    """对给定问题跑一次 Planner（用当前已启用的能力 + Planner 提示词），
    并对产出的计划做与执行前一致的 validate_plan 静态校验。预览不落 RunRecord。
    body: {question, mandatoryCapabilities?}"""
    b = _body()
    question = (b.get("question") or "").strip()
    if not question:
        return _fail("question 不能为空")
    mandatory = b.get("mandatoryCapabilities")
    if mandatory is None:
        mandatory = (store.load_planner_config() or {}).get("mandatoryCapabilities", []) or []
    try:
        pl = engine.plan(question, mandatory_capabilities=mandatory or None)
        risk_hits = validator_mod.detect_risk(question)
        val = validator_mod.validate_plan(pl, engine.build_abilities(),
                                          question=question, risk_signal=risk_hits)
        return jsonify({"ok": True, "plan": pl, "validation": val})
    except Exception as e:
        # 能力不足等导致无法生成计划时，给出结构化校验失败而非 500
        err = {"code": "plan_failed", "severity": "error", "detail": str(e)}
        return jsonify({"ok": False, "error": str(e),
                        "plan": None,
                        "validation": {"ok": False, "errors": [err], "warnings": [],
                                       "degraded": True, "scenario": validator_mod.classify_scenario(question)}})


# ---------------------------------------------------------------- LLM-Config（/models 后台）
def _llm_config_payload():
    from core import providers as P
    cat = P.provider_catalog_status()
    eff = P.effective_provider_name()
    ls = store.load_llm_state()
    provs = ls.get("providers") or {}
    # 回显 llm_state 里的非密钥字段（base_url/model/timeout/bot_id）；密钥永不回传原文
    for c in cat:
        st = provs.get(c["id"]) or {}
        c["config"] = {k: st.get(k) for k in ("base_url", "model", "timeout", "bot_id")
                       if k in st and k != "api_key"}
    env_forced = (os.environ.get("SNACK_LLM_PROVIDER") or "").strip().lower() or None
    return {
        "ok": True,
        "effective": {
            "name": eff,
            "demo": eff == "demo-fixture",
            "ready": P._provider_ready(eff),
            "env_forcing": bool(env_forced),
            "env_provider": env_forced,
        },
        "catalog": cat,
    }


@app.get("/api/llm-config")
def llm_config_get():
    return jsonify(_llm_config_payload())


@app.put("/api/llm-config")
def llm_config_put():
    b = _body()
    try:
        sel = b.get("selected") or b.get("provider")
        if sel:
            store.save_llm_state(provider=sel)
        for pid, pc in (b.get("providers") or {}).items():
            store.save_llm_state(provider_config={**(pc or {}), "id": pid})
        return jsonify(_llm_config_payload())
    except Exception as e:
        return _fail(f"保存失败：{e}")


@app.post("/api/llm-config/switch")
def llm_config_switch():
    pid = _body().get("provider")
    if not pid:
        return _fail("provider 不能为空")
    try:
        store.save_llm_state(provider=pid)
        return jsonify(_llm_config_payload())
    except Exception as e:
        return _fail(str(e))


@app.post("/api/llm-config/test")
def llm_config_test():
    pid = _body().get("provider")
    try:
        res = providers.test_connection(pid or None)
        return jsonify({"ok": True, **res})
    except Exception as e:
        # 结构化的中文错误（含如何切演示模式提示），回传完整 detail
        return jsonify({"ok": False, "error": str(e), "detail": str(e)})


# ---------------------------------------------------------------- Runs（RunRecord）
@app.get("/api/runs")
def runs_list():
    limit = request.args.get("limit", default=30, type=int)
    offset = request.args.get("offset", default=0, type=int)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    def _trim(r):
        fr = r.get("finalReply") or {}
        return {
            "id": r.get("id"), "question": r.get("question"),
            "source": r.get("source"), "status": r.get("status"),
            "createdAt": r.get("createdAt"), "durationMs": r.get("durationMs"),
            "provider": r.get("provider"), "model": r.get("model"),
            "retryOf": r.get("retryOf"),
            "steps_count": len(r.get("steps") or []),
            "reply_preview": str(fr.get("text", ""))[:80] if isinstance(fr, dict) else "",
            "error": r.get("error"),
        }

    rows = [_trim(r) for r in store.list_runs(limit=limit, offset=offset)]
    return jsonify({"ok": True, "limit": limit, "offset": offset, "runs": rows})


@app.get("/api/runs/<rid>")
def runs_detail(rid):
    rec = store.get_run(rid)
    if not rec:
        return _fail("运行记录不存在", 404)
    return jsonify({"ok": True, "run": rec})


@app.post("/api/run/retry")
def run_retry():
    rid = _body().get("runId")
    orig = store.get_run(rid) if rid else None
    if not orig:
        return _fail("运行记录不存在", 404)
    question = (orig.get("question") or "").strip()
    if not question:
        return _fail("原记录缺少 question，无法重试", 404)
    try:
        rec = runner.run_run(question, source=orig.get("source") or "api",
                             conversation_id=orig.get("conversationId"),
                             retry_of=rid)
        return jsonify(_run_json_payload(rec))
    except Exception as e:
        return _fail(f"重试失败：{e}")


@app.post("/api/runs/<rid>/handoff")
def run_handoff(rid):
    b = _body()
    rec = runner.set_handoff(rid, mode=b.get("mode") or "manual", note=b.get("note") or "")
    if not rec:
        return _fail("运行记录不存在", 404)
    return jsonify({"ok": True, "run": rec})


@app.post("/api/runs/<rid>/annotate")
def run_annotate(rid):
    b = _body()
    rec = runner.set_annotation(rid, text=b.get("text") or "", author=b.get("author") or "")
    if not rec:
        return _fail("运行记录不存在", 404)
    return jsonify({"ok": True, "run": rec})


@app.post("/api/explain")
def run_explain():
    rid = _body().get("runId")
    rec = store.get_run(rid) if rid else None
    if not rec:
        return _fail("运行记录不存在", 404)
    text = runner.build_explanation(rec)
    # 有真实 Provider Key 时可选润色；失败/无 key 一律回落确定性解释器
    polished = text
    name = (os.environ.get("SNACK_LLM_PROVIDER") or "openai-compatible").strip().lower()
    if name not in ("demo-fixture", "fixture"):
        try:
            from core import llm as llm_mod
            resp = llm_mod.call_llm([{"role": "system", "content": "你是技术解释助手，把下面的运行记录解释用通顺中文润色成 3-6 句，不要编造事实，直接输出润色文本。"},
                                     {"role": "user", "content": text}],
                                    temperature=0.3, max_tokens=400)
            polished = (resp.get("content") or "").strip() or text
        except Exception:
            polished = text
    return jsonify({"ok": True, "runId": rid, "explanation": polished,
                    "polished": polished != text})


# ---------------------------------------------------------------- 执行日志
@app.get("/api/agent/logs")
def agent_logs():
    limit = request.args.get("limit", default=20, type=int)
    return jsonify({"ok": True, "logs": store.load_run_logs(limit)})


@app.get("/api/agent/logs/<log_id>")
def agent_log_detail(log_id):
    e = store.get_run_log(log_id)
    if not e:
        return _fail("日志不存在")
    return jsonify({"ok": True, "log": e})


# ---------------------------------------------------------------- 运营中心（/ops）
@app.get("/api/ops/overview")
def ops_overview():
    try:
        return jsonify({"ok": True, **ops_mod.build_overview()})
    except Exception as e:
        # 任何计算异常都不让运营页 500：回一个可渲染的空结构 + 错误提示
        return jsonify({"ok": False, "error": str(e),
                        "metrics": {}, "riskByType": [], "clusters": [],
                        "recentRuns": []})


@app.post("/api/ratings")
def ops_create_rating():
    b = _body()
    try:
        item = ops_mod.create_rating(
            run_id=b.get("runId"), score=b.get("score", 3),
            problem_type=b.get("problemType"), comment=b.get("comment"),
            author=b.get("author") or "admin", source=b.get("source"))
        return jsonify({"ok": True, "rating": item})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/ratings")
def ops_list_ratings():
    b = request.args
    try:
        rows, total = ops_mod.list_ratings(
            limit=max(1, min(b.get("limit", 50, type=int), 500)),
            offset=max(0, b.get("offset", 0, type=int)),
            from_date=b.get("from") or None, to_date=b.get("to") or None,
            score_min=b.get("scoreMin", type=int),
            problem_type=b.get("problemType") or None,
            badcase_only=bool(b.get("badcase") == "1"))
        return jsonify({"ok": True, "ratings": rows, "total": total,
                        "stats": ops_mod.rating_stats(ops_mod._rows("rating"))})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/annotations")
def ops_create_annotation():
    b = _body()
    try:
        item = ops_mod.create_annotation(
            run_id=b.get("runId"), question=b.get("question") or "",
            dimensions=b.get("dimensions") or {}, status=b.get("status") or "pending",
            annotator=b.get("annotator") or "", note=b.get("note") or "")
        return jsonify({"ok": True, "annotation": item})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/annotations/export")
def ops_export_annotations():
    # 注册在 /<id> 之前，避免 id 路由吞掉 "export"
    fmt = request.args.get("format", "json")
    try:
        body, filename, mime = ops_mod.export_annotations(fmt)
        return Response(body, mimetype=mime,
                        headers={"Content-Disposition":
                                 f"attachment; filename={filename}"})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/annotations")
def ops_list_annotations():
    b = request.args
    try:
        rows, total = ops_mod.list_annotations(
            limit=max(1, min(b.get("limit", 50, type=int), 500)),
            offset=max(0, b.get("offset", 0, type=int)),
            status=b.get("status") or None,
            annotator=b.get("annotator") or None,
            low_only=bool(b.get("low") == "1"))
        return jsonify({"ok": True, "annotations": rows, "total": total})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/annotations/<aid>")
def ops_annotation_detail(aid):
    a = ops_mod.get_annotation(aid)
    if not a:
        return _fail("标注不存在", 404)
    return jsonify({"ok": True, "annotation": a})


@app.put("/api/annotations/<aid>")
def ops_update_annotation(aid):
    b = _body()
    try:
        a = ops_mod.update_annotation(aid, status=b.get("status"), note=b.get("note"))
        if not a:
            return _fail("标注不存在", 404)
        return jsonify({"ok": True, "annotation": a})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/improvements")
def ops_list_improvements():
    b = request.args
    rows, total = ops_mod.list_improvements(
        limit=max(1, min(b.get("limit", 100, type=int), 500)),
        offset=max(0, b.get("offset", 0, type=int)))
    return jsonify({"ok": True, "improvements": rows, "total": total})


@app.post("/api/improvements")
def ops_create_improvement():
    b = _body()
    try:
        item = ops_mod.create_improvement(
            title=b.get("title") or "", kind=b.get("kind") or "manual",
            from_rating=b.get("fromRating"), from_annotation=b.get("fromAnnotation"),
            run_id=b.get("runId"), problem_type=b.get("problemType") or "",
            note=b.get("note") or "")
        return jsonify({"ok": True, "improvement": item})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/improvements/generate-prompt")
def ops_generate_prompt():
    imp_id = _body().get("improvementId")
    if not imp_id:
        return _fail("improvementId 不能为空")
    try:
        imp, draft = ops_mod.compose_draft(imp_id)
        return jsonify({"ok": True, "improvement": imp, "draft": draft})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/improvements/apply-prompt")
def ops_apply_prompt():
    b = _body()
    imp_id = b.get("improvementId")
    if not imp_id:
        return _fail("improvementId 不能为空")
    try:
        imp = ops_mod.apply_improvement(imp_id, confirm=bool(b.get("confirm")),
                                        actor=b.get("actor") or "admin")
        return jsonify({"ok": True, "improvement": imp,
                        "message": f"已应用并自动留档快照 {imp['applied']['snapshotVid']}"})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/improvements/<iid>/status")
def ops_improvement_status(iid):
    b = _body()
    try:
        imp = ops_mod.update_improvement_status(iid, b.get("status") or "")
        if not imp:
            return _fail("改进建议不存在", 404)
        return jsonify({"ok": True, "improvement": imp})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/improvements/<iid>/rollback")
def ops_improvement_rollback(iid):
    try:
        imp = ops_mod.rollback_improvement(iid, actor=_body().get("actor") or "admin")
        return jsonify({"ok": True, "improvement": imp,
                        "message": "已回滚到应用前快照"})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/skills/<sid>/snapshot")
def ops_skill_snapshot(sid):
    b = _body()
    try:
        v = ops_mod.snapshot_skill(sid, note=b.get("note") or "",
                                   actor=b.get("actor") or "admin")
        return jsonify({"ok": True, "version": v,
                        "message": "已创建快照 " + v["vid"]})
    except Exception as e:
        return _fail(str(e))


@app.post("/api/skills/<sid>/rollback")
def ops_skill_rollback(sid):
    b = _body()
    vid = b.get("vid")
    if not vid:
        return _fail("vid 不能为空")
    try:
        res = ops_mod.restore_skill_snapshot(sid, vid, actor=b.get("actor") or "admin",
                                             note=b.get("note") or "")
        return jsonify({"ok": True, **res})
    except Exception as e:
        return _fail(str(e))


@app.get("/api/ab-tests")
def ops_list_ab_tests():
    return jsonify({"ok": True, "tests": ops_mod.list_ab_tests()})


@app.post("/api/ab-tests")
def ops_create_ab_test():
    b = _body()
    try:
        item = ops_mod.create_ab_test(
            name=b.get("name") or "", variant_a=b.get("variantA") or {},
            variant_b=b.get("variantB") or {},
            target_type=b.get("targetType") or "skill",
            target_id=b.get("targetId") or "reply",
            note=b.get("note") or "")
        return jsonify({"ok": True, "test": item})
    except Exception as e:
        return _fail(str(e))


@app.put("/api/ab-tests/<aid>")
def ops_update_ab_test(aid):
    b = _body()
    try:
        item = ops_mod.update_ab_test(aid, active=b.get("active"),
                                      active_variant=b.get("activeVariant"),
                                      note=b.get("note"))
        if not item:
            return _fail("实验不存在", 404)
        return jsonify({"ok": True, "test": item})
    except Exception as e:
        return _fail(str(e))


# ---------------------------------------------------------------- 评测中心（/eval）
def _case_fields(b):
    """从请求体里取用例字段：支持 {case:{...}} 嵌套或平铺；忽略控制字段。"""
    inner = b.get("case") if isinstance(b.get("case"), dict) else b
    out = {k: v for k, v in inner.items()}
    for k in ("fromRunId", "run", "op"):
        out.pop(k, None)
    return out


def _case_json(c, brief=False):
    if brief:
        return {"id": c.get("id"), "name": c.get("name"), "category": c.get("category"),
                "difficulty": c.get("difficulty"), "riskLevel": c.get("riskLevel"),
                "enabled": bool(c.get("enabled"))}
    return c


@app.get("/api/eval")
def eval_list():
    """评测集总览：分页/过滤后的用例 + 能力选项(skills/tools) + 过滤元数据。"""
    a = request.args
    enabled = None
    if a.get("enabled") is not None:
        enabled = str(a.get("enabled")).lower() in ("1", "true", "yes")
    limit = max(1, min(a.get("limit", default=500, type=int), 1000))
    offset = max(0, a.get("offset", default=0, type=int))
    try:
        cases, total = eval_mod.list_cases(
            limit=limit, offset=offset,
            category=a.get("category") or None,
            difficulty=a.get("difficulty") or None,
            risk_level=a.get("risk") or a.get("riskLevel") or None,
            dim=a.get("dim") or None,
            enabled=enabled,
            q=a.get("q") or None)
    except Exception as e:
        return _fail(str(e))
    skills = [{"id": s.get("id"), "name": s.get("name", s.get("id")),
               "enabled": bool(s.get("enabled"))} for s in store.load_skills()]
    tools_list = [{"id": t.get("id"), "name": t.get("name", t.get("id")),
                   "enabled": store.tool_enabled(t.get("id"))} for t in tools.META]
    return jsonify({
        "ok": True,
        "meta": {
            "categories": eval_mod.CATEGORIES,
            "difficulties": eval_mod.DIFFICULTIES,
            "riskLevels": eval_mod.RISK_LEVELS,
            "dimensions": [{"key": k, "label": l} for k, l in eval_mod.DIM_OPTIONS],
        },
        "counts": eval_mod.case_counts(),
        "cases": cases,
        "total": total,
        "skills": skills,
        "tools": tools_list,
        "enabledIds": eval_mod.default_enabled_set(),
    })


def _default_case_fields_from_issue(source_type, source_id, *, question="", title="", note="", run=None):
    risk = (run or {}).get("risk") or (run or {}).get("riskResult") or {}
    is_risk = bool(risk.get("isRisky")) or source_type == "risk"
    problem = note or title or ""
    category = "风险安全" if is_risk else ("价格核验" if "价格" in problem or "金额" in problem else "合规回复")
    dims = ["risk_response", "reply_safety"] if is_risk else (["price_honesty", "accuracy"] if category == "价格核验" else ["accuracy", "completeness", "reply_safety"])
    fields = {
        "name": title or f"由{source_type}沉淀 · {str(question or source_id)[:18]}",
        "question": question,
        "category": category,
        "difficulty": "medium",
        "riskLevel": "high" if is_risk else "mid",
        "expectedBehavior": note or "由运营问题样本沉淀，需走真实 Agent 主链路并输出可追溯、合规的客服回复。",
        "evalDimension": dims,
        "tags": ["ops_issue", source_type],
        "enabled": True,
    }
    if is_risk:
        fields.update({
            "expectRisk": True,
            "forbiddenCapabilities": ["query_products", "query_coupons", "query_activities", "compute_price"],
            "expectedKeywords": [{"words": ["官方", "诈骗", "不要"], "mode": "any"}],
        })
    return fields


def _case_from_issue_payload(b):
    source_type = (b.get("sourceType") or b.get("source_type") or "").strip()
    source_id = (b.get("sourceId") or b.get("source_id") or "").strip()
    fields = b.get("fields") if isinstance(b.get("fields"), dict) else {}
    if source_type not in ("run", "rating", "annotation", "improvement", "risk"):
        raise ValueError("sourceType 必须为 run/rating/annotation/improvement/risk")
    if not source_id:
        raise ValueError("sourceId 不能为空")

    run_id = None
    question = ""
    title = ""
    note = ""
    if source_type in ("run", "risk"):
        run_id = source_id
        run = store.get_run(run_id)
        if not run:
            raise ValueError("运行记录不存在")
        question = run.get("question") or ""
        title = f"由运行沉淀 · {question[:18]}"
        note = "运行样本需要进入回归评测，验证后续改进是否稳定。"
    elif source_type == "rating":
        row = next((r for r in reversed(ops_mod._rows("rating")) if r.get("id") == source_id), None)
        if not row:
            raise ValueError("评分记录不存在")
        run_id = row.get("runId")
        question = row.get("question") or ""
        title = f"低分反馈 · {row.get('problemType') or '服务质量'}"
        note = row.get("comment") or f"来源评分 {row.get('score')} 分，问题类型：{row.get('problemType') or '未分类'}。"
    elif source_type == "annotation":
        row = ops_mod.get_annotation(source_id)
        if not row:
            raise ValueError("标注记录不存在")
        run_id = row.get("runId")
        question = row.get("question") or ""
        overall = (row.get("dimensions") or {}).get("overall")
        title = f"人工标注 · 整体{overall if overall is not None else '-'}分"
        note = row.get("note") or "来源人工标注，需回归验证回复正确性、完整性与安全性。"
    else:
        row = ops_mod.get_improvement(source_id)
        if not row:
            raise ValueError("改进建议不存在")
        run_id = (row.get("sampleRunIds") or [None])[0]
        run = store.get_run(run_id) if run_id else None
        question = (run or {}).get("question") or row.get("title") or ""
        title = f"改进建议回归 · {row.get('title') or source_id}"
        note = row.get("directionText") or "改进建议应用后需要回归验证。"

    run = store.get_run(run_id) if run_id else None
    base = _default_case_fields_from_issue(source_type, source_id, question=question,
                                           title=title, note=note, run=run)
    base.update(fields)
    if run_id and store.get_run(run_id):
        return eval_mod.from_run(run_id, base)
    return eval_mod.create_case(base)


@app.post("/api/eval/cases/from-issue")
def eval_case_from_issue():
    try:
        c = _case_from_issue_payload(_body())
        return jsonify({"ok": True, "case": c})
    except ValueError as e:
        return _fail(str(e), 400)
    except Exception as e:
        return _fail(f"沉淀评测用例失败：{e}")


@app.get("/api/eval/cases/<cid>")
def eval_case_detail(cid):
    c = eval_mod.get_case(cid)
    if not c:
        return _fail("评测用例不存在", 404)
    return jsonify({"ok": True, "case": c})


@app.post("/api/eval/cases")
def eval_create_case():
    b = _body()
    from_run_id = (b.get("fromRunId") or "").strip()
    fields = _case_fields(b)
    try:
        if from_run_id:
            c = eval_mod.from_run(from_run_id, fields or None)
        else:
            c = eval_mod.create_case(fields)
        return jsonify({"ok": True, "case": c})
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"创建用例失败：{e}")


@app.patch("/api/eval/cases/<cid>")
def eval_update_case(cid):
    c = eval_mod.update_case(cid, _case_fields(_body()))
    if not c:
        return _fail("评测用例不存在", 404)
    return jsonify({"ok": True, "case": c})


@app.delete("/api/eval/cases/<cid>")
def eval_delete_case(cid):
    c = eval_mod.delete_case(cid)
    if not c:
        return _fail("评测用例不存在", 404)
    return jsonify({"ok": True, "deleted": _case_json(c, brief=True)})


@app.post("/api/eval/cases/<cid>/copy")
def eval_copy_case(cid):
    b = _body()
    try:
        c = eval_mod.copy_case(cid, with_name=b.get("withName"))
        return jsonify({"ok": True, "case": c})
    except ValueError as e:
        return _fail(str(e))


@app.post("/api/eval/cases/<cid>/run")
def eval_run_case(cid):
    """单条评测：走与 /demo 相同的 runner.run_run 主链路 → 评分，结果持久进该 Run 记录。

    模型/API/工具故障时不伪装成功：run.status=error → score.status=ERROR（不计入产品质量 FAIL）。
    """
    c = eval_mod.get_case(cid)
    if not c:
        return _fail("评测用例不存在", 404)
    b = _body()
    question = (b.get("question") or "").strip() or (c.get("question") or "").strip()
    if not question:
        return _fail("用例缺少 question，无法执行", 404)
    try:
        rec = runner.run_run(question, source="eval",
                             conversation_id=f"eval:{cid}",
                             force_error_tool=b.get("forceErrorTool")
                             or b.get("force_error_tool") or None)
    except Exception as e:  # runner 兜底后理论上不抛，双保险仍落 ERROR 评分
        score = eval_mod.score_case(c, {"id": cid, "status": "error", "error": str(e)})
        return jsonify({"ok": False, "runId": cid, "run": None, "score": score,
                        "error": str(e)})
    score = eval_mod.score_case(c, rec)
    try:
        store.save_run_field(rec.get("id"), {"eval": score})
    except Exception:
        pass
    return jsonify({"ok": rec.get("status") != "error", "runId": rec.get("id"),
                    "run": rec, "score": score})


@app.post("/api/eval/batch-run")
def eval_batch_run():
    """回归批跑：逐条走真实主链路评分。默认跑全部 enabled（<=20 条）。"""
    b = _body()
    case_ids = b.get("caseIds") or []
    all_cases, _total = eval_mod.list_cases(limit=2000)
    by_id = {c.get("id"): c for c in all_cases}
    if not case_ids:
        case_ids = eval_mod.default_enabled_set()
    case_ids = [cid for cid in case_ids[:20] if cid in by_id]
    if not case_ids:
        return _fail("没有可运行的评测用例", 400)
    force = b.get("forceErrorTool") or b.get("force_error_tool") or None
    results, items = [], []
    for cid in case_ids:
        c = by_id[cid]
        try:
            rec = runner.run_run(c.get("question") or "", source="eval",
                                 conversation_id=f"eval:{cid}",
                                 force_error_tool=force)
        except Exception as e:
            score = eval_mod.score_case(c, {"id": cid, "status": "error", "error": str(e)})
        else:
            score = eval_mod.score_case(c, rec)
            try:
                store.save_run_field(rec.get("id"), {"eval": score})
            except Exception:
                pass
        item = {**score, "caseId": cid, "name": c.get("name"),
                "category": c.get("category"), "question": c.get("question")}
        item["runId"] = item.get("runId") or (rec.get("id") if isinstance(rec, dict) else None)
        items.append(item)
        results.append(score)
    return jsonify({"ok": True, "results": items,
                    "summary": eval_mod.summarize_results(results)})


# ================================================================ 评测批次（持久化）+ 版本对比
@app.post("/api/eval/batch/run")
def eval_batch_create():
    """创建持久化评测批次（不传 caseIds=全部 enabled；[]/无效/重复 id →400；有 active→409）。"""
    b = _body()
    try:
        batch = eval_batch_mod.create_batch(
            case_ids=b.get("caseIds"),
            name=b.get("name") or "",
            version_label=b.get("versionLabel") or b.get("version_label") or "",
            change_note=b.get("changeNote") or b.get("change_note") or "",
            force_error_tool=b.get("forceErrorTool") or b.get("force_error_tool") or None,
        )
        return jsonify({"ok": True, "batchId": batch["id"], "batch": batch})
    except eval_batch_mod.ActiveBatchError as e:
        return jsonify({"ok": False, "error": str(e),
                        "activeBatchId": e.active_batch_id}), 409
    except ValueError as e:
        return _fail(str(e), 400)


@app.get("/api/eval/batch")
def eval_batch_list():
    batches = eval_batch_mod.list_batches(limit=200)
    return jsonify({
        "ok": True,
        "batches": batches,
        "total": len(batches),
        "activeBatchId": eval_batch_mod.active_batch_id(),
        "lastCaseIds": eval_batch_mod.last_case_ids(),
        "evaluator": eval_mod.evaluator_snapshot(),
    })


@app.get("/api/eval/batch/compare")
def eval_batch_compare():
    a = (request.args.get("a") or "").strip()
    b = (request.args.get("b") or "").strip()
    if not a or not b:
        return _fail("需要 a 与 b 两个批次 id（GET /api/eval/batch/compare?a=<id>&b=<id>）", 400)
    try:
        return jsonify({"ok": True, **eval_batch_mod.compare_batches(a, b)})
    except eval_batch_mod.BatchNotFoundError as e:
        return _fail(str(e), 404)


@app.get("/api/eval/batch/<bid>")
def eval_batch_detail(bid):
    batch = eval_batch_mod.get_batch(bid)
    if not batch:
        return _fail("评测批次不存在", 404)
    return jsonify({"ok": True, "batch": batch,
                    "activeBatchId": eval_batch_mod.active_batch_id()})


@app.post("/api/eval/batch/<bid>/cancel")
def eval_batch_cancel(bid):
    try:
        batch = eval_batch_mod.cancel_batch(bid)
        return jsonify({"ok": True, "batch": batch})
    except eval_batch_mod.BatchNotFoundError as e:
        return _fail(str(e), 404)
    except ValueError as e:
        return _fail(str(e), 400)


# ---------------------------------------------------------------- 演示重置（受控）
@app.post("/api/system/demo-reset")
def demo_reset_api():
    """恢复演示态到「已知初始态」（仅演示模式放行；有运行中批次拒绝）。"""
    if providers.effective_provider_name() != "demo-fixture":
        return _fail("演示重置仅允许在演示模式（SNACK_LLM_PROVIDER=demo-fixture）下执行；"
                     f"当前为真实 Provider={providers.effective_provider_name()}，已拒绝以防误清。", 403)
    act = eval_batch_mod.active_batch_id()
    if act:
        return jsonify({"ok": False,
                        "error": f"评测批次仍在运行（{act}），请先取消或等待完成后再执行演示重置",
                        "activeBatchId": act}), 409
    try:
        summary = reset_mod.reset_demo(actor="api")
    except Exception as e:
        return _fail(f"演示重置失败：{e}")
    return jsonify(summary)


if __name__ == "__main__":
    cfg = store.load_config()
    host = cfg["app"].get("host", "127.0.0.1")
    port = int(cfg["app"].get("port", 8000))
    demo = (os.environ.get("SNACK_LLM_PROVIDER") or "").strip().lower() in ("demo-fixture", "fixture")
    print("\n==============================================")
    print("  零食电商客服 Agent 主链路平台 已启动")
    print(f"  工作台(Agent): http://{host}:{port}/")
    print(f"  演示页:        http://{host}:{port}/demo")
    print(f"  Runs 详情:     http://{host}:{port}/runs/<runId>")
    print(f"  后台:          http://{host}:{port}/admin")
    provider = (os.environ.get("SNACK_LLM_PROVIDER") or "openai-compatible").strip()
    print(f"  LLM Provider: {provider}（{'演示模式' if demo else '真实模型'}）")
    if not demo and not (cfg["llm"].get("api_key") or os.environ.get("OPENAI_API_KEY") or "").strip():
        print("  [!] 尚未配置 API Key：后台「模型设置」填写，或 export SNACK_LLM_PROVIDER=demo-fixture 切演示")
    print("==============================================\n")
    app.run(host=host, port=port, debug=False, threaded=True)
