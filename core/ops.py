# -*- coding: utf-8 -*-
"""运营中心：评分 / 标注 / 改进建议 / A-B 实验 的本地 JSON 数据层 + 规则分析。

设计边界：
- 数据真实落盘在 data/ops_*.json（评分、标注、改进记录、A/B 实验）；
- 聚类 / 指标 / 改进方向 / 提示词草稿一律为【规则法】，接口与页面均明确标注
  method="规则聚类" / generator="规则草稿"，绝不伪装成 LLM 分析；
- 生成改进草稿从不自动写回生产提示词；apply 需 confirm 且写盘失败抛结构化中文错误。
"""
from __future__ import annotations

import csv
import io
import json
import uuid

from . import store

RATING_PROBLEMS = ["价格金额", "内容准确性", "完整性", "语气态度", "合规安全", "其他"]
ANNO_STATUS = ("pending", "accepted", "rejected")
DIMENSIONS = ("correctness", "relevance", "completeness", "safety", "tone", "overall")
FILES = {
    "rating": "ops_ratings.json",
    "annotation": "ops_annotations.json",
    "improvement": "ops_improvements.json",
    "abtest": "ops_ab_tests.json",
}

_CAP = 2000


def _nid(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _rows(kind):
    arr = store.read_json(store.DATA_DIR / FILES[kind], [])
    return arr if isinstance(arr, list) else []


def _save(kind, rows):
    store.write_json(store.DATA_DIR / FILES[kind], rows[-_CAP:])


def _run(run_id):
    return store.get_run(run_id) if run_id else None


def reply_text(run):
    if not isinstance(run, dict):
        return ""
    fr = run.get("finalReply")
    if isinstance(fr, dict):
        return str(fr.get("text") or "")
    return str(fr or "")


def _risk(run):
    if not isinstance(run, dict):
        return None
    r = run.get("risk") or run.get("riskResult")
    if isinstance(r, dict) and r.get("isRisky"):
        return {"isRisky": True, "riskType": r.get("riskType") or "其他"}
    return None


def _q_excerpt(question, n=48):
    question = question or ""
    return question[:n] + ("…" if len(question) > n else "")


# ================================================================ 服务评分
def create_rating(*, run_id=None, score=3, problem_type=None, comment="", author="", source=None):
    try:
        score = int(score)
    except (TypeError, ValueError):
        raise ValueError("评分必须为 1-5 的整数")
    if score < 1 or score > 5:
        raise ValueError("评分必须为 1-5")
    if problem_type and problem_type not in RATING_PROBLEMS:
        raise ValueError(f"未知问题类型：{problem_type}")
    run = _run(run_id)
    rows = _rows("rating")
    item = {
        "id": _nid("rt_"),
        "runId": run_id or None,
        "source": source or ("run" if run_id else "manual"),
        "question": (run or {}).get("question") or "",
        "replyText": reply_text(run),
        "score": score,
        "badcase": score <= 2,
        "problemType": problem_type or "",
        "comment": comment or "",
        "author": author or "",
        "createdAt": store.now_iso(),
    }
    rows.append(item)
    _save("rating", rows)
    return item


def list_ratings(limit=50, offset=0, *, from_date=None, to_date=None, score_min=None,
                 problem_type=None, badcase_only=False):
    rows = _rows("rating")
    rev = list(reversed(rows))
    out = []
    for r in rev:
        if badcase_only and not r.get("badcase"):
            continue
        if score_min is not None and int(r.get("score", 0) or 0) < int(score_min):
            continue
        if problem_type and (r.get("problemType") or "") != problem_type:
            continue
        if from_date and (r.get("createdAt") or "") < from_date:
            continue
        if to_date and (r.get("createdAt") or "") > to_date:
            continue
        out.append(r)
    return out[offset:offset + limit], len(out)


def rating_stats(rows):
    if not rows:
        return {"count": 0, "avg": 0.0, "low": 0}
    vals = [int(r.get("score", 0) or 0) for r in rows]
    return {"count": len(vals), "avg": round(sum(vals) / len(vals), 2),
            "low": sum(1 for v in vals if v <= 2)}


# ================================================================ 数据标注
def create_annotation(*, run_id=None, question="", dimensions=None, status="pending",
                      annotator="", note=""):
    if status not in ANNO_STATUS:
        raise ValueError(f"状态必须为 {'/'.join(ANNO_STATUS)} 之一")
    dims = {}
    if isinstance(dimensions, dict):
        for k in DIMENSIONS:
            v = dimensions.get(k)
            if v is None or v == "":
                dims[k] = None
            else:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"维度 {k} 必须为 1-5 的整数或空")
                if v < 1 or v > 5:
                    raise ValueError(f"维度 {k} 必须为 1-5")
                dims[k] = v
    run = _run(run_id)
    q = question or (run or {}).get("question") or ""
    rows = _rows("annotation")
    item = {
        "id": _nid("an_"),
        "runId": run_id or None,
        "source": "run" if run_id else "manual",
        "question": q,
        "replyText": reply_text(run),
        "dimensions": dims,
        "status": status,
        "annotator": annotator or "",
        "note": note or "",
        "createdAt": store.now_iso(),
    }
    rows.append(item)
    _save("annotation", rows)
    return item


