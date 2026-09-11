# -*- coding: utf-8 -*-
"""独立 Plan Validator：在真正执行前对 Planner 产出的计划做静态安全/能力校验。

与 Planner/Executor 分离，作为第二道关卡（runner 与 engine.execute 都会先调用）：
- 未知 / 未启用的 Skill/Tool 引用 → 报错；
- 平台硬性必需能力（reply / moderation）未启用 → 报错（不伪装成功）；
- 风险诉求缺「risk」Skill 步骤、或仍要跑销售/算价 Tool → 阻断（blocked）；
- 价格/推荐诉求缺 compute_price、物流/订单诉求缺 query_service → 报错。
同时提供确定性风险词表与场景分类，供 Planner 提示词、runner、演示 Provider 复用。
"""
from __future__ import annotations

# 风险信号词表：命中即视为需要「风险识别与安全回复」Skill 介入（仍由 Planner/Skill 判定，非 if/else 出答案）
RISK_WORDS = [
    "中奖", "领奖", "转账", "手续费", "刷单", "退款到",
    "验证码", "加qq", "扫码", "点链接", "链接", "理赔保证金", "保证金",
]
# 订单 / 物流 / 售后场景词
SERVICE_WORDS = [
    "订单", "物流", "快递", "发货", "售后", "退货", "退款", "配送",
    "签收", "包裹", "查单", "到货", "配送时间", "运费", "运单",
]
# 价格 / 优惠场景词
PRICE_WORDS = [
    "价格", "多少钱", "几块", "便宜", "贵不贵", "满减", "优惠", "折扣",
    "到手价", "怎么算", "划算", "券", "折后", "算一下价",
]
# 推荐类场景词（零食品类 / 送礼 / 尝鲜等）
RECOMMEND_WORDS = [
    "推荐", "买点", "来点", "零食", "坚果", "薯片", "辣条", "巧克力", "饼干",
    "肉脯", "糖果", "送人", "聚会", "送礼", "礼盒", "好吃", "尝尝",
]
# 投诉 / 不满 / 要求转人工 词：命中即按"致歉并转人工核实"语义处理。
# 注意：仅供演示 Provider 与"service 是否强制查单"的判定复用，不改 classify_scenario 语义。
COMPLAINT_WORDS = [
    "投诉", "举报", "态度差", "态度不好", "找领导", "找主管", "见主管", "差评",
    "不给解决", "不给我解决", "一直不解决", "敷衍", "人工客服", "转人工", "转接人工",
    "投诉你们", "要投诉",
]
# 属于"销售 / 算价"性质的 Tool：风险诉求中不应执行
SALES_TOOLS = {"query_products", "query_activities", "query_coupons", "compute_price"}
# 平台硬性必需能力：任何一次执行都必须能产出"客服话术 + 风控审核"
MANDATORY_SKILLS = {"reply", "moderation"}


def detect_risk(text) -> list:
    """返回文本命中的风险词（列表）。空文本或未命中返回 []。"""
    t = (text or "").lower()
    hits = []
    for w in RISK_WORDS:
        if w in t:
            hits.append(w)
    return hits


def looks_complaint(text) -> bool:
    """问题是否带投诉 / 不满 / 要求转人工语义（命中任一即 True）。"""
    t = text or ""
    return any(w in t for w in COMPLAINT_WORDS)


def classify_scenario(question) -> str:
    """把用户问题归类为 risk / service / price / recommend / other（用于校验与演示剧本）。"""
    q = (question or "").lower()
    if detect_risk(q):
        return "risk"
    if any(w in q for w in SERVICE_WORDS):
        return "service"
    if any(w in q for w in PRICE_WORDS):
        return "price"
    if any(w in q for w in RECOMMEND_WORDS):
        return "recommend"
    return "other"


def _enabled_sets(abilities):
    skills, tools_meta = (abilities or {}).get("skills") or [], (abilities or {}).get("tools") or []
    return {s.get("id") for s in skills}, {t.get("id") for t in tools_meta}


