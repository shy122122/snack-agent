# -*- coding: utf-8 -*-
"""评测中心(/eval)离线验收脚本（test_client，不联网，classroom-fixture）。

覆盖本段验收：
1) 新增 EvalCase 后“刷新”仍在（写盘可被重新读取，非内存态）
2) 修改 expectedKeywords 真实改变评分（同一条回复：期望『到手价』FAIL → 期望『请问』PASS）
3) delete / disable 会把用例从默认启用集(default_enabled_set)中移除
4) 单条测试返回真实 Agent 回复（=同问题直连 runner.run_run 结果，非静态样例）且
   评分证据可溯源到该 runId（runs.json 该 Run 记录上挂 eval 评分）
5) 工具故障注入 → score=ERROR 而非 FAIL；批跑 ERROR 不计入产品质量 FAIL(productFailExclError)
6) 由 Run 沉淀入评测集 / 复制 / 过滤 / 结构化错误(非 500)
7) judge=rules 诚实标注（填了 llmJudgePrompt 也不伪装成 LLM 判分）

特点：先备份 data 下触及文件，结束时原样还原，不改动任何持久状态。
用法： python eval_center_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["SNACK_LLM_PROVIDER"] = "classroom-fixture"  # 必须在 import app 之前

sys.path.insert(0, ".")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOUCHED = ["eval_cases.json", "runs.json", "tool_state.json"]
RESULTS: list = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def backup():
    return {n: (DATA / n).read_bytes() if (DATA / n).exists() else None for n in TOUCHED}


def restore(saved):
    for n, b in saved.items():
        p = DATA / n
        if b is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(b)


def json_of(resp):
    try:
        return json.loads(resp.get_data(as_text=True))
    except Exception:
        return {}


def main():
    print("== 0) py_compile ==")
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py", "eval_unit.py",
                        "eval_center_smoke.py"] +
                       [f"core/{m}.py" for m in
                        ("engine", "store", "tools", "llm", "providers", "validator", "runner", "eval")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
    check("py_compile 全模块通过", r.returncode == 0)

    print("== 准备：备份（import app 前抓取原始状态）==")
    saved = backup()
    try:
        from app import app  # 触发 ensure_seed（幂等）
        from core import eval as eval_mod, runner, store
        c = app.test_client()
        # 以种子落库后为基线（备份早于 import，restore 仍回到原始字节）
        j0 = json_of(c.get("/api/eval"))
        base_total = (j0.get("counts") or {}).get("total", 0)
        base_enabled = (j0.get("counts") or {}).get("enabled", 0)
        check("GET /api/eval 返回 ok + 种子已补种", j0.get("ok") is True and base_total >= 9,
              f"total={base_total}")

        # ---- 1) 新增用例 · 刷新后仍在 ----
        print("== 验收 1：新增用例持久（刷新仍可读）==")
        j = json_of(c.post("/api/eval/cases", json={
            "name": "验收·新增持久化用例", "question": "随便来点追剧吃的辣的",
            "category": "推荐", "difficulty": "easy", "riskLevel": "low",
            "expectRisk": False,
            "expectedKeywords": [{"words": ["请问"], "mode": "any"}]}))
        c1 = j.get("case") or {}
        check("POST /api/eval/cases 返回 ok + 带 id/默认字段",
              j.get("ok") is True and bool(c1.get("id")) and c1.get("enabled") is True)
        disk = json.loads((DATA / "eval_cases.json").read_text(encoding="utf-8"))
        check("用例真实写盘（磁盘文件含新 id）", any(x.get("id") == c1.get("id") for x in disk))
        jr = json_of(c.get(f"/api/eval/cases/{c1.get('id')}"))
        check("刷新后 GET 详情仍在", (jr.get("case") or {}).get("id") == c1.get("id"))
        jr2 = json_of(c.get("/api/eval?limit=1000"))
        check("刷新后 GET 列表仍在", any((x or {}).get("id") == c1.get("id") for x in (jr2.get("cases") or [])))

        # ---- 2) 修改 expectedKeywords 真实改变评分 ----
        print("== 验收 2：expectedKeywords 决定评分（改词即改结果）==")
        amb = json_of(c.get("/api/eval/cases/ec_seed_ambiguous"))
        amb_q = (amb.get("case") or {}).get("question")
        j = json_of(c.post("/api/eval/cases", json={
            "name": "验收·期望词变化", "question": amb_q, "category": "模糊需求",
            "difficulty": "easy", "riskLevel": "low", "expectRisk": False,
            "expectedKeywords": [{"words": ["到手价"], "mode": "any"}]}))
        cg = j.get("case") or {}
        run1 = json_of(c.post(f"/api/eval/cases/{cg.get('id')}/run", json={}))
        s1 = run1.get("score") or {}
        check("期望『到手价』→ FAIL（回复是问候不含该词）",
              s1.get("status") == "FAIL" and "到手价" in (s1.get("keywordMisses") or []),
              s1.get("reason"))
        # 直接跑同 question 对比 → 证明是真实主链路回复（非静态样例）
        off = runner.run_run(amb_q, source="smoke_eval", conversation_id="smoke-eval-amb")
        off_text = ((off.get("finalReply") or {}).get("text") or "").strip()
        on_text = (((run1.get("run") or {}).get("finalReply") or {}).get("text") or "").strip()
        check("单条测试回复 = 同问题直连主链路回复（真实而非样例）",
              bool(off_text) and off_text == on_text and off_text != "（样例）")
        j = json_of(c.patch(f"/api/eval/cases/{cg.get('id')}",
                            json={"expectedKeywords": [{"words": ["请问"], "mode": "any"}],
                                  "llmJudgePrompt": "请用大模型评审（示例通道，未接通不启用）"}))
        cg2 = j.get("case") or {}
        run2 = json_of(c.post(f"/api/eval/cases/{cg.get('id')}/run", json={}))
        s2 = run2.get("score") or {}
        check("改期望词为『请问』→ PASS（评分逻辑真的随词变）",
              s2.get("status") == "PASS", s2.get("reason"))
        check("填 llmJudgePrompt 仍 judge=rules + judgeNote（不伪装 LLM 判分）",
              s2.get("judge") == "rules" and bool(s2.get("judgeNote")) and cg2.get("llmJudgePrompt"))

        # ---- 3) delete / disable 移出默认启用集 ----
        print("== 验收 3：delete/disable 移出默认启用集 ==")
        base_jl = json_of(c.get("/api/eval"))
        tot_before = (base_jl.get("counts") or {}).get("total", 0)
        en_before = (base_jl.get("counts") or {}).get("enabled", 0)
        j = json_of(c.post("/api/eval/cases", json={
            "name": "验收·待删除用例", "question": "删我", "enabled": True}))
        cd = j.get("case") or {}
        jl = json_of(c.get("/api/eval"))
        en_after_add = (jl.get("counts") or {}).get("enabled", 0)
        check("新增默认启用 → 进入默认启用集", cd.get("id") in (jl.get("enabledIds") or [])
              and en_after_add == en_before + 1, f"enabled={en_before}→{en_after_add}")
        j = json_of(c.patch(f"/api/eval/cases/{cd.get('id')}", json={"enabled": False}))
        check("PATCH enabled=false 成功", j.get("ok") is True)
        jl = json_of(c.get("/api/eval"))
        check("禁用后移出默认启用集 & enabled 计数回落",
              cd.get("id") not in (jl.get("enabledIds") or [])
              and (jl.get("counts") or {}).get("enabled") == en_before)
        jf = json_of(c.get("/api/eval?enabled=1&limit=1000"))
        check("enabled=1 过滤不含该禁用用例",
              all((x or {}).get("id") != cd.get("id") for x in (jf.get("cases") or [])))
        j = json_of(c.delete(f"/api/eval/cases/{cd.get('id')}"))
        check("DELETE 用例成功", j.get("ok") is True)
        jl = json_of(c.get("/api/eval"))
        check("删除后列表/计数恢复",
              all((x or {}).get("id") != cd.get("id") for x in (jl.get("cases") or []))
              and (jl.get("counts") or {}).get("total") == tot_before)

        # ---- 4) 单条测试真实回复 + 证据可溯源到 runId ----
        print("== 验收 4：单条测试真实回复 & 证据溯源 ==")
        pk = json_of(c.get("/api/eval/cases/ec_seed_price_check"))
        pk_q = (pk.get("case") or {}).get("question")
        off2 = runner.run_run(pk_q, source="smoke_eval", conversation_id="smoke-eval-price")
        check("直连跑价格问题 status=ok 且含 compute_price",
              off2.get("status") == "ok"
              and any((s or {}).get("id") == "compute_price" for s in (off2.get("steps") or [])))
        rj = json_of(c.post("/api/eval/cases/ec_seed_price_check/run", json={}))
        run_id = rj.get("runId")
        s4 = rj.get("score") or {}
        run_rec = rj.get("run") or {}
        on_text2 = (((run_rec).get("finalReply") or {}).get("text") or "").strip()
        off_text2 = (((off2).get("finalReply") or {}).get("text") or "").strip()
        check("单条测试：reply=真实主链路结果(price PASS)",
              rj.get("ok") is True and bool(on_text2) and off_text2 == on_text2
              and s4.get("status") == "PASS" and on_text2 != "（样例）")
        check("score.runId == run.id 且 caseId 一致",
              s4.get("runId") == run_id and s4.get("caseId") == "ec_seed_price_check")
        rec_persist = store.get_run(run_id) if run_id else None
        ev = (rec_persist or {}).get("eval") or {}
        check("评分持久进该 Run 记录（可溯源：eval.runId/caseId + Trace 完整）",
              bool(rec_persist) and ev.get("runId") == run_id
              and ev.get("caseId") == "ec_seed_price_check"
              and len(rec_persist.get("steps") or []) >= 1
              and (((rec_persist.get("finalReply") or {}).get("text") or "").strip()) == on_text2)

        # ---- 5) 故障注入 → ERROR（不是 FAIL），批跑 ERROR 不计产品 FAIL ----
        print("== 验收 5：工具故障注入 = ERROR，批跑排除 ERROR ==")
        runner.reset_force_error("compute_price", True)
        try:
            fj = json_of(c.post("/api/eval/cases/ec_seed_mala_office/run", json={}))
            fs = fj.get("score") or {}
            frun = fj.get("run") or {}
            err_step = [st for st in (frun.get("steps") or [])
                        if st.get("id") == "compute_price" and st.get("status") == "error"]
            check("compute_price 故障 → run.status=error, score=ERROR 且带 error（非 FAIL）",
                  frun.get("status") == "error" and fs.get("status") == "ERROR"
                  and bool(fs.get("error")) and bool(err_step), fs.get("reason"))
            bj = json_of(c.post("/api/eval/batch-run",
                                json={"caseIds": ["ec_seed_mala_office"]}))
            bs = bj.get("summary") or {}
            first = ((bj.get("results") or []) + [{}])[0]
            check("批跑结果 ERROR 单独计数，不计入产品质量 FAIL",
                  first.get("status") == "ERROR" and bs.get("errorCount") >= 1
                  and bs.get("productFailExclError") == 0,
                  f"summary={json.dumps(bs, ensure_ascii=False)}")
        finally:
            runner.reset_force_error("compute_price", False)

        # ---- 6) 由 Run 沉淀 / 复制 / 过滤 / 结构化错误 ----
        print("== 验收 6：从 Run 沉淀、复制、过滤、结构化错误 ==")
        j = json_of(c.post("/api/eval/cases", json={
            "fromRunId": off2.get("id"), "name": "验收·由运行沉淀"}))
        cr = j.get("case") or {}
        check("由 Run 沉淀：question/sourceRunId 取自真实运行",
              j.get("ok") is True and cr.get("question") == pk_q
              and cr.get("sourceRunId") == off2.get("id"))
        j = json_of(c.post("/api/eval/cases/ec_seed_scam_lottery/copy",
                           json={"withName": "诈骗(验收副本)"}))
        cn = j.get("case") or {}
        check("复制用例：新 id/副本名/保留 expectRisk=True",
              j.get("ok") is True and cn.get("id") != "ec_seed_scam_lottery"
              and cn.get("name") == "诈骗(验收副本)" and cn.get("expectRisk") is True
              and not cn.get("sourceRunId"))
        fc = json_of(c.get("/api/eval?category=风险安全&limit=1000"))
        check("按 category=风险安全 过滤含诈骗种子",
              any((x or {}).get("id") == "ec_seed_scam_lottery" for x in (fc.get("cases") or []))
              and all((x or {}).get("category") == "风险安全" for x in (fc.get("cases") or [])))
        fq = json_of(c.get("/api/eval?q=5888&limit=1000"))
        check("按 q=5888 搜索到中奖诈骗种子",
              any((x or {}).get("id") == "ec_seed_scam_lottery" for x in (fq.get("cases") or [])))
        fd = json_of(c.get("/api/eval?dim=reply_safety&limit=1000"))
        ids_d = [x.get("id") for x in (fd.get("cases") or [])]
        check("按 dim=reply_safety 过滤含风险用例且不含无关推荐",
              "ec_seed_scam_lottery" in ids_d and "ec_seed_mala_office" not in ids_d)

        print("== 验收 7：接口越界/非法入参 → 结构化错误而非 500 ==")
        b1 = json_of(c.post("/api/eval/cases", json={}))
        check("缺 name/question 创建 → {ok:false}", b1.get("ok") is False)
        b2 = json_of(c.post("/api/eval/cases", json={"name": "x", "question": "y", "category": "不存在"}))
        check("非法 category → {ok:false}", b2.get("ok") is False)
        b3 = json_of(c.post("/api/eval/cases", json={"name": "x", "question": "y", "expectRisk": "maybe"}))
        check("非法 expectRisk → {ok:false}", b3.get("ok") is False)
        b4 = json_of(c.patch("/api/eval/cases/ec_nope", json={"name": "z"}))
        check("PATCH 不存在用例 → {ok:false}", b4.get("ok") is False)
        b5 = json_of(c.delete("/api/eval/cases/ec_nope"))
        check("DELETE 不存在用例 → {ok:false}", b5.get("ok") is False)
        b6 = json_of(c.post("/api/eval/cases/ec_nope/copy", json={}))
        check("COPY 不存在用例 → {ok:false}", b6.get("ok") is False)
        b7 = json_of(c.post("/api/eval/cases", json={"fromRunId": "run_nope"}))
        check("fromRun 引用不存在 → {ok:false}", b7.get("ok") is False)
        b8 = json_of(c.post("/api/eval/batch-run", json={"caseIds": ["ec_nope"]}))
        check("批跑空有效用例 → {ok:false}", b8.get("ok") is False)
        page = c.get("/eval")
        check("GET /eval 页面 200（静态页不受接口故障影响）", page.status_code == 200
              and "评测中心" in page.get_data(as_text=True))

        print("\n== 还原备份 ==")
    finally:
        restore(saved)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  未通过: {name}" + (f"  — {detail}" if detail else ""))
    if passed != len(RESULTS):
        print("存在失败断言（不伪装通过）")
        sys.exit(1)


if __name__ == "__main__":
    main()
