# -*- coding: utf-8 -*-
"""评测批次（持久化）+ 版本对比。

在既有 Eval 子系统之上：把“一次性前台循环批跑”升级为可轮询、可回看、可对比的
持久化批次，并支持两个已结束批次间的逐例版本对比。

铁律（与主链路/Eval 一致口径）：
- 每一例都真实复用 runner.run_run（与 /demo、单条评测完全同一条 Agent 主链路），
  评分恒用 eval_mod.score_case；绝不读取旧批次 caseResults 伪造新批次结果。
- 每完成一例即整体写回 data/eval_batches.json（进度可轮询）；读取/GET 一律按
  caseResults 实时派生 passed/failed/review/errors —— 手改磁盘计数不生效（防伪）。
- 同时只允许一个非终态批次（queued/running）：重复创建抛 ActiveBatchError(409)。
- 单条 ERROR（工具/运行失败）只记该 case=ERROR，批次继续；批次级故障才 error。
  终态满足 total = pass+fail+review+error（cancel/error 时未跑 case 补 ERROR，attempted=false）。
- 批次快照 case 语义指纹(contentHash)+skills/tool 启用态+评分器(evaluator)版本，
  用于版本对比时判断“两批是否可比较”。skill/tool 差异本身不阻断对比（那是对比对象），
  仅作上下文展示。
"""
from __future__ import annotations

import hashlib
import threading
import uuid

from . import eval as eval_mod, runner, store, tools

BATCHES_FILE = "eval_batches.json"
_CAP = 300
_TERMINAL = {"done", "error", "cancelled"}
_SOURCE = "eval"

_LOCK = threading.Lock()
_CANCEL: dict = {}  # bid -> threading.Event


class ActiveBatchError(Exception):
    """已有 queued/running 批次（409）。"""

    def __init__(self, active_batch_id=None):
        self.active_batch_id = active_batch_id
        super().__init__("已存在正在执行/排队中的评测批次")


class BatchNotFoundError(Exception):
    """批次不存在（404）。"""

    def __init__(self, message="评测批次不存在"):
        super().__init__(message)


# ---------------------------------------------------------------- 存取
def _file():
    return store.DATA_DIR / BATCHES_FILE


def _rows():
    arr = store.read_json(_file(), None)
    return arr if isinstance(arr, list) else []


def _write(rows):
    store.write_json(_file(), rows[-_CAP:])


def _find_raw(bid):
    for b in _rows():
        if b.get("id") == bid:
            return b
    return None


def _persist_locked(b):
    rows = _rows()
    for i, x in enumerate(rows):
        if x.get("id") == b.get("id"):
            rows[i] = b
            _write(rows)
            return
    rows.append(b)
    _write(rows)


# ---------------------------------------------------------------- 快照原语
def _skill_env():
    """skills/tools 语义快照：skillHash 覆盖全部 skill 的 id/enabled/prompt。"""
    skills = sorted(store.load_skills(), key=lambda s: (s.get("id") or ""))
    parts = [f"{s.get('id')}|{1 if s.get('enabled') else 0}|{s.get('prompt') or ''}"
             for s in skills]
    skill_hash = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return {
        "skills": [{"id": s.get("id"), "name": s.get("name", s.get("id")),
                    "enabled": bool(s.get("enabled"))} for s in skills],
        "skillHash": skill_hash,
        "tools": [{"id": m.get("id"), "name": m.get("name", m.get("id")),
                   "enabled": store.tool_enabled(m.get("id"))} for m in tools.META],
    }


def _case_snapshot(case):
    return {
        "id": case.get("id"),
        "name": case.get("name", ""),
        "question": case.get("question", ""),
        "category": case.get("category"),
        "difficulty": case.get("difficulty"),
        "riskLevel": case.get("riskLevel"),
        "contentHash": eval_mod.case_content_hash(case),
        "expectRisk": case.get("expectRisk"),
        "expectedKeywords": case.get("expectedKeywords") or [],
        "forbiddenWords": case.get("forbiddenWords") or [],
        "requiredCapabilities": case.get("requiredCapabilities") or [],
        "forbiddenCapabilities": case.get("forbiddenCapabilities") or [],
    }