def validate_plan(plan, abilities, *, question=None, risk_signal=None):
    """校验 Planner 产出的计划。返回 {ok, errors:[{code,severity,detail}], warnings, degraded}。"""
    steps = (plan or {}).get("steps") or []
    errors, warnings = [], []
    en_skills, en_tools = _enabled_sets(abilities)

    # ---- 引用存在性：步骤只能引用存在且启用的能力（红线，不应出现，出现即双保险报错）
    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict):
            errors.append({"code": "bad_step", "severity": "error",
                           "detail": f"步骤{i} 不是合法对象，无法执行"})
            continue
        stype, sid = st.get("type"), st.get("id")
        if stype == "skill":
            if sid not in en_skills:
                errors.append({"code": "disabled_or_unknown_skill", "severity": "error",
                               "detail": f"计划步骤调用了不存在或未启用的 Skill：{sid}"})
        elif stype == "tool":
            if sid not in en_tools:
                errors.append({"code": "disabled_or_unknown_tool", "severity": "error",
                               "detail": f"计划步骤调用了不存在或未启用的 Tool：{sid}"})
        else:
            errors.append({"code": "bad_type", "severity": "error",
                           "detail": f"步骤{i} type 只能是 skill/tool，实际为 {stype}"})

    skill_steps = {st.get("id") for st in steps if isinstance(st, dict) and st.get("type") == "skill"}
    tool_steps = {st.get("id") for st in steps if isinstance(st, dict) and st.get("type") == "tool"}

    # ---- 空计划不可执行
    if not steps:
        errors.append({"code": "empty_plan", "severity": "error",
                       "detail": "计划为空，没有任何可执行步骤"})

    # ---- 平台硬性必需能力必须启用（否则无法产出"过审的最终客服话术"）
    for sid in MANDATORY_SKILLS:
        if sid not in en_skills:
            errors.append({"code": "mandatory_disabled", "severity": "blocked",
                           "detail": f"必备 Skill「{sid}」当前被禁用，任何回复都无法安全送审；请先在后台启用再提问"})

    # ---- 显式要求的能力必须出现在步骤里（mandatoryCapabilities，与 PLAN_SYSTEM 规则 6 对齐）
    plan_mandatory = (plan or {}).get("mandatoryCapabilities") or []
    for cid in plan_mandatory:
        if cid in skill_steps or cid in tool_steps:
            continue
        errors.append({"code": "mandatory_missing", "severity": "error",
                       "detail": f"本次运行被要求必须包含能力「{cid}」（mandatoryCapabilities），但计划 steps 未引用它"})

    # ---- 风险诉求：必须含 risk 步骤且不得执行销售/算价 Tool（blocked）
    risk_hits = list(risk_signal or [])
    if not risk_hits and question:
        risk_hits = detect_risk(question)
    if risk_hits:
        if "risk" not in skill_steps:
            errors.append({"code": "missing_risk", "severity": "blocked",
                           "detail": "检测到风险诉求（中奖/转账/验证码等），但计划缺少「风险识别与安全回复」Skill 步骤"})
        sales_in_risk = tool_steps & SALES_TOOLS
        if sales_in_risk:
            errors.append({"code": "sales_in_risk", "severity": "blocked",
                           "detail": "风险诉求不应执行销售/算价 Tool：" + "、".join(sorted(sales_in_risk))})

    # ---- 价格 / 推荐诉求必须走 compute_price（金额只能由 Tool 精确计算）
    scenario = "risk" if risk_hits else (classify_scenario(question) if question else "other")
    if scenario in ("price", "recommend") and "compute_price" not in tool_steps:
        errors.append({"code": "missing_price_tool", "severity": "error",
                       "detail": "涉及价格/推荐给出到手价的诉求，计划缺少 compute_price 计算价格 Tool（禁止模型口算金额）"})
    # ---- 订单 / 物流 / 售后诉求必须查真实服务数据
    #     投诉/转人工诉求例外：回复仅致歉并转人工核实，不陈述具体订单/物流状态，故不强制查单
    #     （否则"物流 + 投诉"的问题会被硬性要求 query_service，无法走转人工话术）。
    if scenario == "service":
        complaint_q = bool(question) and looks_complaint(question)
        if not complaint_q and "query_service" not in tool_steps:
            errors.append({"code": "missing_service_tool", "severity": "error",
                           "detail": "订单/物流/售后诉求，计划缺少 query_service 查询服务 Tool"})
        elif complaint_q and "query_service" not in tool_steps:
            warnings.append({"code": "complaint_escalation",
                             "detail": "投诉/转人工诉求：按致歉并转人工客服处理，不强制查询服务数据"})

    if scenario == "risk":
        warnings.append({"code": "risk_scenario", "detail": "本诉求按风险流程处理，不执行任何销售/收款/链接操作"})

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "degraded": bool(errors),
        "scenario": scenario,
        "risk_hits": risk_hits,
    }