def get_annotation(ann_id):
    for a in _rows("annotation"):
        if a.get("id") == ann_id:
            return a
    return None


def list_annotations(limit=50, offset=0, *, status=None, low_only=False, annotator=None):
    rows = _rows("annotation")
    rev = list(reversed(rows))
    out = []
    for a in rev:
        if status and (a.get("status") or "") != status:
            continue
        if annotator and (a.get("annotator") or "") != annotator:
            continue
        if low_only:
            ov = (a.get("dimensions") or {}).get("overall")
            if ov is None or int(ov) > 2:
                continue
        out.append(a)
    return out[offset:offset + limit], len(out)


def update_annotation(ann_id, *, status=None, note=None):
    rows = _rows("annotation")
    for a in rows:
        if a.get("id") != ann_id:
            continue
        if status is not None:
            if status not in ANNO_STATUS:
                raise ValueError(f"状态必须为 {'/'.join(ANNO_STATUS)} 之一")
            a["status"] = status
        if note is not None:
            a["note"] = note
        a["updatedAt"] = store.now_iso()
        _save("annotation", rows)
        return a
    return None


def export_annotations(fmt="json"):
    rows = list(_rows("annotation"))
    rows = list(reversed(rows))
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        head = ["id", "runId", "status", "annotator", "createdAt", "question",
                "note"] + list(DIMENSIONS)
        w.writerow(head)
        for a in rows:
            dims = a.get("dimensions") or {}
            w.writerow([a.get("id"), a.get("runId") or "", a.get("status") or "",
                        a.get("annotator") or "", a.get("createdAt") or "",
                        a.get("question") or "", a.get("note") or ""]
                       + ["" if dims.get(k) is None else dims.get(k) for k in DIMENSIONS])
        return "﻿" + buf.getvalue(), "annotations_export.csv", "text/csv; charset=utf-8"
    body = json.dumps(rows, ensure_ascii=False, indent=2)
    return body, "annotations_export.json", "application/json; charset=utf-8"


# ================================================================ 改进建议 / 记录
def _target_of_run(run):
    """由样例 run 推断最值得改进的目标（规则法）。"""
    steps = (run or {}).get("steps") or []
    if _risk(run):
        return [{"type": "skill", "id": "risk"}], "命中风险场景，安全话术可按具体风险类型（中奖/刷单/退款/钓鱼）细分并给出建议核实渠道"
    for s in steps:
        if s.get("status") == "error":
            t = s.get("type")
            return [{"type": t if t in ("skill", "tool") else "tool",
                     "id": s.get("id") or s.get("name") or ""}], \
                f"{'Skill' if (s.get('type') == 'skill') else 'Tool'}「{s.get('name') or s.get('id')}」执行失败，建议优先修复其数据/逻辑"
    if (run or {}).get("status") == "handoff":
        return [{"type": "planner"}], "出现转人工，建议规划阶段更早识别能力边界并给出可执行的降级话术"
    return [{"type": "skill", "id": "reply"}], "回复整体质量待提升，建议收敛话术风格与结构"


