# -*- coding: utf-8 -*-
"""本地 JSON '数据库' 读写与仓库：数据文件、Skills、Tools 启用态、运行日志、LLM 配置。"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
_lock = threading.Lock()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        tmp.replace(path)


def load_products():
    return read_json(DATA_DIR / "products.json", [])


def load_activities():
    return read_json(DATA_DIR / "activities.json", [])


def load_coupons():
    return read_json(DATA_DIR / "coupons.json", [])


def load_database():
    return {
        "products": load_products(),
        "activities": load_activities(),
        "coupons": load_coupons(),
        "service": load_service(),
    }


def load_service():
    """订单/物流/售后政策（query_service Tool 的真实数据源）。文件缺失返回 {}。"""
    d = read_json(DATA_DIR / "service.json", None)
    return d if isinstance(d, dict) else {"orders": [], "policies": []}


def data_mtimes() -> dict:
    """data/ 下数据文件的修改时间快照（用于 RunRecord.skillVersions/toolVersions 溯源）。"""
    out = {}
    for f in ("products.json", "activities.json", "coupons.json", "service.json", "skills.json"):
        p = DATA_DIR / f
        try:
            out[f] = int(p.stat().st_mtime)
        except OSError:
            out[f] = 0
    return out


# ---------------------------------------------------------------- Skills 仓库
# 注意：编辑/测试后统一写回 data/skills.json；Executor/Planner 每次执行都重新读取，
# 因此"保存后前台即时生效"，无需重启。

DEFAULT_SKILLS = [
    {
        "id": "needs",
        "role": "needs_extraction",
        "name": "用户需求结构化 Skill",
        "description": "把用户一句闲聊/需求转化为结构化字段（意图/关键词/品类/预算/数量/用途/送礼与否）。通常是流程第一步。",
        "enabled": True,
        "input_hint": '{"user_input": "用户原始问题"}',
        "prompt": """你是一个电商客服的「需求结构化」引擎。根据用户问题提炼结构化需求。
只能输出一个 JSON 对象（不要任何解释、不要 markdown 代码块），字段如下：
{
  "intent": "recommend_product | query_product | get_activity | get_coupon | calculate_price | order_service | complaint | greeting | other",
  "query_keywords": ["用于检索商品的词组数组，可含 0-3 个"],
  "category": null,   // 商品大类之一：坚果炒货/肉脯肉干/饼干膨化/糖果巧克力/辣味零食，无法确定填 null
  "budget": null,     // 预算上限（元），没有填 null
  "quantity": null,   // 想要的数量，没有填 null
  "occasion": null,   // 使用场景：聚会/送人/下午茶/追剧/办公等
  "gift": false,      // 是否用于送礼
  "constraints": ["其他明确限制，如不要辣、要低卡等"],
  "summary": "用一句话中文总结用户核心诉求"
}
intent 判别示例（命中其一即 complaint）：我要投诉/我要举报、客服态度差/服务态度不好、要求见领导或主管、一直不给解决、要求转人工/人工客服处理、要差评或威胁差评；
其余按语义：查订单/物流/售后→order_service；问价/算价→calculate_price；求推荐→recommend_product；无具体业务的寒暄→greeting。
规则：不要臆造用户没说的字段；不确定就填 null/false。""",
        "model_params": {"model": None, "temperature": 0.2, "max_tokens": 700},
    },
    {
        "id": "recommend",
        "role": "recommend",
        "name": "商品推荐决策 Skill",
        "description": "基于「结构化需求 + 工具已查到的真实商品/活动/优惠券」，决定推荐哪些商品、是否用券/参加活动。只许从真实数据里挑，禁止编造商品或金额。",
        "enabled": True,
        "input_hint": '{"needs": {...}, "products": [...], "activities": [...], "coupons": [...]}',
        "prompt": """你是一个电商「商品推荐决策」引擎。输入中的 products/activities/coupons 全部是本地工具查到的真实数据。