def _case_set_hash(snap_items):
    parts = sorted(f"{i.get('id')}|{i.get('contentHash')}" for i in snap_items)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 派生统计
def _derive_locked(b):
    counts = {"PASS": 0, "FAIL": 0, "REVIEW": 0, "ERROR": 0}
    for r in b.get("caseResults") or []:
        s = r.get("status")
        counts[s if s in counts else "ERROR"] += 1
    b["passed"] = counts["PASS"]
    b["failed"] = counts["FAIL"]
    b["review"] = counts["REVIEW"]
    b["errors"] = counts["ERROR"]
    b["total"] = len(b.get("caseIds") or [])
    return b


def _stats(b):
    """把一条批次压成对比/报告用的统计快照（ERROR 不进通过率分母）。"""
    res = b.get("caseResults") or []
    c = {k: sum(1 for r in res if r.get("status") == k)
         for k in ("PASS", "FAIL", "REVIEW", "ERROR")}
    denom = c["PASS"] + c["FAIL"] + c["REVIEW"]
    durs = [r.get("durationMs") for r in res
            if isinstance(r.get("durationMs"), (int, float)) and r.get("durationMs", 0) > 0]
    return {
        "id": b.get("id"), "name": b.get("name", ""),
        "versionLabel": b.get("versionLabel", ""), "changeNote": b.get("changeNote", ""),
        "status": b.get("status"), "createdAt": b.get("createdAt"),
        "passed": c["PASS"], "failed": c["FAIL"], "review": c["REVIEW"],
        "errors": c["ERROR"], "total": len(res),
        "passRate": round(100.0 * c["PASS"] / denom, 1) if denom else None,
        "avgDurationMs": int(sum(durs) / len(durs)) if durs else None,
    }


def _empty_error_score(note, cid):
    return {"caseId": cid, "runId": None, "durationMs": None, "provider": None,
            "model": None, "judge": "rules", "judgeNote": "",
            "status": "ERROR", "passed": False, "reason": note, "error": note,
            "evidence": ["批次级终止，该 case 未执行（attempted=false）。"]}


def _error_result(note, cid, *, attempted=False, snap=None):
    snap = snap or {}
    return {
        "caseId": cid,
        "name": snap.get("name", ""),
        "question": snap.get("question", ""),
        "status": "ERROR", "runId": None, "replyText": "",
        "durationMs": None, "attempted": attempted, "note": note,
        "score": _empty_error_score(note, cid),
    }


def _fill_leftovers_locked(b, note):
    done = {r.get("caseId") for r in b.get("caseResults") or []}
    snap_map = {s.get("id"): s for s in (b.get("caseSnapshot") or [])}
    for cid in b.get("caseIds") or []:
        if cid in done:
            continue
        b.setdefault("caseResults", []).append(
            _error_result(note, cid, attempted=False, snap=snap_map.get(cid)))


# ---------------------------------------------------------------- 创建 / worker
def active_batch_id():
    with _LOCK:
        return active_batch_id_locked()


def active_batch_id_locked():
    for b in _rows():
        if b.get("status") in ("queued", "running"):
            return b.get("id")
    return None


def last_case_ids():
    """最新一批的 caseIds（供“二次回归”默认复用第一批）。"""
    rows = _rows()
    return list((rows[-1].get("caseIds") or [])) if rows else []