def _problem_target(problem_type):
    return {
        "价格金额": ("skill", "reply", "金额呈现需与「计算价格」明细逐项核对，禁止口算/编造合计"),
        "内容准确性": ("skill", "reply", "回答需逐句对照真实商品/价格/活动数据，避免信息漂移"),
        "完整性": ("skill", "reply", "回复需覆盖用户全部诉求点，结尾主动追问未满足项"),
        "语气态度": ("skill", "reply", "语气更亲切自然，先共情再处理，减少生硬模板感"),
        "合规安全": ("skill", "moderation", "加强合规要点：价格可溯源、无绝对化承诺、敏感引导拦截"),
    }.get(problem_type or "", ("skill", "reply", "回复整体质量待提升，建议收敛话术风格与结构"))


def create_improvement(*, title="", kind="manual", from_rating=None, from_annotation=None,
                       run_id=None, problem_type="", note="", targets=None, direction_text=None):
    ref = None
    src_run = None
    if from_rating:
        for r in list(reversed(_rows("rating"))):
            if r.get("id") == from_rating:
                ref = {"type": "rating", "id": from_rating,
                       "score": r.get("score"), "problemType": r.get("problemType")}
                src_run = _run(r.get("runId"))
                problem_type = problem_type or r.get("problemType") or ""
                break
        if not ref:
            raise ValueError("评分记录不存在")
        kind = "rating"
    elif from_annotation:
        for a in list(reversed(_rows("annotation"))):
            if a.get("id") == from_annotation:
                ref = {"type": "annotation", "id": from_annotation,
                       "overall": (a.get("dimensions") or {}).get("overall"),
                       "note": a.get("note")}
                src_run = _run(a.get("runId"))
                break
        if not ref:
            raise ValueError("标注记录不存在")
        kind = "annotation"

    # targets 为空时交给 _assign_targets 按 风险/失败步骤/问题类型 做规则推断
    if targets is None:
        targets = []
    sample_run_ids = [src_run.get("id")] if (src_run and src_run.get("id")) else ([run_id] if run_id else [])
    imp = {
        "id": _nid("im_"),
        "title": title or "",
        "kind": kind,
        "sourceRef": ref,
        "sourceNote": note or "",
        "sampleRunIds": [r for r in sample_run_ids if r],
        "createdAt": store.now_iso(),
        "status": "open",
        "targets": [],
        "draft": None,
        "applied": None,
        "history": [{"at": store.now_iso(), "what": "创建建议", "note": ""}],
    }
    rows = _rows("improvement")
    rows.append(imp)
    _save("improvement", rows)
    return _assign_targets(imp, targets, src_run, problem_type, direction_text)


def _assign_targets(imp, targets, src_run, problem_type, direction_text):
    """解析出确定的 targets + directionText（规则法），再写回该条记录。"""
    resolved = []
    direct = direction_text or ""
    steps = (src_run or {}).get("steps") or []
    if not targets:
        if _risk(src_run):
            resolved = [{"type": "skill", "id": "risk"}]
            direct = direct or "命中风险场景，建议细分不同风险类型的拦截话术与核实渠道"
        else:
            for s in steps:
                if s.get("status") == "error":
                    t = "skill" if s.get("type") == "skill" else "tool"
                    resolved = [{"type": t, "id": s.get("id") or s.get("name") or ""}]
                    direct = direct or f"{s.get('name') or s.get('id')} 执行失败，建议修复数据/逻辑"
                    break
            if not resolved and imp.get("kind") == "rating":
                t, tid, direct = _problem_target(problem_type)
                resolved = [{"type": t, "id": None if t == "planner" else tid}]
            elif not resolved:
                resolved = [{"type": "planner"}]
                direct = direct or "建议在规划阶段更早识别能力边界并给出降级话术"
    else:
        resolved = targets
        if not direct:
            direct = direction_text or ("待人工补充改进方向" if imp.get("kind") == "manual" else "综合样本提炼改进点")
    row = _rows("improvement")
    for i, it in enumerate(row):
        if it.get("id") == imp.get("id"):
            it["targets"] = resolved
            it["directionText"] = direct or ""
            if not it.get("title"):
                label = "、".join(
                    {"skill": "Skill", "tool": "Tool", "planner": "Planner"}.get(x.get("type"), x.get("type")) +
                    (f"「{x.get('id')}」" if x.get("id") else "") for x in resolved)
                it["title"] = f"{label} 改进（{imp.get('kind')}）" if label else "改进建议"
            row[i] = it
            _save("improvement", row)
            return it
    return imp