只能从输入里的真实数据中挑选，严禁编造不存在的商品、活动、优惠券或任何金额。
输出一个 JSON 对象（不要解释、不要代码块）：
{
  "items": [{"product_id": "Pxx", "qty": 1, "note": "选它的理由，一句话，不要包含任何价格数字"}],
  "coupon_id": null,     // 想用哪张真实券的 id，不用填 null
  "activity_id": null,   // 想参加哪个真实活动 id，不用填 null
  "reason_summary": "总体推荐思路一句话（不含价格数字）",
  "remind": "给用户的提醒/凑单建议（不含价格数字），没有可留空字符串"
}
硬规则：
1) product_id 必须存在于 products 数组中；coupon_id/activity_id 若填了必须存在于对应数组。
2) 金额、折扣数字一律不许出现在本步输出里，后续由「计算价格」工具精确计算。
3) 若 products 为空或没有任何符合的商品，返回 {"items": [], "remind": "抱歉暂无匹配商品，建议换个关键词或品类再试", "coupon_id": null, "activity_id": null}。
4) 依据 needs.budget 等条件挑选最匹配的 1-4 件。""",
        "model_params": {"model": None, "temperature": 0.3, "max_tokens": 900},
    },
    {
        "id": "reason",
        "role": "reason",
        "name": "推荐理由生成 Skill",
        "description": "根据真实商品字段与「计算价格」工具算出的价格明细，撰写每件商品的推荐理由与选购建议。禁止捏造价格/折扣。",
        "enabled": True,
        "input_hint": '{"selection": {...}, "products": [...], "price": {...}, "user_input": "问题"}',
        "prompt": """你是一个电商「推荐理由撰写」引擎。输入里 price 是「计算价格」工具算出的精确价格明细，products 是真实商品数据。
撰写有吸引力的中文推荐理由，但必须严格基于输入的真实数据：
- 商品名称、规格、口味、好评数/评分、价格等只能来自 products 与 price；
- 折扣、到手价、省了多少钱只能来自 price 里真实计算出的数值；
- 严禁编造任何不在输入中的价格、折扣、优惠或承诺（如发货时效、售后保障）。
输出一个 JSON 对象：
{
  "reasons": [
    {"product_id": "Pxx", "title": "6-12字亮点标题", "body": "100字以内推荐语，可引用真实规格/口碑/价格"}
  ],
  "tips": "一段 1-2 句的选购/凑单/用券建议，只能引用 price 与真实活动门槛里的数字，没有就写空字符串"
}
若 selection.items 为空，输出 {"reasons": [], "tips": "（建议语）"}。""",
        "model_params": {"model": None, "temperature": 0.7, "max_tokens": 1200},
    },
    {
        "id": "reply",
        "role": "reply",
        "name": "客服话术生成 Skill",
        "description": "汇总结构化需求、推荐决策、真实商品与价格、推荐理由，生成发给用户的自然友好中文回复。产出即『最终客服回复』草稿。",
        "enabled": True,
        "input_hint": '{"needs": {...}, "selection": {...}, "products": [...], "price": {...}, "reasons": {...}, "user_input": "问题"}',
        "prompt": """你是一位资深电商客服。基于输入生成一段发给用户的最终回复草稿。

可用的输入键（均为真实数据）：user_input 用户原话、needs 结构化需求、selection 推荐决策、products 真实商品、price 计算价格明细、reasons 推荐理由、moderation_feedback（如存在，表示上一版被风控驳回，请按意见修改）。