def create_batch(*, case_ids=None, name="", version_label="", change_note="",
                 force_error_tool=None):
    """新建批次并启动后台 worker。

    case_ids=None → 全部 enabled；[] → 抛错；含未知 id/重复 id → 抛错（列出）。
    已有 active → 抛 ActiveBatchError。
    """
    with _LOCK:
        act = active_batch_id_locked()
        if act:
            raise ActiveBatchError(act)
        if case_ids is None:
            case_ids = eval_mod.default_enabled_set()
            if not case_ids:
                raise ValueError("没有可启用的评测用例")
        elif not case_ids:
            raise ValueError("caseIds 不能为空数组")
        seen, dups = set(), []
        for cid in case_ids:
            if cid in seen:
                dups.append(cid)
            seen.add(cid)
        if dups:
            raise ValueError(f"caseIds 含重复 id：{'、'.join(dict.fromkeys(dups))}")
        missing = [cid for cid in case_ids if eval_mod.get_case(cid) is None]
        if missing:
            raise ValueError(f"用例不存在：{'、'.join(missing)}")
        ordered = list(case_ids)
        snap_items = [_case_snapshot(eval_mod.get_case(cid)) for cid in ordered]
        evaluator = eval_mod.evaluator_snapshot()
        env = _skill_env()
        bid = "eb_" + uuid.uuid4().hex[:10]
        batch = {
            "id": bid,
            "name": str(name).strip() or "评测批次",
            "versionLabel": str(version_label).strip() or "",
            "changeNote": str(change_note).strip() or "",
            "caseIds": ordered,
            "caseSetHash": _case_set_hash(snap_items),
            "caseSnapshot": snap_items,
            "skills": env["skills"],
            "skillHash": env["skillHash"],
            "tools": env["tools"],
            "evaluator": {"name": evaluator["name"], "version": evaluator["version"],
                          "hash": evaluator["hash"], "judgeNote": evaluator["judgeNote"]},
            "provider": "", "model": "",
            "params": {"source": _SOURCE, "forceErrorTool": force_error_tool},
            "createdAt": store.now_iso(), "startedAt": None, "finishedAt": None,
            "status": "queued",
            "currentIndex": 0, "total": len(ordered),
            "passed": 0, "failed": 0, "review": 0, "errors": 0,
            "caseResults": [],
        }
        rows = _rows()
        rows.append(batch)
        _write(rows)
    threading.Thread(target=_worker, args=(bid,), daemon=True).start()
    return batch


def _worker(bid):
    with _LOCK:
        b = _find_raw(bid)
        if not b or b.get("status") != "queued":
            return
        b["status"] = "running"
        b["startedAt"] = store.now_iso()
        _persist_locked(b)
    try:
        _run_loop(bid)
    except Exception as e:  # noqa: BLE001 —— 批次级灾难才把整批置 error
        with _LOCK:
            b = _find_raw(bid)
            if b and b.get("status") not in _TERMINAL:
                b["status"] = "error"
                _fill_leftovers_locked(b, note=f"批次执行中断：{e}")
                b["finishedAt"] = store.now_iso()
                _derive_locked(b)
                _persist_locked(b)
    finally:
        _CANCEL.pop(bid, None)


def _run_loop(bid):
    with _LOCK:
        b = _find_raw(bid)
        if not b:
            return
        case_ids = list(b.get("caseIds") or [])
        force = (b.get("params") or {}).get("forceErrorTool")
    for idx, cid in enumerate(case_ids):
        if _cancelled(bid):
            with _LOCK:
                b = _find_raw(bid)
                if b and b.get("status") not in _TERMINAL:
                    b["status"] = "cancelled"
                    _fill_leftovers_locked(b, note="已取消，未执行")
                    b["finishedAt"] = store.now_iso()
                    _derive_locked(b)
                    _persist_locked(b)
            return
        with _LOCK:
            b = _find_raw(bid)
            if not b:
                return
            b["currentIndex"] = idx
            _persist_locked(b)
        case = eval_mod.get_case(cid)
        if case is None:
            result = _error_result("用例不存在（可能已被删除）", cid, snap=next(
                (s for s in (b or {}).get("caseSnapshot") or [] if s.get("id") == cid), {}))
        else:
            result = _run_one(bid, cid, case, force)
        with _LOCK:
            b = _find_raw(bid)
            if not b:
                return
            sc = (result.get("score") or {})
            if sc.get("provider"):
                b["provider"] = sc.get("provider")
            if sc.get("model"):
                b["model"] = sc.get("model")
            b.setdefault("caseResults", []).append(result)
            b["currentIndex"] = idx + 1
            _derive_locked(b)
            _persist_locked(b)
    with _LOCK:
        b = _find_raw(bid)
        if not b or b.get("status") in _TERMINAL:
            return
        b["status"] = "done"
        b["finishedAt"] = store.now_iso()
        _derive_locked(b)
        _persist_locked(b)


def _cancelled(bid):
    ev = _CANCEL.get(bid)
    return bool(ev and ev.is_set())