def get_improvement(imp_id):
    for i in _rows("improvement"):
        if i.get("id") == imp_id:
            return i
    return None


def list_improvements(limit=100, offset=0):
    rows = _rows("improvement")
    rev = list(reversed(rows))
    out = []
    for i in rev:
        sample = []
        for rid in (i.get("sampleRunIds") or []):
            r = store.get_run(rid)
            if r:
                sample.append({"runId": rid, "question": _q_excerpt(r.get("question"))})
        out.append({**i, "sampleRuns": sample})
    return out[offset:offset + limit], len(out)


def update_improvement_status(imp_id, status):
    if status not in ("open", "applied", "rejected", "resolved"):
        raise ValueError("状态必须为 open/applied/rejected/resolved")
    rows = _rows("improvement")
    for i in rows:
        if i.get("id") == imp_id:
            i["status"] = status
            i["history"].append({"at": store.now_iso(), "what": f"状态置为 {status}", "note": ""})
            _save("improvement", rows)
            return i
    return None


def _prompt_target_payload(imp):
    """把改进目标解析成可写盘的目标（skill/planner）。tool/eval 目标返回 None。"""
    for t in (imp.get("targets") or []):
        tt = t.get("type")
        tid = t.get("id")
        if tt == "planner":
            cfg = store.load_planner_config()
            cur = cfg.get("prompt") or ""
            if not cur:
                cur = __import__("core.engine", fromlist=["engine"]).PLAN_SYSTEM
            return {"type": "planner", "id": None, "prompt": cur, "label": "Planner 系统提示词"}
        if tt == "skill" and tid:
            sk = store.get_skill(tid)
            if not sk:
                continue
            return {"type": "skill", "id": tid, "prompt": sk.get("prompt") or "",
                    "label": f"Skill「{sk.get('name') or tid}」"}
        if tt == "tool":
            return {"type": "tool", "id": tid, "prompt": None,
                    "label": f"Tool「{tid}」（无提示词，需改数据/代码）"}
    return None


def _append_rule(prompt, rule):
    prompt = (prompt or "").rstrip()
    return (prompt + "\n\n" + rule) if prompt else rule


def compose_draft(imp_id):
    """生成【规则草稿】（仅存入记录，绝不写回生产提示词）。返回 (imp, payload)。"""
    imp = get_improvement(imp_id)
    if not imp:
        raise ValueError("改进建议不存在")
    tgt = _prompt_target_payload(imp)
    available = bool(tgt and tgt.get("prompt") is not None)
    text = ""
    if available and tgt["type"] == "skill":
        rule = _rule_for(imp)
        text = _append_rule(tgt["prompt"], rule)
    elif available and tgt["type"] == "planner":
        rule = _rule_for(imp)
        text = _append_rule(tgt["prompt"], rule)
    draft = {"text": text, "available": available, "target": tgt,
             "generator": "规则草稿", "at": store.now_iso(),
             "note": "由规则拼接当前提示词生成，未写入生产；应用前需人工确认并可先预览 diff"}
    rows = _rows("improvement")
    for i in rows:
        if i.get("id") == imp_id:
            i["draft"] = draft
            i["history"].append({"at": store.now_iso(), "what": "生成提示词草稿", "note": ""})
            i["status"] = "open"
            _save("improvement", rows)
            imp = i
            break
    return imp, draft