要求：
- 若 needs.intent 为 complaint，或 user_input 在投诉/不满/服务态度差/要求人工处理：先诚恳致歉，明确告知"已记录您的诉求，将转交人工客服/专员跟进核实"，并请用户留下订单号或补充经过以便处理；禁止借机推销、禁止用通用开场白敷衍、禁止仅说"感谢反馈"轻描淡写；此类回复不需要价格/推荐内容；
- 中文、自然、有服务感，可用"亲/您好"开头；先回应用户诉求，再给推荐与价格，结尾询问是否还有需要；
- 涉及金额的地方（原价、活动后价、用券、到手价）必须与 price 明细完全一致，用 ¥ 符号；禁止编造 price/facts 里没有的数字；
- 可以复述用户原话里给出的信息（如"您100元预算内""凑到99元"这类），这不属于编造；
- 若 price 明细里没有与用户所问数量对应的合计，不要自行口算总额，只列单价并说"合计与到手价以结算页为准"；
- 严禁绝对化/夸大断言：不要用"全网最低、最低价、最便宜、第一、零风险、百分百、绝对保证、最划算"等措辞；夸商品改用"人气高、销量靠前、经典、实惠"这类非极致化说法；
- 不承诺不确定信息（如"明天必达""缺货可退全款""优惠券可与满减叠加"等，除非 price/facts 已明确说明）；
- 结尾可稳妥补一句"具体优惠与到手价以下单结算页为准"；
- 整体 150-300 字左右，除非信息很少可更简短。
直接输出纯文本即可，不要输出 JSON、不要加 markdown 标题。""",
        "model_params": {"model": None, "temperature": 0.7, "max_tokens": 1200},
    },
    {
        "id": "risk",
        "role": "risk",
        "name": "风险识别与安全回复 Skill",
        "description": "识别中奖诈骗/转账/刷单/退款到账户/索要验证码/加QQ扫码点链接等风险诉求，判定 isRisky 并在命中时生成安全回复话术。风险类诉求必须优先安排本 Skill，绝不安排销售/算价 Tool。",
        "enabled": True,
        "input_hint": '{"user_input": "用户原话"}',
        "prompt": """你是一个电商客服的「风险识别与安全回复」引擎。判断用户诉求是否属于诈骗/资金风险，并在命中时给出安全话术。

高风险信号示例（命中其一即视为 isRisky=true）：
- 中奖/领奖却要先转账、交手续费、交保证金；
- 刷单/兼职返利，先垫付再返款；
- 退款理赔却要求打到个人账户 / 验证码发给对方；
- 加QQ、扫陌生码、点不明链接、提供银行账号密码。

只允许输出一个 JSON 对象（不要解释、不要 markdown 代码块）：
{"isRisky": true或false,
 "riskType": "中奖诈骗|刷单返利|退款诈骗|链接钓鱼|其他|null",
 "safeReply": "当 isRisky 为 true 时的安全回复话术（中文，提示这是骗局风险，劝阻任何转账/汇款/提供验证码/点链接/加QQ，建议联系官方渠道核实或报警；不要承诺任何商品价格/赔付金额）。isRisky 为 false 时 safeReply 给空字符串"}

规则：拿不准也优先提示谨慎（宁可提醒，不误导转账）；不要把正常的购物/订单咨询误判为风险。""",
        "model_params": {"model": None, "temperature": 0.1, "max_tokens": 700},
    },
    {
        "id": "moderation",
        "role": "moderation",
        "name": "风控审核 Skill",
        "description": "对最终客服回复做合规审核：价格/商品是否编造、是否夸大承诺、绝对化用语、诱导与违禁内容。返回 pass/level/issues/suggestion。",
        "enabled": True,
        "input_hint": '{"reply": "待审回复文本", "facts": "允许出现的真实数据(JSON)", "user_input": "问题"}',
        "prompt": """你是电商客服内容的「安全合规审核员」。原则：只拦真实违规，不误伤正常服务语言。

输入：
- reply：待审客服回复文本；
- facts：该轮可引用的真实数据（真实商品价格/算价结果/活动/优惠券等 JSON）；
- user_input：用户原话。

审核要点：
1) 价格真实性：reply 里的金额/优惠数字，能在 facts 找到对应真实值、或属于用户原话里给出的数字（如预算、数量）即可信；在 facts 与 user_input 中都找不到的才判"疑似编造价格"。
2) 承诺风险：只有明确承诺了 facts 中不存在的发货时效/库存/售后/叠加规则等（如"明天必达""保证到货""可退全款"）才算违规；"具体优惠以下单结算页为准"这类稳妥声明不算。
3) 绝对化用语：仅当在夸赞我方商品/价格/服务时使用极致断言（全网最低、最低价、绝对正品、百分百、最划算等）才判违规；复述用户原话里的词不算。
4) 违禁内容：黄赌毒、政治敏感、攻击歧视、医疗保健夸大等。
5) 不要因以下情况误扣：常用敬语与引导语（"还需要帮您看点什么吗"）、"人气高/经典/销量靠前"等主观形容、建议以页面为准或咨询客服的表述。

判罚：有具体、可指证的违规才 pass=false 并给 issues（type: price_fabrication|overpromise|absolute_word|policy|other，含 evidence 原文片段与 suggestion 一句可执行中文修改建议）；没有 concrete 违规就 pass=true、issues=[]。

