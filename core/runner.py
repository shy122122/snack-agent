# -*- coding: utf-8 -*-
"""Run 编排层：把 plan → validate → execute → 风险审计 → RunRecord 串成一次可流式的运行。

- 串行调用 core.engine.plan / core.validator.validate_plan / core.engine.execute；
- 每个阶段通过 emit(event_type, payload) 向外推事件（SSE / 内存收集器均可），
  事件类型：status / plan / step / reply / risk / error / done；
- 先将结果持久化为 data/runs.json 的一条 status=running 记录，跑完用 save_run_field
  原位更新成最终状态（保持按 createdAt 的列表顺序稳定）；
- 提供 retry / handoff / annotate / explain 所需的最小字段与工具。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from . import engine, llm as llm_mod, store, tools, validator as validator_mod

SOURCE_DEFAULT = "api"


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _snapshot():
    """skillVersions / toolVersions 运行时快照（skill id/name + tool meta/enabled + 数据文件 mtime）。"""
    return {
        "skillVersions": [
            {"id": s["id"], "name": s["name"]}
            for s in store.load_skills() if s.get("enabled")
        ],
        "toolVersions": [
            {"id": m["id"], "name": m["name"], "enabled": store.tool_enabled(m["id"])}
            for m in tools.META
        ],
        "dataMtimes": store.data_mtimes(),
    }


def _derive_status(result):
    """把 engine.execute 的返回映射为 RunRecord.status。"""
    if not result.get("ok"):
        return "error"
    blocked = bool(result.get("blocked_reason")
                   or (result.get("final_reply") or {}).get("blocked"))
    return "degraded" if blocked else "ok"


def run_run(question: str, *, source=None, conversation_id=None,
            mandatory_capabilities=None, force_error_tool=None,
            emit=None, retry_of=None):
    """执行一次完整 Agent 主链路并返回最终 RunRecord dict。

    emit 为可选回调 emit(event_type, payload)；缺省时静默（便于纯离线调用）。
    """
    question = (question or "").strip()
    source = source or SOURCE_DEFAULT
    conv_id = conversation_id or ""
    emit = emit or (lambda etype, data: None)

    def _emit(etype, data):
        try:
            emit(etype, data)
        except Exception:
            pass

    run_id = uuid.uuid4().hex[:12]
    created_at = _now_iso()
    _emit("status", {"phase": "running", "question": question, "runId": run_id})

    # 预写 running 记录：长耗时运行中刷新详情页也能看到
    base = {
        "id": run_id,
        "question": question,
        "source": source,
        "conversationId": conv_id,
        "retryOf": retry_of or None,
        "createdAt": created_at,
        "status": "running",
        "provider": "",
        "model": "",
        "error": None,
    }
    if retry_of:
        base["note"] = f"由运行 {retry_of} 重试触发"
    store.save_run(base)

    t0 = time.time()
    result = None
    plan_obj = None
    try:
        # ---- 1) Planner ----
        pl = engine.plan(question, mandatory_capabilities=mandatory_capabilities)
        plan_obj = pl
        abilities = pl.get("abilities") or engine.build_abilities()
        _emit("plan", {
            "summary": pl.get("summary", ""),
            "reasoning": pl.get("reasoning", ""),
            "steps": pl.get("steps", []),
            "selectedSkills": pl.get("selectedSkills", []),
            "selectedTools": pl.get("selectedTools", []),
            "mandatoryCapabilities": pl.get("mandatoryCapabilities", []),
            "risk": pl.get("risk", {}),
            "warnings": pl.get("warnings", []),
            "provider": pl.get("provider", ""),
            "model": pl.get("model", ""),
        })

        # ---- 2) 独立 Plan Validator ----
        risk_hits = validator_mod.detect_risk(question)
        v = validator_mod.validate_plan(pl, abilities, question=question, risk_signal=risk_hits)
        if not v["ok"]:
            # 不伪装成功：直接把校验错误写进 RunRecord（degraded / error）
            msgs = [f"[{e['code']}] {e['detail']}" for e in v["errors"]]
            text = "计划未通过安全校验：" + "；".join(msgs)
            _emit("error", {"code": "validation_failed", "message": text,
                            "errors": v["errors"]})
            result = {
                "ok": False, "error": text, "question": question, "plan": pl,
                "warnings": v["warnings"], "trace": [], "steps_count": 0,
                "usage": {}, "cost_yuan": 0, "latency_ms": 0,
                "final_reply": None, "moderation": None,
            }
            return _finish(run_id, question, source, conv_id, created_at, pl, result,
                           v["errors"], retry_of, force_error_tool, emit, t0)

        # ---- 3) Executor（on_step → step 事件流式转发）----
        seen = []

        def on_step(entry):
            seen.append(entry.get("index", 0))
            _emit("step", entry)

        result = engine.execute(question, pl, db=None, on_step=on_step,
                                force_error_tool=force_error_tool)

        # _finalize 内部自动补全的话术/风控步骤没有走 on_step，这里把尾部补发一遍，
        # 保证前端 step 流完整（去重依据：这些补发步骤的 index 大于已见的最大 index）。
        tail = result.get("trace") or []
        max_seen = max(seen) if seen else 0
        for entry in tail:
            if entry.get("index", 0) > max_seen and entry.get("index", 0) not in seen:
                seen.append(entry.get("index", 0))
                _emit("step", entry)

        # ---- 4) reply / risk / error 事件 ----
        fr = result.get("final_reply")
        if fr:
            _emit("reply", fr)
        if result.get("risk"):
            _emit("risk", result["risk"])
        if not result.get("ok"):
            _emit("error", {"code": "execution_error",
                            "message": result.get("error") or "执行出错"})
        elif fr and fr.get("blocked"):
            _emit("error", {"code": "blocked", "message": result.get("blocked_reason")
                            or "最终回复未通过风控，已拦截"})
        return _finish(run_id, question, source, conv_id, created_at, plan_obj, result,
                       None, retry_of, force_error_tool, emit, t0)
    except Exception as e:  # noqa: BLE001  —— plan/execute 抛错也要落库，绝不假装成功
        message = str(e) or e.__class__.__name__
        _emit("error", {"code": "run_error", "message": message})
        result = {
            "ok": False, "error": message, "question": question, "plan": plan_obj,
            "warnings": [], "trace": [], "steps_count": 0, "usage": {},
            "cost_yuan": 0, "latency_ms": 0, "final_reply": None, "moderation": None,
        }
        return _finish(run_id, question, source, conv_id, created_at, plan_obj, result,
                       None, retry_of, force_error_tool, emit, t0)
    # 理论不可达（上面 try 内已全部 return），兜底返回 running 记录
    return store.get_run(run_id) or base


def _finish(run_id, question, source, conv_id, created_at, plan_obj, result,
            validation_errors, retry_of, force_error_tool, emit, t0):
    """把 engine 结果落成最终 RunRecord 并广播 done 事件。"""

    def _emit(etype, data):
        try:
            emit(etype, data)
        except Exception:
            pass

    duration_ms = int((time.time() - t0) * 1000)
    status = _derive_status(result)
    plan_obj = plan_obj or (result.get("plan") or {})
    fr = result.get("final_reply")
    risk = result.get("risk")
    snap = _snapshot()

    record = {
        "id": run_id,
        "question": question,
        "source": source,
        "conversationId": conv_id,
        "retryOf": retry_of or None,
        "createdAt": created_at,
        "status": status,
        "finalReply": fr,
        "plan": plan_obj,
        "steps": result.get("trace") or [],
        "riskResult": risk or (plan_obj.get("risk") if plan_obj else None),
        "risk": risk,
        "moderation": result.get("moderation"),
        "durationMs": duration_ms,
        "error": result.get("error"),
        "blockedReason": result.get("blocked_reason"),
        "provider": plan_obj.get("provider", ""),
        "model": plan_obj.get("model", ""),
        "warnings": result.get("warnings") or [],
        "validationErrors": validation_errors,
        "usage": result.get("usage") or {},
        "costYuan": result.get("cost_yuan") or 0,
        "latencyMs": result.get("latency_ms") or 0,
        "forceErrorTool": force_error_tool,
        "annotation": None,
        "handoff": None,
    }
    store.save_run_field(run_id, record)  # 原位更新，列表顺序稳定

    _emit("done", {
        "runId": run_id,
        "status": status,
        "question": question,
        "summary": (plan_obj or {}).get("summary", ""),
        "finalReply": fr,
        "provider": record["provider"],
        "model": record["model"],
        "risk": risk,
        "durationMs": duration_ms,
        "url": f"/runs/{run_id}",
    })
    return record


# ------------------------------------------------------------------ handoff / annotate / explain
def set_handoff(run_id: str, mode: str = "manual", note: str = ""):
    """人工接管：状态置 handoff 并记录原因（返回更新后的记录或 None）。"""
    return store.save_run_field(run_id, {
        "status": "handoff",
        "handoff": {"mode": mode, "note": note, "at": _now_iso()},
    })


def set_annotation(run_id: str, text: str, author: str = ""):
    return store.save_run_field(run_id, {
        "annotation": {"text": text, "author": author, "at": _now_iso()},
    })


def build_explanation(record) -> str:
    """基于 RunRecord 的中文解释器（纯代码、无外部 LLM）。"""
    if not isinstance(record, dict):
        return "未找到该运行记录。"
    status = record.get("status", "")
    status_txt = {"ok": "正常完成", "error": "执行出错", "degraded": "受限完成",
                  "handoff": "已转人工", "running": "运行中", "retry": "重试中"}.get(status, status)
    lines = [f"本轮运行状态：{status_txt}。"]
    plan = record.get("plan") or {}
    summary = plan.get("summary", "")
    if summary:
        lines.append(f"规划思路：{summary}")
    if record.get("provider"):
        lines.append(f"由 {record['provider']} / {record.get('model') or '-'} 生成，"
                     f"耗时 {record.get('durationMs', 0)}ms。")
    steps = record.get("steps") or []
    if steps:
        lines.append(f"共执行 {len(steps)} 个步骤：")
        for st in steps:
            tag = {"skill": "Skill", "tool": "Tool"}.get(st.get("type"), "步骤")
            st_text = f"· {st.get('index', '')}.{tag}「{st.get('name') or st.get('id')}」"
            s = st.get("status", "")
            if s == "ok":
                st_text += "：成功"
            elif s == "error":
                st_text += f"：失败（{st.get('error', '')}）"
            elif s == "skipped":
                st_text += "：跳过（风险命中）"
            else:
                st_text += f"：{s}"
            lines.append(st_text)
    if record.get("error"):
        lines.append(f"错误信息：{record['error']}")
    risk = record.get("risk") or record.get("riskResult")
    if isinstance(risk, dict) and risk.get("isRisky"):
        lines.append("命中风险场景：已执行风险识别与安全回复，未进行任何销售/收款/链接操作。")
    moderation = record.get("moderation")
    if isinstance(moderation, dict):
        passed = "通过" if moderation.get("pass") else "未通过（已拦截）"
        lines.append(f"风控审核：{passed}。")
    fr = record.get("finalReply")
    if isinstance(fr, dict) and fr.get("text"):
        lines.append(f"最终回复（节选）：{str(fr['text'])[:60]}…")
    elif not steps and record.get("status") == "error":
        pass
    return "\n".join(lines)


def run_sync(question: str, *, source=None, conversation_id=None):
    """一次性同步执行（测试/离线脚本用），返回最终 RunRecord。"""
    return run_run(question, source=source, conversation_id=conversation_id)


def reset_force_error(tool_id: str, on: bool):
    store.set_tool_force_error(tool_id, bool(on))