def _rule_for(imp):
    direct = (imp.get("directionText") or "").strip()
    kind = imp.get("kind")
    if kind == "rating":
        pt = (imp.get("sourceRef") or {}).get("problemType")
        rules = {
            "价格金额": "金额呈现规则（自动追加草稿）：所有价格、折扣、优惠数字必须逐项来自真实「计算价格」明细或用户原话数字，禁止自行口算/编造合计；列出多项时如明细无合计，只列单价并提示以下单结算页为准。",
            "内容准确性": "准确性规则（自动追加草稿）：成稿前逐句核对商品名称/规格/活动/优惠券字段来自本轮回传的真实数据，任何输入中不存在的承诺（时效、库存、售后）一律不得写入。",
            "完整性": "完整性规则（自动追加草稿）：先逐点列全用户诉求（数量/预算/规格/活动），全部回应后再收尾追问“还有其他需要帮您查看的吗”，缺信息的先澄清。",
            "语气态度": "语气规则（自动追加草稿）：开头先共情/承接（如“好的亲，这就为您看看”），行文口语化、少用机械模板句，避免连续问句堆叠。",
            "合规安全": "合规规则（自动追加草稿）：凡涉及转账/链接/验证码/私下加联系方式等一律明确拒绝并提醒谨防诈骗，回复不承诺任何赔付金额或到账时效。",
        }
        if pt in rules:
            return rules[pt]
    if _any_target(imp, "risk"):
        return "风险话术规则（自动追加草稿）：按具体风险类型（中奖先交手续费/刷单垫付/退款到个人账户/加QQ扫码点链接）分别给出“不转账·不提供验证码·不点链接·走官方渠道核实·必要时报警”的安全指引，话术与风险类型一一对应，避免一刀切模板。"
    if _any_target(imp, "moderation"):
        return "审核补充规则（自动追加草稿）：对金额类文案优先核验 facts 溯源，命中疑似编造价格时给出含 evidence 原句的可执行修改建议。"
    if direct and "失败" in direct:
        return "失败处理规则（自动追加草稿）：执行失败时如实说明“当前无法为您查询，请稍后重试或转人工”，并引导到可靠替代入口，不得编造结果。"
    return "通用改进规则（自动追加草稿）：回复先承接用户诉求，条理化组织信息，结尾主动询问是否需要进一步处理。".replace("（自动追加草稿）", "")


def _any_target(imp, skill_id):
    return any((t or {}).get("id") == skill_id for t in (imp.get("targets") or []))


def apply_improvement(imp_id, *, confirm=False, actor="ops"):
    """把已生成的草稿写回目标 Skill/Planner（需 confirm），应用前自动快照旧值。"""
    if not confirm:
        raise ValueError("未确认应用：请先确认改进草稿，再提交 confirm=true")
    imp = get_improvement(imp_id)
    if not imp:
        raise ValueError("改进建议不存在")
    draft = imp.get("draft") or {}
    if not draft.get("available") or not (draft.get("text") or "").strip():
        tgt = _prompt_target_payload(imp)
        if tgt and tgt["type"] == "tool":
            raise ValueError("目标为 Tool，无提示词可写盘；请改数据/代码后在记录中手动标记 resolved")
        raise ValueError("请先生成提示词草稿（generate-prompt）")
    tgt = draft.get("target") or {}
    new_prompt = draft["text"]
    note = f"应用改进 {imp_id}"
    result = {"target": tgt, "snapshotVid": None, "oldPrompt": (tgt.get("prompt") or "")}
    if tgt.get("type") == "skill" and tgt.get("id"):
        sk, ver = store.update_skill_versioned(tgt["id"], {"prompt": new_prompt},
                                               note=note, actor=actor)
        result["snapshotVid"] = (ver or {}).get("vid")
        result["newPrompt"] = new_prompt
        result["skillId"] = tgt["id"]
        result["label"] = tgt.get("label") or f"Skill {tgt['id']}"
    elif tgt.get("type") == "planner":
        store.save_planner_prompt(new_prompt, note=note, actor=actor)
        cfg = store.load_planner_config()
        result["snapshotVid"] = f"pv{cfg.get('version', 0)}"
        result["newPrompt"] = new_prompt
        result["label"] = "Planner"
    else:
        raise ValueError("该改进目标不支持提示词应用")
    result["at"] = store.now_iso()
    rows = _rows("improvement")
    for i in rows:
        if i.get("id") == imp_id:
            i["applied"] = result
            i["status"] = "applied"
            i["history"].append({"at": store.now_iso(), "what": "已应用草稿",
                                 "note": f"目标 {result.get('label')}，快照 {result.get('snapshotVid')}"})
            _save("improvement", rows)
            return i
    return imp