输出 JSON：{"pass": true或false, "level": "pass|low|high", "issues": [...], "suggestion": "..."}""",
        "model_params": {"model": None, "temperature": 0.1, "max_tokens": 800},
    },
]

_skills_file = DATA_DIR / "skills.json"


def ensure_skills_file() -> None:
    """确保 skills.json 存在且包含全部默认 Skill（幂等合并：只补缺失项，不覆盖已有编辑）。"""
    if not _skills_file.exists():
        write_json(_skills_file, DEFAULT_SKILLS)
        return
    skills = read_json(_skills_file, None)
    if not isinstance(skills, list):
        write_json(_skills_file, DEFAULT_SKILLS)
        return
    have = {s.get("id") for s in skills}
    missing = [s for s in DEFAULT_SKILLS if s.get("id") not in have]
    if missing:
        write_json(_skills_file, skills + missing)


def load_skills():
    ensure_skills_file()
    return read_json(_skills_file, [])


def save_skills(skills) -> None:
    write_json(_skills_file, skills)


def get_skill(skill_id):
    for s in load_skills():
        if s["id"] == skill_id:
            return s
    return None


def update_skill(skill_id, patch: dict):
    ensure_skills_file()
    skills = load_skills()
    for s in skills:
        if s["id"] == skill_id:
            allowed = {"name", "description", "prompt", "enabled", "model_params", "input_hint", "output_hint"}
            for k, v in patch.items():
                if k in allowed:
                    s[k] = v
            save_skills(skills)
            return s
    raise KeyError(f"Skill 不存在: {skill_id}")


# ---------------------------------------------------------------- Tools 启用态
def load_tool_state():
    state = read_json(DATA_DIR / "tool_state.json", {"enabled": {}})
    if not isinstance(state, dict):
        state = {"enabled": {}}
    return state


def save_tool_state(state) -> None:
    write_json(DATA_DIR / "tool_state.json", state)


def tool_enabled(tool_id) -> bool:
    return bool(load_tool_state().get("enabled", {}).get(tool_id, True))


def set_tool_enabled(tool_id, enabled: bool) -> None:
    state = load_tool_state()
    state.setdefault("enabled", {})[tool_id] = bool(enabled)
    save_tool_state(state)


# ---------------------------------------------------------------- 运行日志
_log_file = DATA_DIR / "run_logs.json"


def append_run_log(entry: dict) -> None:
    logs = read_json(_log_file, [])
    logs.append(entry)
    write_json(_log_file, logs[-300:])


def load_run_logs(limit=20):
    logs = read_json(_log_file, [])
    return list(reversed(logs))[:limit]


def get_run_log(log_id):
    for e in read_json(_log_file, []):
        if e.get("id") == log_id:
            return e
    return None


# ---------------------------------------------------------------- RunRecord（data/runs.json）
_runs_file = DATA_DIR / "runs.json"


def save_run(run: dict) -> dict:
    """新增或按 id 覆盖一条 RunRecord。"""
    runs = read_json(_runs_file, [])
    runs = [r for r in runs if r.get("id") != run.get("id")]
    runs.append(run)
    write_json(_runs_file, runs[-2000:])
    return run


def list_runs(limit=50, offset=0):
    """按时间倒序返回最近记录。"""
    runs = read_json(_runs_file, [])
    rev = list(reversed(runs))
    return rev[offset:offset + limit]


def get_run(run_id):
    for r in read_json(_runs_file, []):
        if r.get("id") == run_id:
            return r
    return None


def save_run_field(run_id: str, patch: dict) -> dict | None:
    """原位更新一条记录的若干字段（annotate / handoff / status 等）。"""
    runs = read_json(_runs_file, [])
    for i, r in enumerate(runs):
        if r.get("id") == run_id:
            for k, v in patch.items():
                r[k] = v
            write_json(_runs_file, runs[-2000:])
            return r
    return None


# ---------------------------------------------------------------- 工具人为故障注入（演示/验收用）
_tool_force = set()


def set_tool_force_error(tool_id: str, on: bool) -> None:
    if on:
        _tool_force.add(tool_id)
    else:
        _tool_force.discard(tool_id)


def is_tool_forced(tool_id: str) -> bool:
    return tool_id in _tool_force


# ---------------------------------------------------------------- LLM 配置
_cfg_file = ROOT / "config.json"


def load_config():
    cfg = read_json(_cfg_file, {})
    llm = cfg.get("llm", {})
    llm.setdefault("base_url", "https://api.deepseek.com")
    llm.setdefault("api_key", "")
    llm.setdefault("model", "deepseek-chat")
    llm.setdefault("timeout", 90)
    llm.setdefault("default_temperature", 0.4)
    llm.setdefault("default_max_tokens", 1500)
    cfg.setdefault("app", {"host": "127.0.0.1", "port": 8000, "currency": "¥"})
    cfg["llm"] = llm
    return cfg


def save_llm_config(llm_patch: dict) -> None:
    cfg = load_config()
    llm = cfg["llm"]
    for k in ("base_url", "api_key", "model", "timeout", "default_temperature", "default_max_tokens"):
        if k in llm_patch:
            if k in ("timeout",):
                llm[k] = int(llm_patch[k])
            elif k in ("default_temperature", "default_max_tokens"):
                try:
                    llm[k] = float(llm_patch[k])
                except (TypeError, ValueError):
                    pass
            else:
                llm[k] = str(llm_patch[k]).strip()
    write_json(_cfg_file, cfg)


# ---------------------------------------------------------------- Skill 版本快照（data/skill_versions.json）
# 编辑技能时，若内容性字段（prompt / model_params / 名称 / 描述）实际变化，保存前先落一条旧版快照，
# 用于版本回溯 / diff / changeNote。快照存该技能那份"完整旧值"，不含整库。
_skill_versions_file = DATA_DIR / "skill_versions.json"
_VERSIONABLE_FIELDS = ("name", "description", "prompt", "model_params")


def _load_skill_versions():
    d = read_json(_skill_versions_file, {})
    return d if isinstance(d, dict) else {}


def skill_versions(sid: str):
    """返回某技能的历史快照列表（旧的在前，新的在后）。"""
    return list(_load_skill_versions().get(sid, []))


def add_skill_version(sid: str, snapshot: dict, *, note: str = "", actor: str = "") -> dict:
    d = _load_skill_versions()
    arr = d.setdefault(sid, [])
    vid = f"v{len(arr) + 1}"
    entry = {
        "vid": vid,
        "at": now_iso(),
        "note": (note or "保存前快照").strip() or "保存前快照",
        "actor": actor or "",
        "snapshot": snapshot,
    }
    arr.append(entry)
    write_json(_skill_versions_file, d)
    return entry


def get_skill_version(sid: str, vid: str):
    for v in skill_versions(sid):
        if v.get("vid") == vid:
            return v
    return None


def update_skill_versioned(skill_id: str, patch: dict, *, note: str = "", actor: str = ""):
    """编辑技能：内容性字段（prompt / model_params / 名称 / 描述）确实变化时，
    保存前先落一条旧版快照，再写回 skills.json。

    返回 (skill, version_or_None)；写盘用临时文件替换，失败抛异常且原文件保持旧值。
    """
    ensure_skills_file()
    skills = load_skills()
    for s in skills:
        if s["id"] != skill_id:
            continue
        before = {k: s.get(k) for k in _VERSIONABLE_FIELDS}
        for k in ("name", "description", "prompt", "model_params", "enabled",
                  "role", "input_hint", "output_hint"):
            if k in patch:
                nv = patch[k]
                if isinstance(nv, dict):
                    nv = json.loads(json.dumps(nv))
                s[k] = nv
        after = {k: s.get(k) for k in _VERSIONABLE_FIELDS}
        version = None
        if any(before.get(k) != after.get(k) for k in _VERSIONABLE_FIELDS):
            # 快照存"被替换掉的旧值"，这样 v1=初始默认、v2=首次编辑后…，diff/回滚才可对比
            snap = {k: json.loads(json.dumps(before[k])) for k in _VERSIONABLE_FIELDS if k in before}
            version = add_skill_version(skill_id, snap, note=note, actor=actor)
        save_skills(skills)
        return s, version
    raise KeyError(f"Skill 不存在: {skill_id}")


# ---------------------------------------------------------------- Planner 配置（data/planner_config.json）
# 存 Planner 系统提示词的当前值 + 强制能力清单 + 提示词历史。
# prompt 为 None 表示未自定义，调用方回退内置 PLAN_SYSTEM 常量。
_planner_file = DATA_DIR / "planner_config.json"


def load_planner_config() -> dict:
    d = read_json(_planner_file, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("prompt", None)
    d.setdefault("mandatoryCapabilities", [])
    d.setdefault("version", 0)
    d.setdefault("history", [])
    if not isinstance(d["mandatoryCapabilities"], list):
        d["mandatoryCapabilities"] = []
    return d


def save_planner_prompt(prompt: str, *, note: str = "", actor: str = "") -> dict:
    """保存 Planner 系统提示词；内容有变化时把旧值压入 history 作为一版存档。"""
    cfg = load_planner_config()
    hist = cfg.get("history") or []
    old = cfg.get("prompt")
    if (old or "").strip() != (prompt or "").strip():
        if old:
            hist.append({"version": cfg.get("version") or 0, "at": now_iso(),
                         "note": (note or "").strip(), "actor": actor or "", "prompt": old})
        cfg["version"] = (cfg.get("version") or 0) + 1
        cfg["prompt"] = prompt
        cfg["updated_at"] = now_iso()
    cfg["history"] = hist[-50:]
    write_json(_planner_file, cfg)
    return cfg


def save_planner_capabilities(mandatory: list, *, actor: str = "") -> dict:
    cfg = load_planner_config()
    cfg["mandatoryCapabilities"] = [c for c in (mandatory or []) if isinstance(c, str) and c.strip()]
    cfg["updated_at"] = now_iso()
    write_json(_planner_file, cfg)
    return cfg


# ---------------------------------------------------------------- LLM Provider 运行时状态（data/llm_state.json）
# 记录"当前选用的 Provider" + 每类 Provider 在后台页面保存的连接参数。
# 优先级：环境变量 SNACK_LLM_PROVIDER 选择 provider 时最优先（env 权威，冒烟脚本不受页面切换影响）；
# 参数解析时 env > llm_state > 旧 config.json。
_llm_state_file = DATA_DIR / "llm_state.json"
_LLM_DEFAULT_PROVIDER = "openai-compatible"
_LLM_PROVIDER_DEFAULTS = {
    "openai-compatible": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                          "api_key": "", "timeout": 90},
    "coze": {"base_url": "https://api.coze.com", "bot_id": "", "model": "",
             "api_key": "", "timeout": 120},
    "demo-fixture": {},
}


def load_llm_state() -> dict:
    d = read_json(_llm_state_file, {})
    if not isinstance(d, dict):
        d = {}
    provs = d.setdefault("providers", {})
    for pid, defaults in _LLM_PROVIDER_DEFAULTS.items():
        c = provs.setdefault(pid, {})
        if not isinstance(c, dict):
            provs[pid] = c = {}
        for k, v in defaults.items():
            c.setdefault(k, v)
    d.setdefault("provider", _LLM_DEFAULT_PROVIDER)
    return d


def save_llm_state(provider: str | None = None, provider_config: dict | None = None) -> dict:
    """写回当前 provider 选择与/或某类 provider 的连接参数。api_key 为空串时保留旧值（不清空）。"""
    state = load_llm_state()
    provs = state.setdefault("providers", {})
    if provider_config is not None:
        pid = provider_config.get("id")
        if pid and pid in _LLM_PROVIDER_DEFAULTS:
            target = provs.setdefault(pid, {})
            for k in _LLM_PROVIDER_DEFAULTS[pid]:
                if k in provider_config:
                    nv = provider_config[k]
                    if k == "api_key":
                        if not isinstance(nv, str) or not nv.strip():
                            continue  # 留空=不改密钥，避免页面清掉已保存的密钥
                    elif k in ("timeout",):
                        try:
                            nv = int(nv)
                        except (TypeError, ValueError):
                            continue
                    else:
                        nv = str(nv).strip()
                    target[k] = nv
            provs[pid] = target
    if provider:
        if provider not in _LLM_PROVIDER_DEFAULTS:
            raise ValueError(f"未知 Provider: {provider}")
        state["provider"] = provider
    state["updated_at"] = now_iso()
    write_json(_llm_state_file, state)
    return state

