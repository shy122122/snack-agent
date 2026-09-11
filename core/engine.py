# -*- coding: utf-8 -*-
"""Agent 编排引擎：Planner 生成计划 → Executor 按计划执行 Skill/Tool。

关键规则（本模块用代码强制保证，不依赖模型自觉）：
1. 计划步骤的 id 必须存在且启用，否则该步骤会被丢弃并记录警告；
2. 商品/活动/优惠券/金额一律来自 Tool 的真实输出，涉及"推荐/理由/算价"依赖真实数据；
3. 发给用户的最终回复必须来自「客服话术生成」Skill，并在发出前经过「风控审核」Skill；
4. 对回复做"价格真实性"代码审计：文本里的金额必须能在真实数据中找到，否则判为高风险并驳回。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, date

from . import llm as llm_mod
from . import store, tools, validator as validator_mod

PLAN_SYSTEM = """你是「零食电商客服」平台的执行规划器。用户提出问题，你需要基于当前"已启用"的能力（Skills 与 Tools）编排可执行的分步计划。

输入中会给出 abilities（已启用能力清单），可能还有 conversation_history（历史）、mandatory_capabilities（本问题必须具备的能力清单）、risk_signal（命中的风险词）。

严格硬性规则：
1) 价格计算、满减/折扣/第二件半价活动、优惠券信息、商品选择与推荐，都必须调用对应 Tool（query_products / query_activities / query_coupons / compute_price），禁止由你推算或编造任何金额、商品、活动或优惠券；凡涉及推荐商品给出到手的，必须安排 compute_price（价格只能由 Tool 精确计算）；
2) 用户问订单/物流/发货/售后/退货/退款时，必须安排 query_service Tool 查真实服务数据，禁止臆测时效、承诺赔付；
3) 若命中风险词（中奖/领奖/转账/手续费/刷单/退款到/验证码/加QQ/扫码/链接/理赔保证金等，输入里有 risk_signal）：本问题只需安排「risk」Skill 步骤（风险识别与安全回复），且绝不安排任何查询商品/活动/优惠券/算价/推荐等销售 Tool；由引擎在识别到风险后自动走安全话术 + 风控；
4) 发给用户的最终回复必须由「客服话术生成」Skill 生成，并在其后紧接一个「风控审核」Skill 步骤审核该回复；
5) 涉及推荐商品时：先「用户需求结构化」→ 查询商品/活动/优惠券 →「商品推荐决策」→「计算价格」→「推荐理由生成」→ 客服话术 → 风控审核；
6) mandatory_capabilities（若给出）里的每个能力 id 必须出现在 steps 中（否则本轮校验会失败）；
7) 只安排必要的步骤，别让流程过长；若当前能力无法满足（比如缺少必要 Skill/Tool），在 summary 中说明并走"结构化 + 客服话术 + 风控审核"诚实答复，必要时提示转人工。

输出：只输出一个 JSON 对象（不要 markdown、不要解释）：
{"summary": "一句话说明本计划思路", "reasoning": "选这些步骤的简要理由(可选)", "steps": [{"type": "skill|tool", "id": "能力id", "purpose": "本步骤目的", "inputs": {...}}]}

steps 字段约定：
- type 为 skill 或 tool；id 必须来自 abilities（只能选已启用的能力）；
- inputs 用引用表示需要的数据（也可省略，引擎会自动按需补全）：
  $question=用户原问题；$needs=需求结构化输出；$products=真实商品；$activities=真实活动；$coupons=真实优惠券；
  $selection=推荐决策；$price=算价结果；$reasons=推荐理由；$reply=客服话术草稿。