def rollback_improvement(imp_id, *, actor="ops"):
    """回滚一次已应用的改进：恢复应用前快照（Skill 旧版）并标记状态。"""
    imp = get_improvement(imp_id)
    if not imp:
        raise ValueError("改进建议不存在")
    app = imp.get("applied") or {}
    if not app.get("snapshotVid"):
        raise ValueError("该建议尚未应用或缺少快照，无法回滚")
    tgt = app.get("target") or {}
    restored = None
    if tgt.get("type") == "skill" and tgt.get("id"):
        restored = restore_skill_snapshot(tgt["id"], app["snapshotVid"], actor=actor,
                                          note=f"回滚改进 {imp_id}")
    elif tgt.get("type") == "planner":
        raise ValueError("Planner 回滚请到「规划器」历史中选择旧版本手动恢复")
    rows = _rows("improvement")
    for i in rows:
        if i.get("id") == imp_id:
            i["applied"]["revertedAt"] = store.now_iso()
            i["status"] = "resolved"
            i["history"].append({"at": store.now_iso(), "what": "已回滚改进",
                                 "note": f"恢复快照 {app['snapshotVid']}"})
            _save("improvement", rows)
            return i
    return imp


# ================================================================ Skill 快照 / 回滚
def snapshot_skill(skill_id, *, note="", actor=""):
    """为当前 Skill 内容落一条快照（供改进前留档 / 手工回滚点）。"""
    sk = store.get_skill(skill_id)
    if not sk:
        raise ValueError(f"Skill 不存在: {skill_id}")
    snap = {k: sk.get(k) for k in ("name", "description", "prompt", "model_params")}
    return store.add_skill_version(skill_id, snap, note=note or "手工快照", actor=actor)


def restore_skill_snapshot(skill_id, vid, *, actor="ops", note=""):
    """把某条快照的字段恢复到技能当前内容（自动再落一条当前值快照留痕）。"""
    sk = store.get_skill(skill_id)
    if not sk:
        raise ValueError(f"Skill 不存在: {skill_id}")
    v = store.get_skill_version(skill_id, vid)
    if not v:
        raise ValueError(f"版本不存在: {vid}")
    snap = v.get("snapshot") or {}
    patch = {k: snap[k] for k in ("name", "description", "prompt", "model_params") if k in snap}
    if not patch:
        raise ValueError("该快照无可恢复的内容字段")
    s, ver = store.update_skill_versioned(skill_id, patch,
                                          note=(note or f"回滚到 {vid}"), actor=actor)
    return {"skill": s, "version": ver, "restoredVid": vid}


# ================================================================ A/B 实验记录
def list_ab_tests(limit=100):
    rows = list(_rows("abtest"))
    return list(reversed(rows))[:limit]


def create_ab_test(*, name, variant_a, variant_b, target_type="skill", target_id="reply", note=""):
    if not (name or "").strip():
        raise ValueError("实验名称不能为空")
    rows = _rows("abtest")
    item = {
        "id": _nid("ab_"),
        "name": name,
        "createdAt": store.now_iso(),
        "active": False,
        "activeVariant": None,
        "target": {"type": target_type, "id": target_id},
        "variantA": {"name": (variant_a.get("name") or "A"), "prompt": variant_a.get("prompt") or ""},
        "variantB": {"name": (variant_b.get("name") or "B"), "prompt": variant_b.get("prompt") or ""},
        "notes": note or "",
    }
    rows.append(item)
    _save("abtest", rows)
    return item


