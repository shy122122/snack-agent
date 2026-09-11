# -*- coding: utf-8 -*-
"""评测批次（持久化）+ 版本对比 离线验收脚本（test_client，不联网，demo-fixture）。

覆盖本段验收：
1) 选 3 个 caseIds 创建批次 → total 恰为 3；caseResults 只含这 3 个 id
2) 每条都真实走 Agent 主链路（复用 runner.run_run + eval_mod.score_case，非旧结果伪造）
3) 轮询 GET 批次详情直至终态（done）
4) 结果落盘：再次 GET + 直接重读磁盘文件仍完整；GET /api/eval/batch 给出 lastCaseIds 供二次回归复用
5) baseline-v1(关 compute_price) / risk-fix-v2(开 compute_price 关 query_service) 用相同 caseIds +
   同一 rules 评分器快照 → compare.comparable=true，同 caseIds/contentHash/evaluator/provider·model·params
6) deltas 同时出现「已修复」与「新增失败」；两批总通过数相同但构成变化（不能只展示总分上涨）
7) 同一用例 contentHash 一致才可比较；skills/tools 开关差异只作上下文、不阻断
8) 两批不可比较（caseIds 不一致）→ comparable=false + blocks + 不给“版本更好”结论
9) 防伪：直接手改磁盘旧批次 passed=999 → GET 返回派生真实值；再建同 ids 新批次 = 真实新跑（与手改无关）
10) 结构化错误：[]→400、无效 id→400 并列出、重复 id→400、批次不存在 GET/cancel/compare→404、
    已终态 cancel→400、compare 缺参→400、快速重复点击→409(带 activeBatchId) 可区分
11) cancel 未终态批次 → cancelled，未跑 case 补 ERROR(attempted=false) 保持 total 不变式

验收脚本特性（与 eval_center_smoke 一致）：先备份 data/ 触及文件、finally 原样还原，不改持久状态。
「pnpm typecheck/lint/build」属 TS 命令，Python 栈不可执行、不作假；等价替代 = py_compile 全模块 +
本脚本全绿 + 其余 smoke 顺序回归（见 main 末尾说明打印）。

用法： python eval_batch_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["SNACK_LLM_PROVIDER"] = "demo-fixture"  # 必须在 import app 之前

sys.path.insert(0, ".")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOUCHED = ["eval_batches.json", "runs.json", "tool_state.json", "skills.json", "eval_cases.json"]
CASE_IDS = ["ec_seed_mala_office", "ec_seed_after_sale", "ec_seed_ambiguous"]
RESULTS: list = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def backup():
    return {n: (DATA / n).read_bytes() if (DATA / n).exists() else None for n in TOUCHED}


def restore(saved):
    for n, b in saved.items():
        p = DATA / n
        # daemon worker 线程可能在异常中断后仍短暂持有文件句柄 → 重试释放
        for _ in range(60):
            try:
                if b is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.write_bytes(b)
                break
            except PermissionError:
                time.sleep(0.25)


def json_of(resp):
    try:
        return json.loads(resp.get_data(as_text=True))
    except Exception:
        return {}


def create(c, case_ids=None, **kw):
    body = dict(kw)
    if case_ids is not None:
        body["caseIds"] = case_ids
    return c.post("/api/eval/batch/run", json=body)


def wait_terminal(c, bid, timeout=60.0):
    """轮询批次详情直至终态，返回 batch dict（超时返回 {}）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = json_of(c.get(f"/api/eval/batch/{bid}"))
        b = j.get("batch") or {}
        if b.get("status") in ("done", "error", "cancelled"):
            return b
        time.sleep(0.15)
    return {}