def _run_one(bid, cid, case, force_error_tool):
    question = (case.get("question") or "").strip()
    if not question:
        return _error_result("用例缺少 question", cid, attempted=False,
                             snap=_case_snapshot(case))
    try:
        rec = runner.run_run(question, source=_SOURCE,
                             conversation_id=f"eval_batch:{bid}:{cid}",
                             force_error_tool=force_error_tool)
    except Exception as e:  # noqa: BLE001 —— runner 兜底后理论不抛，双保险仍记 ERROR
        return _error_result(f"运行抛错：{e}", cid, attempted=True,
                             snap=_case_snapshot(case))
    if not isinstance(rec, dict) or not rec.get("id"):
        return _error_result("未返回运行记录", cid, attempted=False,
                             snap=_case_snapshot(case))
    score = eval_mod.score_case(case, rec)
    try:
        store.save_run_field(rec.get("id"), {"eval": score})  # 溯源：评分挂回该 Run
    except Exception:
        pass
    return {
        "caseId": cid,
        "name": case.get("name", ""),
        "question": question,
        "status": score.get("status"),
        "runId": rec.get("id"),
        "replyText": ((rec.get("finalReply") or {}).get("text") or "").strip(),
        "durationMs": rec.get("durationMs"),
        "attempted": True,
        "note": (score.get("error") or "") if score.get("status") == "ERROR" else "",
        "score": score,
    }


# ---------------------------------------------------------------- 读取 / cancel
def get_batch(bid):
    with _LOCK:
        b = _find_raw(bid)
        return _derive_locked(b) if b else None


def list_batches(limit=50):
    with _LOCK:
        rows = _rows()
        out = []
        for b in reversed(rows[-_CAP:]):
            out.append(_derive_locked(b))
        return out[:max(1, min(limit, 500))]


def cancel_batch(bid):
    with _LOCK:
        b = _find_raw(bid)
        if not b:
            raise BatchNotFoundError("评测批次不存在")
        if b.get("status") in _TERMINAL:
            raise ValueError("批次已结束，无法取消")
        if b.get("status") == "queued":
            # worker 尚未开跑：直接置终态并补齐，保持 total 不变式
            b["status"] = "cancelled"
            _fill_leftovers_locked(b, note="已取消，未执行")
            b["finishedAt"] = store.now_iso()
            _derive_locked(b)
            _persist_locked(b)
            return b
        # running → 通知 worker 于当前 case 结束后停下
        flag = _CANCEL.setdefault(bid, threading.Event())
        flag.set()
        return _derive_locked(b)


# ---------------------------------------------------------------- 版本对比
def _diff_rows(base_env, cur_env, key):
    """env 为 [{id,name,enabled,...}]，返回形如 "compute_price: 关→开" 的差异行。"""
    bm = {x.get("id"): x for x in base_env}
    cm = {x.get("id"): x for x in cur_env}
    rows = []
    for cid in sorted(set(bm) | set(cm)):
        b, c = bm.get(cid), cm.get(cid)
        if b is None:
            rows.append(f"{cid}: 新增(enable={bool(c and c.get('enabled'))})")
        elif c is None:
            rows.append(f"{cid}: 移除")
        else:
            label = c.get("name") or cid
            if bool(b.get("enabled")) != bool(c.get("enabled")):
                rows.append(f"{label}: {('开' if b.get('enabled') else '关')}→"
                            f"{('开' if c.get('enabled') else '关')}")
            elif key == "skills" and (b.get("prompt") or "") != (c.get("prompt") or ""):
                rows.append(f"{label}: prompt 已变更")
    return rows