参考顺序样例（推荐类问题）：
needs(skill) → query_products(tool) → query_activities(tool) → query_coupons(tool) → recommend(skill) → compute_price(tool) → reason(skill) → reply(skill) → moderation(skill)"""

# 每个 skill 完成后写入 ctx 的产物别名（引擎内部数据总线）
SKILL_ARTIFACT = {
    "needs": "needs",
    "recommend": "selection",
    "reason": "reasons",
    "reply": "reply",
    "risk": "risk",
    "moderation": "moderation",
}

# 每个 skill 运行时按需自动注入的上下文（param -> ctx 引用），保证"真实数据不被绕过"
SKILL_AUTO_INPUT = {
    "needs": {"user_input": "$question"},
    "recommend": {"needs": "$needs", "products": "$products", "activities": "$activities", "coupons": "$coupons"},
    "reason": {"selection": "$selection", "products": "$products", "price": "$price", "user_input": "$question"},
    "reply": {"needs": "$needs", "selection": "$selection", "products": "$products",
              "price": "$price", "reasons": "$reasons", "service": "$service", "user_input": "$question"},
    "risk": {"user_input": "$question"},
    "moderation": {"reply": "$reply", "user_input": "$question"},
}

# tool 缺省参数自动填充
TOOL_AUTO_INPUT = {
    "query_products": {"keywords": "$needs.query_keywords", "category": "$needs.category", "max_price": "$needs.budget"},
    "query_activities": {"category": "$needs.category"},
    "query_coupons": {"category": "$needs.category"},
    "compute_price": {},
}


class EngineError(Exception):
    pass


class _Missing:
    pass


_MISSING = _Missing()


def _purge_missing(value):
    """递归剔除 _MISSING 占位符：大模型规划器可能给出"嵌套引用"（如
    {"items":[{"product_id":"$selection.product_id"}]}），解析不到时 _MISSING 会藏在
    列表/字典内部，顶层删除逻辑抓不到，残留会导致 jsonify / json.dumps 抛错。"""
    if value is _MISSING:
        return None
    if isinstance(value, dict):
        return {k: _purge_missing(v) for k, v in value.items() if v is not _MISSING}
    if isinstance(value, (list, tuple)):
        return [_purge_missing(v) for v in value if v is not _MISSING]
    return value


def _resolve_ref(value, ctx):
    """将 $xxx 或 $xxx.yyy 引用解析成 ctx 中的真实对象；无法解析返回 _MISSING。"""
    if isinstance(value, str) and value.startswith("$"):
        node = ctx
        for part in value[1:].split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return _MISSING
        return node
    if isinstance(value, list):
        return [_resolve_ref(x, ctx) for x in value]
    if isinstance(value, dict):
        return {k: _resolve_ref(v, ctx) for k, v in value.items()}
    return value


def build_abilities():
    """返回当前 Planner 可用的能力清单（只含启用项）。"""
    skills, tools_meta = store.load_skills(), tools.META
    return {
        "skills": [
            {"id": s["id"], "name": s["name"], "description": s.get("description", ""), "role": s.get("role", "")}
            for s in skills if s.get("enabled")
        ],
        "tools": [
            {"id": m["id"], "name": m["name"], "description": m["description"], "params": m["params"], "artifact": m["artifact"]}
            for m in tools_meta if store.tool_enabled(m["id"])
        ],
    }


# 后台展示用：某个 Skill 要产出可靠结果，其上游通常需要哪些 Tool 查得真实数据。
# 仅作界面指引（recommend/reason 强依赖真实数据源），风险类无需销售 Tool。
SKILL_DEP_TOOLS = {
    "needs": [],
    "recommend": ["query_products", "query_activities", "query_coupons"],
    "reason": ["query_products", "compute_price"],
    "reply": ["compute_price"],
    "risk": [],
    "moderation": [],
}
# 反查：某 Tool 供哪些 Skill 消费（reason 需要 compute_price 的价格、reply 需要等）
_TOOL_FED_SKILLS = {}
for _sid, _tds in SKILL_DEP_TOOLS.items():
    for _tid in _tds:
        _TOOL_FED_SKILLS.setdefault(_tid, []).append(_sid)


def planner_system_prompt() -> str:
    """Planner 当前生效的系统提示词：后台自定义(persisted)优先，否则内置 PLAN_SYSTEM。"""
    try:
        custom = (store.load_planner_config() or {}).get("prompt") or ""
    except Exception:
        custom = ""
    return custom.strip() or PLAN_SYSTEM


# ---------------------------------------------------------------------------- Planner
def plan(question: str, llm_fn=None, *, conversation_history=None,
         mandatory_capabilities=None, risk_signal=None):
    """Planner：产出结构化 Plan。

    risk_signal 缺省时按确定性风险词表自动检测（中奖/转账/验证码等）；命中后仍由
    Planner 决定步骤，只是把风险信号与 mandatory_capabilities 一并告知模型与校验器。
    返回 Plan 同时保留 steps/summary 供旧前台/admin 兼容，并新增 selectedSkills/
    selectedTools/reasoning/mandatoryCapabilities/risk 等字段。
    """
    question = (question or "").strip()
    if not question:
        raise EngineError("请输入客服问题。")
    if risk_signal is None:
        risk_signal = validator_mod.detect_risk(question)
    abilities = build_abilities()
    llm_fn = llm_fn or llm_mod.call_llm
    payload = {"user_question": question, "abilities": abilities}
    if conversation_history:
        payload["conversation_history"] = conversation_history
    mandatory = list(mandatory_capabilities or [])
    if mandatory:
        payload["mandatory_capabilities"] = mandatory
    if risk_signal:
        payload["risk_signal"] = {"matched": list(risk_signal)}

    messages = [
        {"role": "system", "content": planner_system_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    call_kw = dict(messages=messages, json_mode=True, temperature=0.2, max_tokens=1800)
    if llm_fn is llm_mod.call_llm:
        call_kw["provider_hint"] = "planner"
    try:
        resp = llm_fn(**call_kw)
    except llm_mod.LLMError as e:
        raise EngineError(f"规划失败：{e}")
    content = resp["content"]
    try:
        obj = llm_mod.extract_json(content)
    except llm_mod.LLMError as e:
        raise EngineError(f"Planner 输出无法解析：{e}")

    raw_steps = obj.get("steps") if isinstance(obj, dict) else None
    if not isinstance(raw_steps, list):
        raise EngineError("Planner 未返回 steps 数组。")
    steps, warnings = _validate_steps(raw_steps)
    if not steps:
        raise EngineError("规划结果中没有可用步骤（能力可能被禁用或全部无效）。" + ("；".join(warnings)))

    skill_ids = [st["id"] for st in steps if st.get("type") == "skill"]
    tool_ids = [st["id"] for st in steps if st.get("type") == "tool"]
    selected_skills, selected_tools = [], []
    for sid in skill_ids:
        if sid not in selected_skills:
            selected_skills.append(sid)
    for tid in tool_ids:
        if tid not in selected_tools:
            selected_tools.append(tid)

    risk_hits = list(risk_signal or [])
    reasoning = obj.get("reasoning") or ("按风险流程处理" if risk_hits else obj.get("summary", ""))
    return {
        "summary": obj.get("summary", ""),
        "steps": steps,
        "selectedSkills": selected_skills,
        "selectedTools": selected_tools,
        "reasoning": reasoning,
        "mandatoryCapabilities": mandatory,
        "risk": {"isRisky": bool(risk_hits), "matched": risk_hits,
                 "note": ("命中风险词：" + "、".join(risk_hits)) if risk_hits else ""},
        "warnings": warnings,
        "abilities": abilities,
        "usage": resp.get("usage", {}),
        "latency_ms": resp.get("latency_ms", 0),
        "cost_yuan": resp.get("cost_yuan", 0),
        "model": resp.get("model", ""),
        "provider": resp.get("provider", ""),
    }


def _validate_steps(raw_steps):
    """校验步骤引用的能力存在且启用；非法步骤剔除并给警告。返回 (steps, warnings)。"""
    skills = {s["id"]: s for s in store.load_skills()}
    enabled_tools = {m["id"] for m in tools.META if store.tool_enabled(m["id"])}
    enabled_skills = {sid for sid, s in skills.items() if s.get("enabled")}
    ok, warnings = [], []
    for i, st in enumerate(raw_steps):
        if not isinstance(st, dict):
            warnings.append(f"步骤{i + 1} 格式非法已忽略")
            continue
        stype, sid = st.get("type"), st.get("id")
        if stype == "skill":
            if sid in enabled_skills:
                ok.append(st)
            else:
                warnings.append(f"步骤 {sid or '?'} 调用了不存在或被禁用的 Skill，已剔除")
        elif stype == "tool":
            if sid in enabled_tools:
                ok.append(st)
            else:
                warnings.append(f"步骤 {sid or '?'} 调用了不存在或被禁用的 Tool，已剔除")
        else:
            warnings.append(f"步骤{i + 1} type 只能是 skill/tool，已忽略")
    return ok, warnings


def _sanitize_selection(ctx):
    """推荐决策只允许使用真实商品/券/活动：非法 id 一律由代码剔除（防大模型编造）。"""
    sel = ctx.get("selection")
    if not isinstance(sel, dict):
        return
    notes = ctx.setdefault("_notes", [])
    products = ctx.get("products")
    if isinstance(products, list) and products:
        ids = {p["id"] for p in products}
        raw = sel.get("items")
        if isinstance(raw, list):
            kept = [it for it in raw if isinstance(it, dict) and it.get("product_id") in ids]
            if len(kept) != len(raw):
                notes.append(f"推荐决策含 {len(raw) - len(kept)} 个非真实商品 id，已被代码剔除")
            sel["items"] = kept
    coupons, acts = ctx.get("coupons"), ctx.get("activities")
    cp = sel.get("coupon_id")
    if cp and isinstance(coupons, list) and cp not in {c["id"] for c in coupons}:
        notes.append(f"推荐决策中的优惠券 {cp} 不是真实优惠券，已被代码清除")
        sel["coupon_id"] = None
    act = sel.get("activity_id")
    if act and isinstance(acts, list) and act not in {a["id"] for a in acts}:
        notes.append(f"推荐决策中的活动 {act} 不是真实活动，已被代码清除")
        sel["activity_id"] = None


# ---------------------------------------------------------------------------- Executor
def _skill_output_json_mode(skill_id):
    # 客服话术输出纯文本，其余 Skill 输出 JSON
    return skill_id != "reply"


def _call_one_skill(skilldef, inputs, ctx, note_log):
    """真正执行一次 Skill 的大模型调用。返回 (content, parsed, usage, latency)。"""
    mp = skilldef.get("model_params") or {}
    skill_id = skilldef["id"]
    json_mode = _skill_output_json_mode(skill_id)
    # 风控需要"允许出现的真实数据"供模型比对
    if skill_id == "moderation" and "facts" not in inputs:
        inputs = dict(inputs)
        inputs["facts"] = _build_facts(ctx)
    messages = [
        {"role": "system", "content": skilldef.get("prompt", "")},
        {"role": "user", "content": "输入(JSON)：\n" + json.dumps(inputs, ensure_ascii=False)},
    ]
    temperature = mp.get("temperature")
    max_tokens = mp.get("max_tokens")
    try:
        resp = llm_mod.call_llm(
            messages, json_mode=json_mode,
            model=mp.get("model") or None, temperature=temperature, max_tokens=max_tokens,
            provider_hint=skill_id,
        )
    except llm_mod.LLMError as e:
        raise
    content = resp["content"]
    parsed = None
    if json_mode:
        try:
            parsed = llm_mod.extract_json(content)
        except llm_mod.LLMError:
            parsed = {"raw": content}
            note_log.append("模型返回不是合法 JSON，已按原样保存")
    else:
        parsed = content
    return content, parsed, resp["usage"], resp["latency_ms"], resp.get("model"), resp.get("cost_yuan", 0)


def _build_skill_inputs(skill_id, step_inputs, ctx, note_log):
    inputs = _resolve_ref(step_inputs or {}, ctx)
    if not isinstance(inputs, dict):
        inputs = {}
    # 按需自动补全，避免关键真实数据被漏传
    for param, ref in (SKILL_AUTO_INPUT.get(skill_id) or {}).items():
        if param in inputs:
            continue
        v = _resolve_ref(ref, ctx)
        if v is not _MISSING:
            inputs[param] = v
        else:
            note_log.append(f"上下文缺少 {ref}，Skill 输入 {param} 未注入")
    # 过滤残留的未解析引用
    dropped = [k for k, v in inputs.items() if v is _MISSING]
    for k in dropped:
        inputs.pop(k)
        note_log.append(f"输入参数 {k} 引用的数据未生成，已忽略")
    return _purge_missing(inputs)


def _build_tool_params(tool_id, step_inputs, ctx, note_log):
    inputs = _resolve_ref(step_inputs or {}, ctx)
    if not isinstance(inputs, dict):
        inputs = {}
    if tool_id == "compute_price":
        sel = ctx.get("selection")
        if isinstance(sel, dict):
            if "items" not in inputs:
                items = sel.get("items") or []
                if items:
                    inputs["items"] = [
                        {"product_id": it.get("product_id"), "qty": it.get("qty", 1)}
                        for it in items if it.get("product_id")
                    ]
            inputs.setdefault("coupon_id", sel.get("coupon_id"))
            inputs.setdefault("activity_id", sel.get("activity_id"))
    else:
        for param, ref in (TOOL_AUTO_INPUT.get(tool_id) or {}).items():
            if param in inputs:
                continue
            v = _resolve_ref(ref, ctx)
            if v is not _MISSING and v is not None and v != []:
                inputs[param] = v
    dropped = [k for k, v in inputs.items() if v is _MISSING]
    for k in dropped:
        inputs.pop(k)
        note_log.append(f"Tool 参数 {k} 引用未生成，已忽略")
    return _purge_missing(inputs)


def execute(question: str, plan_obj, llm_fn=None, db=None, on_step=None,
            force_error_tool=None):
    db = db or store.load_database()
    steps = (plan_obj or {}).get("steps") or []
    if not steps:
        raise EngineError("计划为空，无法执行。")
    skills = {s["id"]: s for s in store.load_skills()}

    # ---- 独立 Plan Validator：不合法则直接停止，绝不假装执行成功 ----
    risk_hits = validator_mod.detect_risk(question)
    v = validator_mod.validate_plan(plan_obj, build_abilities(), question=question, risk_signal=risk_hits)
    if not v["ok"]:
        detail = "；".join(e["detail"] for e in v["errors"])
        raise EngineError(f"计划未通过安全校验，已停止执行（不会返回任何编造/未送审结果）：{detail}")

    trace, warnings = [], []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total_cost, total_latency = 0.0, 0
    ctx = {"question": question, "db": db}

    def emit(entry):
        trace.append(entry)
        if on_step:
            try:
                on_step(dict(entry))
            except Exception:
                pass

    # ---------------- 顺序执行计划步骤 ----------------
    for idx, st in enumerate(steps, start=1):
        stype, sid = st.get("type"), st.get("id")
        entry = {
            "index": idx, "stepId": f"{idx}.{stype}.{sid}",
            "type": stype, "id": sid,
            "name": (skills.get(sid, {}).get("name") if stype == "skill" else
                     (tools.get_meta(sid) or {}).get("name")),
            "role": skills.get(sid, {}).get("role", "") if stype == "skill" else "tool",
            "purpose": st.get("purpose", ""), "source": "plan",
            "status": "ok", "note_log": [], "inputs": {}, "output": None, "error": "",
        }
        try:
            if stype == "skill":
                skilldef = skills.get(sid)
                if not skilldef or not skilldef.get("enabled"):
                    raise EngineError(f"Skill {sid} 不存在或已被禁用")
                inputs = _build_skill_inputs(sid, st.get("inputs"), ctx, entry["note_log"])
                content, parsed, usage, lat, model, cost = _call_one_skill(skilldef, inputs, ctx, entry["note_log"])
                entry.update(inputs=inputs, output=parsed, output_text=content,
                             usage={**usage, "model": model}, latency_ms=lat, cost_yuan=cost)
                if sid in SKILL_ARTIFACT:
                    ctx[SKILL_ARTIFACT[sid]] = parsed
                if sid == "recommend":
                    _sanitize_selection(ctx)
                if sid == "reply":
                    ctx["reply"] = parsed if isinstance(parsed, str) else content
                total_usage = _add_usage(total_usage, usage)
                total_cost += cost
                total_latency += lat
                # 风险命中：不再执行任何后续销售/收款/链接操作，剩余步骤标记为跳过
                if sid == "risk" and isinstance(parsed, dict) and parsed.get("isRisky"):
                    ctx["_risk"] = parsed
                    emit(entry)
                    for j, rem in enumerate(steps[idx:], start=idx + 1):
                        rtype, rid = rem.get("type"), rem.get("id")
                        sname = (skills.get(rid, {}).get("name") if rtype == "skill" else
                                 (tools.get_meta(rid) or {}).get("name")) or rid
                        skip = {
                            "index": j, "stepId": f"{j}.{rtype}.{rid}",
                            "type": rtype, "id": rid, "name": sname,
                            "role": skills.get(rid, {}).get("role", "") if rtype == "skill" else "tool",
                            "purpose": rem.get("purpose", ""), "source": "plan",
                            "status": "skipped", "note_log": ["风险命中(isRisky)，按安全策略跳过该步骤"],
                            "inputs": {}, "output": None, "error": "", "latency_ms": 0,
                        }
                        emit(skip)
                    break
            elif stype == "tool":
                meta = tools.get_meta(sid)
                if not meta or not store.tool_enabled(sid):
                    raise EngineError(f"Tool {sid} 不存在或已被禁用")
                params = _build_tool_params(sid, st.get("inputs"), ctx, entry["note_log"])
                if force_error_tool and sid == force_error_tool:
                    raise EngineError(f"人为故障注入：Tool {sid} 被模拟为执行异常")
                try:
                    out = tools.run_tool(sid, params, db)
                except Exception as e:
                    raise EngineError(f"Tool {sid} 执行出错：{e}")
                entry.update(inputs=params, output=out, latency_ms=0)
                if meta["artifact"]:
                    ctx[meta["artifact"]] = out.get(meta["artifact"]) if isinstance(out, dict) and meta["artifact"] in out else out
            else:
                raise EngineError(f"未知步骤类型: {stype}")
        except llm_mod.LLMError as e:
            entry.update(status="error", error=str(e))
            emit(entry)
            return _result(question, plan_obj, trace, warnings, total_usage, total_cost,
                           total_latency, ok=False, error=str(e), blocked_reason=str(e),
                           risk=ctx.get("_risk"))
        except (EngineError, KeyError) as e:
            entry.update(status="error", error=str(e))
            emit(entry)
            return _result(question, plan_obj, trace, warnings, total_usage, total_cost,
                           total_latency, ok=False, error=str(e), blocked_reason=str(e),
                           risk=ctx.get("_risk"))
        emit(entry)

    return _finalize(question, plan_obj, trace, warnings, ctx, total_usage, total_cost,
                     total_latency, on_step=on_step)


# ---------------------------------------------------------------------------- 收尾：话术 + 风控
def _add_usage(total, usage):
    return {
        "prompt_tokens": total["prompt_tokens"] + usage.get("prompt_tokens", 0),
        "completion_tokens": total["completion_tokens"] + usage.get("completion_tokens", 0),
        "total_tokens": total["total_tokens"] + usage.get("total_tokens", 0),
    }


def _collect_replies(trace):
    return [t for t in trace if t.get("type") == "skill" and t.get("id") == "reply" and t.get("output")]


def _result(question, plan_obj, trace, warnings, usage, cost, latency, *,
            ok=True, error=None, final_reply=None, moderation=None, blocked_reason=None,
            risk=None):
    r = {
        "ok": ok, "error": error, "question": question,
        "plan": plan_obj, "warnings": warnings,
        "trace": trace, "steps_count": len(trace),
        "usage": usage, "cost_yuan": round(cost, 5), "latency_ms": latency,
        "final_reply": final_reply,
        "moderation": moderation,
    }
    if blocked_reason:
        r["blocked_reason"] = blocked_reason
    if risk:
        r["risk"] = risk
    return r


def _finalize(question, plan_obj, trace, warnings, ctx, usage, cost, latency, *, on_step=None):
    notes = ctx.pop("_notes", [])
    if notes:
        warnings = list(notes) + list(warnings)
    reply_entries = _collect_replies(trace)
    # 只认"真实执行成功"的风控结果；被风险策略跳过的 moderation（无 output）不算已审核，
    # 否则其 output=None 会被判 pass=False，把风险安全话术误打成 blocked 兜底。
    mod_entries = [t for t in trace if t.get("id") == "moderation" and t.get("status") == "ok"]
    skills = {s["id"]: s for s in store.load_skills()}

    # 风险命中(isRisky)：不进行任何销售/收款/链接操作，直接采用 risk Skill 的安全话术作为
    # 最终文本（仍需经风控审核）。
    risk = ctx.get("_risk") if isinstance(ctx.get("_risk"), dict) else None
    is_risky = bool(risk and risk.get("isRisky"))
    reply_text = ctx.get("reply")
    if is_risky:
        reply_text = str(risk.get("safeReply") or "").strip()
        ctx["reply"] = reply_text

    # 1) 若计划没产出客服话术，但存在可回答的真实内容 → 自动补一次
    if not reply_entries and any(k in ctx for k in ("products", "activities", "coupons", "price", "selection", "needs")):
        skilldef = skills.get("reply")
        if skilldef and skilldef.get("enabled"):
            idx = len(trace) + 1
            inputs = _build_skill_inputs("reply", {"user_input": "$question"}, ctx, [])
            try:
                content, parsed, u, lat, model, c = _call_one_skill(skilldef, inputs, ctx, [])
                entry = {
                    "index": idx, "type": "skill", "id": "reply", "name": skilldef["name"],
                    "role": skilldef.get("role", ""), "purpose": "（引擎自动补全）生成最终客服回复",
                    "source": "auto", "status": "ok", "note_log": [], "inputs": inputs,
                    "output": content, "output_text": content, "usage": {**u, "model": model},
                    "latency_ms": lat, "cost_yuan": c, "error": "",
                }
                trace.append(entry)
                ctx["reply"] = content
                reply_text = content
                warnings.append("计划未包含客服话术步骤，引擎已自动补全最终回复。")
                usage = _add_usage(usage, u)
                cost += c
                latency += lat
            except llm_mod.LLMError as e:
                return _result(question, plan_obj, trace, warnings, usage, cost, latency,
                               ok=False, error=f"自动补全话术失败：{e}",
                               blocked_reason=f"自动补全话术失败：{e}")

    if not reply_text or not str(reply_text).strip():
        return _result(question, plan_obj, trace, warnings, usage, cost, latency,
                       ok=True, final_reply=None,
                       moderation={"pass": False, "level": "high",
                                   "issues": [{"type": "policy", "detail": "本轮未产出可供发送的最终客服回复",
                                               "evidence": "无回复文本"}],
                                   "suggestion": "请重新提问或检查计划是否缺少客服话术步骤。"},
                       blocked_reason="未产出最终客服回复", risk=risk)

    # 2) 若无风控步骤 → 自动追加一个「风控审核」Skill 步骤（强制规则）
    mod_entry = mod_entries[-1] if mod_entries else None
    if not mod_entry:
        skilldef = skills.get("moderation")
        if skilldef and skilldef.get("enabled"):
            inputs = _build_skill_inputs("moderation", {"reply": "$reply"}, ctx, [])
            try:
                content, parsed, u, lat, model, c = _call_one_skill(skilldef, inputs, ctx, [])
                entry = {
                    "index": len(trace) + 1, "type": "skill", "id": "moderation", "name": skilldef["name"],
                    "role": skilldef.get("role", ""), "purpose": "（引擎自动追加）最终回复风控审核",
                    "source": "auto", "status": "ok", "note_log": [], "inputs": inputs,
                    "output": parsed, "output_text": content, "usage": {**u, "model": model},
                    "latency_ms": lat, "cost_yuan": c, "error": "",
                }
                trace.append(entry)
                had_skipped_mod = any(t.get("id") == "moderation" and t.get("status") == "skipped"
                                      for t in trace)
                warnings.append(("计划内的风控步骤因风险策略被跳过，已对最终安全话术重新执行风控审核。"
                                 if had_skipped_mod else
                                 "计划未包含风控审核步骤，引擎已按规则自动追加风控审核。"))
                usage = _add_usage(usage, u)
                cost += c
                latency += lat
                mod_entry = entry
            except llm_mod.LLMError as e:
                return _result(question, plan_obj, trace, warnings, usage, cost, latency,
                               ok=False, error=f"风控审核失败：{e}", blocked_reason=str(e))

    decision = mod_entry.get("output") if mod_entry else None
    decision = _normalize_decision(decision)
    # 叠加代码级"金额真实性"审计
    audit = _audit_price(reply_text, ctx)
    decision = _merge_audit(decision, audit)

    if decision.get("pass"):
        mode = "risk_guard" if is_risky else "pass"
        return _result(question, plan_obj, trace, warnings, usage, cost, latency,
                       ok=True, final_reply={"text": reply_text, "blocked": False, "mode": mode},
                       moderation=decision, risk=risk)

    # 风险场景：安全话术未过审 → 直接拦截，不做常规重写（避免把正常推荐话术回给诈骗诉求）
    if is_risky:
        fallback = ("抱歉，本轮安全回复未通过合规审核，已拦截且不会发送任何转账/领奖指引。"
                    "如遇疑似诈骗请及时报警，或转接人工客服处理。")
        warnings.append("风险场景安全话术未通过风控，已拦截。")
        return _result(question, plan_obj, trace, warnings, usage, cost, latency, ok=True,
                       final_reply={"text": fallback, "blocked": True, "mode": "blocked",
                                    "note": "风险安全话术未过审，已拦截"},
                       moderation=decision, blocked_reason="风险场景安全话术未通过风控",
                       risk=risk)

    # 3) 未通过 → 带风控意见重新生成一次话术并复审
    feedback = {
        "issues": decision.get("issues", []),
        "suggestion": decision.get("suggestion", ""),
        "pass": decision.get("pass"),
    }
    skilldef = skills.get("reply")
    regen_note = []
    if skilldef and skilldef.get("enabled"):
        try:
            idx = len(trace) + 1
            inputs = _build_skill_inputs("reply", {"user_input": "$question"}, ctx, [])
            inputs["moderation_feedback"] = json.dumps(feedback, ensure_ascii=False)
            content, parsed, u, lat, model, c = _call_one_skill(skilldef, inputs, ctx, [])
            entry = {
                "index": idx, "type": "skill", "id": "reply", "name": skilldef["name"],
                "role": skilldef.get("role", ""), "purpose": "（风控驳回后重新生成话术）", "source": "auto",
                "status": "ok", "note_log": [], "inputs": inputs, "output": content, "output_text": content,
                "usage": {**u, "model": model}, "latency_ms": lat, "cost_yuan": c, "error": "",
            }
            trace.append(entry)
            usage = _add_usage(usage, u)
            cost += c
            latency += lat
            regen_note.append("首轮风控未通过，已按审核意见重新生成话术。")
            new_text = content if isinstance(content, str) else str(parsed)
            # 复审
            mdef = skills.get("moderation")
            if mdef and mdef.get("enabled"):
                try:
                    mi = _build_skill_inputs("moderation", {"reply": "$reply"}, dict(ctx, reply=new_text), [])
                    mcontent, mparsed, mu, mlat, mmodel, mc = _call_one_skill(mdef, mi, dict(ctx, reply=new_text), [])
                    entry2 = {
                        "index": len(trace) + 1, "type": "skill", "id": "moderation", "name": mdef["name"],
                        "role": mdef.get("role", ""), "purpose": "（复审）", "source": "auto", "status": "ok",
                        "note_log": [], "inputs": mi, "output": mparsed, "output_text": mcontent,
                        "usage": {**mu, "model": mmodel}, "latency_ms": mlat, "cost_yuan": mc, "error": "",
                    }
                    trace.append(entry2)
                    usage = _add_usage(usage, mu)
                    cost += mc
                    latency += mlat
                    dec2 = _merge_audit(_normalize_decision(mparsed), _audit_price(new_text, ctx))
                    if dec2.get("pass"):
                        return _result(question, plan_obj, trace, warnings, usage, cost, latency, ok=True,
                                       final_reply={"text": new_text, "blocked": False, "mode": "regenerated",
                                                    "note": "；".join(regen_note)},
                                       moderation=dec2)
                    decision = dec2
                except llm_mod.LLMError as e:
                    regen_note.append(f"复审失败：{e}")
        except llm_mod.LLMError as e:
            regen_note.append(f"重新生成失败：{e}")

    # 仍不通过 → 拦截，不把风险话术发出去
    fallback = ("抱歉，本轮自动生成的回复未通过安全合规审核（已拦截，不会发送给用户）。"
                "您可以换个说法重新提问，或转接人工客服处理。")
    warnings.extend(regen_note or ["风控未通过且已拦截最终回复。"])
    return _result(question, plan_obj, trace, warnings, usage, cost, latency, ok=True,
                   final_reply={"text": fallback, "blocked": True, "mode": "blocked",
                                "note": "；".join(regen_note)},
                   moderation=decision, risk=risk,
                   blocked_reason="风控审核未通过，最终回复已被拦截")


def _normalize_decision(parsed):
    if not isinstance(parsed, dict):
        return {"pass": False, "level": "high", "issues": [{"type": "other",
                                                            "detail": "风控模型输出无法解析", "evidence": str(parsed)[:120]}],
                "suggestion": "请重新审核"}
    return {
        "pass": bool(parsed.get("pass")),
        "level": parsed.get("level") or ("pass" if parsed.get("pass") else "low"),
        "issues": parsed.get("issues") or [],
        "suggestion": parsed.get("suggestion") or "",
    }


# ---------------------------------------------------------------- 代码级金额审计
_HARD_WORDS = [
    ("price_fabrication", None),
    ("absolute_word", ["全网最低", "最低价", "最便宜", "零风险", "百分百", "100%", "绝对正品", "绝对保证", "第一低价"]),
    ("overpromise", ["明天必达", "今天到", "次日达", "马上到", "保证到货", "肯定发货"]),
]


def _all_numbers_in(value, out):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(round(float(value), 2))
        return
    if isinstance(value, (list, tuple)):
        for x in value:
            _all_numbers_in(x, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in ("sales", "stock", "rating", "qty", "count", "latency_ms"):
                continue
            _all_numbers_in(v, out)


def _build_facts(ctx):
    lines = []
    products = ctx.get("products")
    if isinstance(products, list):
        lines.append("【真实在售商品】")
        for p in products[:12]:
            tags = "、".join((p.get("tags") or [])[:3])
            lines.append(f"- {p.get('id')} {p.get('name')} ¥{p.get('price')}（销量{p.get('sales')}，评分{p.get('rating')}，{p.get('spec', '')}，标签：{tags}）")
    activities = ctx.get("activities")
    if isinstance(activities, list):
        lines.append("【真实活动】")
        for a in activities[:10]:
            lines.append(f"- {a.get('id')} {a.get('name')}（{a.get('description', '')}）")
    coupons = ctx.get("coupons")
    if isinstance(coupons, list):
        lines.append("【真实优惠券】")
        for c in coupons[:10]:
            lines.append(f"- {c.get('id')} {c.get('name')}：满{c.get('condition')}减{c.get('value')}（{c.get('description', '')}）")
    price = ctx.get("price")
    if isinstance(price, dict):
        lines.append("【算价结果】")
        for it in price.get("items", []):
            lines.append(f"- {it.get('name')} ×{it.get('qty')} = ¥{it.get('line_total')}")
        if price.get("activity"):
            act = price["activity"]
            lines.append(f"- 活动{act.get('name')}：{'生效，减¥' + str(act.get('discount')) if act.get('applied') else '未生效'}")
        if price.get("coupon"):
            cp = price["coupon"]
            lines.append(f"- 优惠券{cp.get('name')}：{'抵扣¥' + str(cp.get('discount')) if cp.get('applied') else '未使用（' + cp.get('note', '') + '）'}")
        lines.append(f"- 商品原价小计 ¥{price.get('subtotal')}，活动后 ¥{price.get('after_activity')}，最终到手 ¥{price.get('final_total')}（共节省 ¥{price.get('saved')}）")
    return "\n".join(lines) if lines else "本轮无真实商品/活动/优惠券数据。"


def _text_numbers(text):
    """提取一段文本里的所有数字（含小数），用于把"用户原话里的数字"视为可信可复述值。"""
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text or ""))]


def _audit_price(reply_text, ctx):
    nums = []
    for k in ("products", "activities", "coupons", "price"):
        _all_numbers_in(ctx.get(k), nums)
    allowed = set(round(float(x), 2) for x in nums if x is not None)
    # 用户自己原话里给出的数字（预算/数量/门槛等）允许被客服复述，不算编造
    qtext = str(ctx.get("question") or "")
    allowed.update(round(float(x), 2) for x in _text_numbers(qtext) if x is not None)
    # 真实单价 × 整数件数 = 确定性算术小计（如"两袋=137.8"），视同可引用金额
    prods = ctx.get("products")
    if isinstance(prods, list):
        for p in prods:
            try:
                unit = round(float(p.get("price", 0)), 2)
            except (TypeError, ValueError):
                continue
            for m in range(1, 10):
                allowed.add(round(unit * m, 2))
    patterns = [r"[¥￥]\s*(\d+(?:\.\d+)?)", r"(\d+(?:\.\d+)?)\s*元"]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, str(reply_text)):
            try:
                found.append((round(float(m.group(1)), 2), m.group(0)))
            except ValueError:
                pass
    issues = []
    for val, snippet in found:
        if val not in allowed and val > 0:
            issues.append({"type": "price_fabrication",
                           "detail": f"回复中金额 {snippet}（{val}）在真实数据与用户原话中都不存在，疑似编造价格",
                           "evidence": snippet})
    low_text = str(reply_text)
    for wtype, words in _HARD_WORDS:
        if not words:
            continue
        for w in words:
            if w in low_text:
                # 仅是复述用户原话里的词（如用户自己问"最便宜"）不算我方做绝对化断言
                if w in qtext:
                    continue
                issues.append({"type": wtype, "detail": f"出现合规风险用语：{w}", "evidence": w})
    return {"issues": issues, "price_numbers_checked": len(found)}


def _merge_audit(decision, audit):
    audit_issues = audit.get("issues") or []
    if audit_issues:
        decision = dict(decision)
        decision["issues"] = list(decision.get("issues") or []) + audit_issues
        hard = [i for i in audit_issues if i.get("type") in ("price_fabrication", "absolute_word")]
        if hard:
            decision["pass"] = False
            decision["level"] = "high"
        decision["audit"] = audit
    return decision


# ---------------------------------------------------------------------------- run_full + 独立测试
def run_full(question, llm_fn=None, db=None, log=True):
    pl = plan(question, llm_fn=llm_fn)
    result = execute(question, pl, llm_fn=llm_fn, db=db)
    result["plan"] = pl
    if log:
        _log_run(question, pl, result)
    return result


def _log_run(question, pl, result):
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "plan_summary": (pl or {}).get("summary", ""),
        "steps_count": result.get("steps_count", 0),
        "ok": result.get("ok"),
        "blocked": bool(result.get("blocked_reason") or (result.get("final_reply") or {}).get("blocked")),
        "moderation_pass": bool((result.get("moderation") or {}).get("pass")),
        "cost_yuan": result.get("cost_yuan", 0),
        "final_reply": (result.get("final_reply") or {}).get("text", "")[:400],
        "error": result.get("error"),
    }
    store.append_run_log(entry)


def test_skill(skill_id: str, inputs, llm_fn=None):
    """后台「测试单个 Skill」：用给定输入独立跑一次该 Skill，不做流程约束。"""
    skilldef = store.get_skill(skill_id)
    if not skilldef:
        raise EngineError(f"Skill 不存在: {skill_id}")
    if isinstance(inputs, str):
        inputs = {"user_input": inputs}
    elif "user_input" not in inputs and "question" in inputs:
        inputs = dict(inputs, user_input=inputs["question"])
    if "question" in inputs and "user_input" not in inputs:
        inputs = dict(inputs, user_input=inputs["question"])
    content, parsed, usage, latency, model, cost = _call_one_skill(skilldef, inputs, {}, [])
    return {
        "skill_id": skill_id, "name": skilldef["name"], "inputs": inputs,
        "content": content, "parsed": parsed,
        "usage": {**usage, "model": model}, "latency_ms": latency, "cost_yuan": cost,
    }