def update_ab_test(ab_id, *, active=None, active_variant=None, note=None):
    rows = _rows("abtest")
    for a in rows:
        if a.get("id") != ab_id:
            continue
        if active is not None:
            a["active"] = bool(active)
            if not active:
                a["activeVariant"] = None
        if active_variant in ("A", "B"):
            a["activeVariant"] = active_variant
            a["active"] = True
        if note is not None:
            a["notes"] = note
        a["updatedAt"] = store.now_iso()
        _save("abtest", rows)
        return a
    return None


# ================================================================ 指标 + 规则聚类（overview）
def _failed_steps(run):
    return [s for s in ((run or {}).get("steps") or []) if s.get("status") == "error"]


def run_status_of(run):
    if not isinstance(run, dict):
        return ""
    return run.get("status") or ""


def _trim_run(run):
    fr = run.get("finalReply") or {}
    return {
        "id": run.get("id"), "question": run.get("question"), "source": run.get("source"),
        "status": run.get("status"), "createdAt": run.get("createdAt"),
        "durationMs": run.get("durationMs"), "provider": run.get("provider"),
        "replyPreview": str(fr.get("text", ""))[:60] if isinstance(fr, dict) else "",
        "error": run.get("error"),
    }


def build_overview(*, recent=10):
    all_rows = store.read_json(store.DATA_DIR / "runs.json", [])
    all_rows = all_rows if isinstance(all_rows, list) else []
    runs = list(reversed(all_rows))
    total = len(runs)
    by_status = {}
    finished = []
    failed_steps = 0
    risk_entries = []
    blocked_replies = 0
    for r in runs:
        st = run_status_of(r)
        by_status[st] = by_status.get(st, 0) + 1
        if st in ("ok", "error", "degraded", "handoff"):
            finished.append(r)
        failed_steps += len(_failed_steps(r))
        rk = _risk(r)
        if rk:
            risk_entries.append({"runId": r.get("id"), "riskType": rk["riskType"],
                                 "question": r.get("question")})
        fr = r.get("finalReply")
        if isinstance(fr, dict) and fr.get("blocked"):
            blocked_replies += 1
    ok_n = by_status.get("ok", 0)
    durations = [int((r.get("durationMs") or 0)) for r in finished
                 if r.get("durationMs") is not None]
    handoff_n = by_status.get("handoff", 0)
    ratings_all = _rows("rating")
    rs = rating_stats(ratings_all)
    risk_by_type = {}
    for e in risk_entries:
        risk_by_type[e["riskType"]] = risk_by_type.get(e["riskType"], 0) + 1
    metrics = {
        "total": total,
        "byStatus": by_status,
        "ok": ok_n,
        "finished": len(finished),
        "successRate": round(ok_n / len(finished), 3) if finished else 0.0,
        "avgDurationMs": int(sum(durations) / len(durations)) if durations else 0,
        "failedSteps": failed_steps,
        "blockedReplies": blocked_replies,
        "handoffCount": handoff_n,
        "handoffRate": round(handoff_n / len(finished), 3) if finished else 0.0,
        "riskRuns": len(risk_entries),
        "ratings": rs,
        "annotations": len(_rows("annotation")),
        "improvements": len(_rows("improvement")),
    }
    recent_runs = [_trim_run(r) for r in runs[:recent]]
    clusters = build_clusters(runs, ratings_all, risk_entries)
    return {"metrics": metrics, "riskByType": [{"riskType": k, "count": v}
                                                for k, v in risk_by_type.items()],
            "clusters": clusters, "recentRuns": recent_runs}