def compare_batches(ida, idb):
    a = get_batch(ida)
    if a is None:
        raise BatchNotFoundError("基准批次不存在")
    b = get_batch(idb)
    if b is None:
        raise BatchNotFoundError("对比批次不存在")
    if (a.get("createdAt") or "") <= (b.get("createdAt") or ""):
        base, cur = a, b
    else:
        base, cur = b, a

    blocks = []
    if base.get("status") != "done" or cur.get("status") != "done":
        blocks.append(f"两批均需执行完成(done)后对比，当前 base={base.get('status')} cur={cur.get('status')}")
    bset = set(base.get("caseIds") or [])
    cset = set(cur.get("caseIds") or [])
    if bset != cset:
        blocks.append("两批 caseIds 集合不一致"
                      + (f"（仅 base：{sorted(bset - cset)}；仅 cur：{sorted(cset - bset)}）"
                         if (bset - cset) or (cset - bset) else ""))
    bmap = {i.get("id"): i for i in base.get("caseSnapshot") or []}
    cmap = {i.get("id"): i for i in cur.get("caseSnapshot") or []}
    diff_content = [cid for cid in (bset & cset)
                    if (bmap.get(cid) or {}).get("contentHash") != (cmap.get(cid) or {}).get("contentHash")]
    if diff_content:
        blocks.append(f"用例内容发生变更（contentHash 不同）：{'、'.join(sorted(diff_content))}")
    ev0, ev1 = base.get("evaluator") or {}, cur.get("evaluator") or {}
    ev_same = (ev0.get("version"), ev0.get("hash")) == (ev1.get("version"), ev1.get("hash"))
    if not ev_same:
        blocks.append("两批评分器(evaluator) 版本/hash 不一致")
    pm0, pm1 = base.get("params") or {}, cur.get("params") or {}
    run_same = (base.get("provider"), base.get("model"), pm0) == \
               (cur.get("provider"), cur.get("model"), pm1)
    if not run_same:
        blocks.append("两批 provider/model/params 不一致（执行环境不同）")

    comparable = not blocks
    bres = {r.get("caseId"): r for r in base.get("caseResults") or []}
    cres = {r.get("caseId"): r for r in cur.get("caseResults") or []}
    deltas = []
    for cid in base.get("caseIds") or []:
        br, cr = bres.get(cid), cres.get(cid)
        if br is None or cr is None:
            continue
        ps = br.get("status") or "ERROR"
        cs = cr.get("status") or "ERROR"
        prt = br.get("replyText") or ""
        crt = cr.get("replyText") or ""
        deltas.append({
            "caseId": cid,
            "name": cr.get("name") or br.get("name") or cid,
            "question": cr.get("question") or br.get("question") or "",
            "prevStatus": ps, "curStatus": cs,
            "prevRunId": br.get("runId"), "curRunId": cr.get("runId"),
            "fixed": bool(ps != "PASS" and cs == "PASS"),
            "regressed": bool(ps == "PASS" and cs != "PASS"),
            "statusChanged": ps != cs,
            "replyChanged": bool(ps == cs and prt and crt and prt != crt),
            "prevReply": prt, "curReply": crt,
            "durPrev": br.get("durationMs"), "durCur": cr.get("durationMs"),
        })
    n_fixed = sum(1 for d in deltas if d["fixed"])
    n_reg = sum(1 for d in deltas if d["regressed"])
    n_chg = sum(1 for d in deltas if d["statusChanged"])
    n_rchg = sum(1 for d in deltas if d["replyChanged"])

    verdict = []
    if not comparable:
        verdict.append("两批不可直接比较（差异见 blocks），不给出“版本更好/变差”结论")
    else:
        if n_fixed:
            verdict.append(f"已修复 {n_fixed} 例")
        if n_reg:
            verdict.append(f"新增失败 {n_reg} 例")
        if n_rchg:
            verdict.append(f"状态未变但回复变化 {n_rchg} 例")
        if not verdict:
            verdict.append("两批结果一致，无回归也无修复")

    skill_diff = _diff_rows(base.get("skills") or [], cur.get("skills") or [], "skills")
    tool_diff = _diff_rows(base.get("tools") or [], cur.get("tools") or [], "tools")

    return {
        "comparable": comparable,
        "blocks": blocks,
        "base": _stats(base),
        "cur": _stats(cur),
        "sameHarness": {
            "caseIds": bset == cset,
            "snapshot": not diff_content,
            "evaluator": ev_same,
            "runtime": run_same,
        },
        "skillChanged": bool(skill_diff),
        "toolChanged": bool(tool_diff),
        "context": {"skillDiff": skill_diff, "toolDiff": tool_diff},
        "deltas": deltas,
        "counts": {"fixed": n_fixed, "regressed": n_reg,
                   "statusChanged": n_chg, "replyChanged": n_rchg},
        "verdict": "；".join(verdict),
    }