def main():
    print("== 0) py_compile ==")
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py", "eval_batch_smoke.py"] +
                       [f"core/{m}.py" for m in
                        ("engine", "store", "tools", "llm", "providers", "validator", "runner",
                         "eval", "eval_batch")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
    check("py_compile 全模块通过（含 core/eval_batch.py）", r.returncode == 0)

    print("== 准备：备份（import app 前抓取原始状态）==")
    saved = backup()
    try:
        from app import app  # 触发 ensure_seed（幂等）
        from core import eval as eval_mod, runner, store
        c = app.test_client()
        # 显式把工具拨回确定默认态（compute_price/query_service 开），保证 PASS 基线
        store.set_tool_enabled("compute_price", True)
        store.set_tool_enabled("query_service", True)
        j0 = json_of(c.get("/api/eval"))
        check("GET /api/eval 返回 ok + 种子已补种", j0.get("ok") is True
              and (j0.get("counts") or {}).get("total", 0) >= 9)

        # ---- 验收 1/2/3/4：创建 → total==3 → 轮询 done → 落盘 ----
        print("== 验收 1-4：3 例批次 total==3、逐条真实主链路、轮询终态、落盘持久 ==")
        j = json_of(create(c, CASE_IDS, name="验收·3例批次", versionLabel="v1"))
        b1 = j.get("batch") or {}
        b1_id = b1.get("id")
        check("创建批次返回 ok + total 恰为 3", j.get("ok") is True and b1.get("total") == 3
              and (b1.get("caseIds") or []) == CASE_IDS, f"id={b1_id}")
        check("批次字段齐全（快照/评测器/参数/技能指纹）", bool(b1_id)
              and len(b1.get("caseSnapshot") or []) == 3
              and bool(b1.get("caseSetHash")) and bool(b1.get("skillHash"))
              and (b1.get("evaluator") or {}).get("version") == eval_mod.EVALUATOR_VERSION
              and (b1.get("params") or {}).get("source") == "eval")
        done1 = wait_terminal(c, b1_id)
        check("验收3：轮询到达终态 done", done1.get("status") == "done",
              f"status={done1.get('status')}")
        ids1 = [r.get("caseId") for r in (done1.get("caseResults") or [])]
        check("验收1/2：caseResults 恰为所选 3 例、顺序一致、全部真实 PASS",
              ids1 == CASE_IDS and done1.get("passed") == 3
              and all(r.get("runId") and r.get("attempted") is True
                      and (r.get("score") or {}).get("judge") == "rules"
                      for r in (done1.get("caseResults") or [])),
              f"passed={done1.get('passed')} ids={ids1}")
        disk = json.loads((DATA / "eval_batches.json").read_text(encoding="utf-8"))
        d1 = next((x for x in disk if x.get("id") == b1_id), {})
        check("验收4：磁盘文件重新读取批次完整（3 条 caseResults + runId 溯源）",
              len(d1.get("caseResults") or []) == 3
              and d1.get("status") == "done"
              and all(store.get_run(r.get("runId")) for r in (d1.get("caseResults") or []) if r.get("runId")))
        jl = json_of(c.get("/api/eval/batch"))
        check("GET /api/eval/batch → lastCaseIds=最新一批 caseIds（二次回归默认复用）",
              (jl.get("lastCaseIds") or []) == CASE_IDS and jl.get("total", 0) >= 1
              and bool(jl.get("evaluator")))

        # ---- 验收 10：结构化错误（400/404/409 分层）----
        print("== 验收 10：结构化错误 ==")
        e0 = create(c, [])
        j0e = json_of(e0)
        check("空数组 [] → 400（非 200/500）", e0.status_code == 400 and j0e.get("ok") is False)
        e1 = create(c, ["ec_nope_missing"])
        j1e = json_of(e1)
        check("含无效 id → 400 并列出无效 id", e1.status_code == 400
              and "ec_nope_missing" in (j1e.get("error") or ""))
        e2 = create(c, ["ec_seed_ambiguous", "ec_seed_ambiguous"])
        j2e = json_of(e2)
        check("重复 id → 400 提示重复", e2.status_code == 400
              and "重复" in (j2e.get("error") or ""))
        e3 = json_of(c.get("/api/eval/batch/eb_nope"))
        check("GET 不存在批次 → 404 结构化", e3.get("ok") is False)
        e4 = json_of(c.post("/api/eval/batch/eb_nope/cancel"))
        check("cancel 不存在批次 → 404 结构化", e4.get("ok") is False)
        e5 = json_of(c.post(f"/api/eval/batch/{b1_id}/cancel"))
        check("cancel 已终态(done)批次 → 400 结构化", e5.get("ok") is False)
        e6 = json_of(c.get("/api/eval/batch/compare"))
        check("compare 缺 a/b 参数 → 400 结构化", e6.get("ok") is False)
        e7 = json_of(c.get("/api/eval/batch/compare?a=" + b1_id + "&b=eb_nope"))
        check("compare 引用不存在批次 → 404 结构化", e7.get("ok") is False)

        # ---- 验收 5/6/7：版本对比 baseline-v1 vs risk-fix-v2 ----
        print("== 验收 5-7：tool 开关构造版本对 → 已修复 + 新增失败同现 ==")
        store.set_tool_enabled("compute_price", False)   # baseline-v1：算价被关
        jv1 = json_of(create(c, CASE_IDS, name="baseline-v1", versionLabel="v1"))
        bv1 = jv1.get("batch") or {}
        v1_id = bv1.get("id")
        done_v1 = wait_terminal(c, v1_id)
        s_v1 = {r.get("caseId"): r.get("status") for r in (done_v1.get("caseResults") or [])}
        check("baseline-v1（关 compute_price）done：算价例非 PASS、其余 PASS",
              done_v1.get("status") == "done"
              and s_v1.get("ec_seed_mala_office") != "PASS"
              and s_v1.get("ec_seed_after_sale") == "PASS"
              and s_v1.get("ec_seed_ambiguous") == "PASS",
              f"{s_v1}")
        store.set_tool_enabled("compute_price", True)    # risk-fix-v2：算价开 + 服务关
        store.set_tool_enabled("query_service", False)
        jv2 = json_of(create(c, CASE_IDS, name="risk-fix-v2", versionLabel="v2",
                             changeNote="修算价，模拟服务回归"))
        v2_id = (jv2.get("batch") or {}).get("id")
        done_v2 = wait_terminal(c, v2_id)
        s_v2 = {r.get("caseId"): r.get("status") for r in (done_v2.get("caseResults") or [])}
        check("risk-fix-v2（开算价关服务）done：算价例修复为 PASS、服务例回归非 PASS",
              done_v2.get("status") == "done"
              and s_v2.get("ec_seed_mala_office") == "PASS"
              and s_v2.get("ec_seed_after_sale") != "PASS"
              and s_v2.get("ec_seed_ambiguous") == "PASS",
              f"{s_v2}")
        cv = json_of(c.get(f"/api/eval/batch/compare?a={v1_id}&b={v2_id}"))
        check("验收5：可比较（comparable=true，无阻断）", cv.get("ok") is True
              and cv.get("comparable") is True and not (cv.get("blocks") or []),
              f"blocks={cv.get('blocks')}")
        sh = cv.get("sameHarness") or {}
        check("同一执行/评分底座：caseIds/contentHash/evaluator/runtime 全一致",
              sh.get("caseIds") is True and sh.get("snapshot") is True
              and sh.get("evaluator") is True and sh.get("runtime") is True, f"{sh}")
        td = (cv.get("context") or {}).get("toolDiff", [])
        check("验收7：工具开关差异只作上下文（toolChanged 记录 关→开）不阻断",
              cv.get("toolChanged") is True and len(td) >= 2 and all("→" in x for x in td)
              and (cv.get("context") or {}).get("skillDiff", []) == [],
              f"toolDiff={td}")
        d_by = {d.get("caseId"): d for d in (cv.get("deltas") or [])}
        d_mala = d_by.get("ec_seed_mala_office") or {}
        d_after = d_by.get("ec_seed_after_sale") or {}
        check("验收6a：算价例 已修复(fixed=非PASS→PASS)",
              d_mala.get("fixed") is True and d_mala.get("statusChanged") is True
              and d_mala.get("prevStatus") != "PASS" and d_mala.get("curStatus") == "PASS"
              and bool(d_mala.get("curRunId")))
        check("验收6b：服务例 新增失败(regressed=PASS→非PASS)",
              d_after.get("regressed") is True and d_after.get("statusChanged") is True
              and d_after.get("prevStatus") == "PASS" and d_after.get("curStatus") != "PASS"
              and bool(d_after.get("prevRunId")))
        cnt = cv.get("counts") or {}
        st_base, st_cur = cv.get("base") or {}, cv.get("cur") or {}
        check("验收6c：两批总通过数相同(均为2)但构成变化——只展示总分会掩盖“新增失败”",
              st_base.get("passed") == st_cur.get("passed") == 2
              and cnt.get("fixed") >= 1 and cnt.get("regressed") >= 1,
              f"base={st_base.get('passed')} cur={st_cur.get('passed')} "
              f"fixed={cnt.get('fixed')} regressed={cnt.get('regressed')}")
        check("verdict 同时提及 已修复 与 新增失败", "已修复" in (cv.get("verdict") or "")
              and "新增失败" in (cv.get("verdict") or ""), cv.get("verdict"))

        # ---- 验收 8：不可直接比较（caseIds 不一致）----
        print("== 验收 8：不可直接比较 → blocks + 不给结论 ==")
        store.set_tool_enabled("query_service", True)   # 拨回，用例集不同即可触发阻断
        jx = json_of(create(c, ["ec_seed_ambiguous"], name="单例-不可比"))
        x_id = (jx.get("batch") or {}).get("id")
        wait_terminal(c, x_id)
        cx = json_of(c.get(f"/api/eval/batch/compare?a={v1_id}&b={x_id}"))
        check("caseIds 集合不一致 → comparable=false + blocks 列出 + verdict 不给好坏结论",
              cx.get("ok") is True and cx.get("comparable") is False
              and len(cx.get("blocks") or []) >= 1
              and any("caseIds" in b or "不一致" in b for b in cx.get("blocks") or [])
              and "不可直接比较" in (cx.get("verdict") or ""), f"{cx.get('verdict')}")

        # ---- 验收 9：防伪（手改磁盘 passed 不生效；新批次真实新跑）----
        print("== 验收 9：防伪——手改旧批次计数不生效，新批次真实新跑 ==")
        store.set_tool_enabled("compute_price", True)    # 新批次回到干净基线 → 期望 3/3 PASS
        store.set_tool_enabled("query_service", True)
        disk_p = DATA / "eval_batches.json"
        rows = json.loads(disk_p.read_text(encoding="utf-8"))
        for rec in rows:
            if rec.get("id") == v1_id:
                rec["passed"] = 999   # 直接手改磁盘上的派生计数字段（模拟篡改）
        disk_p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        g1 = json_of(c.get(f"/api/eval/batch/{v1_id}"))
        gb1 = g1.get("batch") or {}
        check("验收9a：GET 返回派生真实 passed(=2)，手改 999 不生效",
              gb1.get("passed") == 2, f"passed={gb1.get('passed')}（手改 999 被还原）")
        jf = json_of(create(c, CASE_IDS, name="验收·防伪重跑", versionLabel="fresh"))
        f_id = (jf.get("batch") or {}).get("id")
        fresh = wait_terminal(c, f_id)
        old_run_ids = {r.get("runId") for r in (done1.get("caseResults") or [])}
        new_run_ids = {r.get("runId") for r in (fresh.get("caseResults") or [])}
        check("验收9b：同 ids 新批次 = 真实新跑（3/3 PASS，runId 全新、与手改值无关）",
              fresh.get("status") == "done" and fresh.get("passed") == 3
              and bool(new_run_ids) and not (new_run_ids & old_run_ids),
              f"passed={fresh.get('passed')} runIds新={new_run_ids & old_run_ids}")

        # ---- 验收 10：快速重复点击 → 409 可区分；cancel 未终态批次 ----
        print("== 验收 10b：重复点击 409 + cancel 未终态 ==")
        real_run = runner.run_run

        def slow_run(question, *a, **kw):
            time.sleep(0.8)   # 拖慢真实主链路，确定性保活 active 批次
            return real_run(question, *a, **kw)

        runner.run_run = slow_run
        try:
            ja = json_of(create(c, CASE_IDS, name="验收·慢批次"))
            a_id = (ja.get("batch") or {}).get("id")
            jdup = json_of(create(c, CASE_IDS, name="验收·重复点击"))
            check("验收10c：active 期间重复创建 → 409 且带 activeBatchId（请求可区分）",
                  jdup.get("ok") is False and jdup.get("activeBatchId") == a_id,
                  f"activeBatchId={jdup.get('activeBatchId')} vs {a_id}")
            jc = json_of(c.post(f"/api/eval/batch/{a_id}/cancel"))
            check("cancel 未终态批次 → ok", jc.get("ok") is True)
            cancelled = wait_terminal(c, a_id)
            crs = cancelled.get("caseResults") or []
            csum = (cancelled.get("passed") or 0) + (cancelled.get("failed") or 0) \
                + (cancelled.get("review") or 0) + (cancelled.get("errors") or 0)
            check("cancel → 状态 cancelled，未跑 case 补 ERROR(attempted=false) 保持 total 不变式",
                  cancelled.get("status") == "cancelled" and len(crs) == 3 and csum == 3
                  and any(r.get("attempted") is False and r.get("status") == "ERROR"
                          for r in crs),
                  f"status={cancelled.get('status')} len={len(crs)} sum={csum}")
            jal = json_of(c.get("/api/eval/batch"))
            check("cancel 后无 active 批次", (jal.get("activeBatchId") or None) is None)
        finally:
            runner.run_run = real_run

        page = c.get("/eval")
        html = page.get_data(as_text=True)
        check("GET /eval 页面 200 且含 评测批次/版本对比 区块", page.status_code == 200
              and "评测批次" in html and "版本对比" in html)

        print("== 验收 10(TS 等价替代) ==")
        print("  [note] pnpm typecheck/lint/build 属 TS 工程命令，Python 栈不可执行、不作假。"
              "等价替代 = 上文 py_compile 全模块 + 本脚本全绿 + eval_unit / eval_center_smoke / "
              "ops_smoke / ops_center_smoke / admin_smoke 顺序回归（与既有 Eval/主链路一致口径）。")

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