def build_clusters(runs, ratings, risk_entries=None):
    """按运行记录/评分/标注做【规则聚类】，method 恒为规则聚类（不做 LLM 分析）。"""
    risk_entries = risk_entries or []
    out = []

    def sample(r, extra=None, n=5):
        e = {"runId": r.get("id"), "question": _q_excerpt(r.get("question")),
             "status": run_status_of(r), "createdAt": r.get("createdAt")}
        if extra:
            e.update(extra)
        return e

    # 1) 高风险 / 命中风险识别
    risky_ids = {e["runId"] for e in risk_entries}
    risky = [r for r in runs if r.get("id") in risky_ids]
    if risky:
        out.append({
            "key": "risk", "title": "高风险样本（诈骗/资金风险命中）", "method": "规则聚类",
            "severity": "high", "count": len(risky), "hint": "命中风险识别，最终未执行任何销售/收款操作",
            "samples": [sample(r) for r in risky[:5]],
        })
    # 2) 拦截 / 校验失败
    blocked = []
    for r in runs:
        fr = r.get("finalReply")
        if (isinstance(fr, dict) and fr.get("blocked")) or r.get("validationErrors"):
            blocked.append(r)
    if blocked:
        out.append({
            "key": "blocked", "title": "被拦截/校验失败样本", "method": "规则聚类",
            "severity": "high", "count": len(blocked),
            "hint": "计划未过校验或最终回复被风控拦截，需人工复核策略",
            "samples": [sample(r) for r in blocked[:5]],
        })
    # 3) 工具/Skill 失败
    err_runs = [r for r in runs if _failed_steps(r)]
    if err_runs:
        bad = _failed_steps(err_runs[0]) if err_runs else []
        out.append({
            "key": "step_error", "title": "执行失败（Tool/Skill 报错）", "method": "规则聚类",
            "severity": "mid", "count": len(err_runs),
            "hint": f"例如 {bad[0].get('name') or bad[0].get('id')} 步骤 error，失败会如实落 RunRecord=ERROR",
            "samples": [sample(r, {"failed": ", ".join(
                str(s.get('name') or s.get('id')) for s in _failed_steps(r))}) for r in err_runs[:5]],
        })
    # 4) 转人工
    handoff = [r for r in runs if run_status_of(r) == "handoff"]
    if handoff:
        out.append({
            "key": "handoff", "title": "转人工样本", "method": "规则聚类",
            "severity": "low", "count": len(handoff),
            "hint": "人工接管率上升时关注能力边界与降级话术",
            "samples": [sample(r) for r in handoff[:5]],
        })
    # 5) 低分评分样本
    lows = [r for r in ratings if r.get("badcase")]
    if lows:
        out.append({
            "key": "low_rating", "title": "低分反馈（可沉淀为坏例）", "method": "规则聚类",
            "severity": "mid", "count": len(lows),
            "hint": "评分 ≤2 自动进入坏例候选，可一键生成改进建议",
            "samples": [{"ratingId": r.get("id"), "runId": r.get("runId"), "score": r.get("score"),
                         "problemType": r.get("problemType") or "",
                         "question": _q_excerpt(r.get("question")), "createdAt": r.get("createdAt")}
                        for r in lows[:5]],
        })
    # 6) 低分标注（overall ≤2）
    ann_rows = list(reversed(_rows("annotation")))
    low_anns = [a for a in ann_rows if (a.get("dimensions") or {}).get("overall") not in (None,) and
                int((a.get("dimensions") or {}).get("overall") or 3) <= 2]
    if low_anns:
        out.append({
            "key": "low_annotation", "title": "低分标注样本", "method": "规则聚类",
            "severity": "mid", "count": len(low_anns),
            "hint": "整体分 ≤2 的标注，可进入评测集或改进建议",
            "samples": [{"annotationId": a.get("id"), "runId": a.get("runId"),
                         "question": _q_excerpt(a.get("question")),
                         "overall": (a.get("dimensions") or {}).get("overall"),
                         "status": a.get("status"), "createdAt": a.get("createdAt")}
                        for a in low_anns[:5]],
        })
    if not out:
        out.append({"key": "empty", "title": "暂无聚类样本", "method": "规则聚类",
                    "severity": "low", "count": 0,
                    "hint": "有运行记录且出现风险/失败/低分后会自动聚类", "samples": []})
    return out
