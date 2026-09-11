# -*- coding: utf-8 -*-
"""运营中心(/ops)离线验收脚本（test_client，不联网，classroom-fixture）。

覆盖本段验收 1-9：
1) 从 Run 创建一条低分评分（自动进坏例）
2) 评分在运营中心可见（列表 + overview 低分 + 低分聚类样本）
3) 创建标注并“刷新”（二次 GET）仍持久存在
4) 导出标注数据（JSON/CSV）字段正确
5) 低分样本可一键沉淀为改进建议
6) 生成改进草稿绝不自动覆盖生产 Skill 提示词
7) 应用改进前可创建版本快照（且应用本身自动留档应用前快照）
8) 版本回滚恢复旧正文（改进回滚 + Skill 直连回滚）
9) 运营接口失败 / 越界入参返回结构化错误而非 500，页面仍可返回（不崩整页）

特点：先备份、结束时原样还原，不改动 data/ 下任何持久状态。
用法： python ops_center_smoke.py
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
TOUCHED = ["ops_ratings.json", "ops_annotations.json", "ops_improvements.json",
           "ops_ab_tests.json", "runs.json", "skills.json", "skill_versions.json"]
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
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py", "ops_center_smoke.py"] +
                       [f"core/{m}.py" for m in
                        ("engine", "store", "tools", "llm", "providers", "validator", "runner", "ops")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
    check("py_compile 全模块通过", r.returncode == 0)

    print("== 准备：备份 + 造一条正常运行 ==")
    saved = backup()
    try:
        from app import app
        from core import ops as ops_mod, runner, store
        c = app.test_client()

        rec = runner.run_run("帮我推荐点追剧吃的零食，预算50，要两三样", source="smoke_ops")
        rid = rec.get("id")
        check("造出一条可用运行记录(status=ok)", rec.get("status") == "ok", f"runId={rid}")
        prompt_before = (store.get_skill("reply") or {}).get("prompt") or ""

        # ---- 1) 从 Run 创建低分评分（≤2 自动进坏例）----
        print("== 验收 1：从 Run 创建低分评分 ==")
        j = json_of(c.post("/api/ratings", json={"runId": rid, "score": 2,
                                                 "problemType": "价格金额",
                                                 "comment": "金额叙述与明细不一致（验收样例）"}))
        rating = j.get("rating") or {}
        check("POST /api/ratings 返回 ok", j.get("ok") is True, json.dumps(j, ensure_ascii=False)[:120])
        check("评分绑定 runId 且自动标 badcase", rating.get("runId") == rid and rating.get("badcase") is True,
              f"ratingId={rating.get('id')}")

        # ---- 2) 评分在运营中心可见 ----
        print("== 验收 2：评分在运营中心可见 ==")
        j = json_of(c.get("/api/ratings?limit=50"))
        ids = [x.get("id") for x in j.get("ratings") or []]
        check("GET /api/ratings 列表含该评分", rating.get("id") in ids)
        ov = json_of(c.get("/api/ops/overview"))
        m = ov.get("metrics") or {}
        check("overview 低分统计 ≥1", (m.get("ratings") or {}).get("low", 0) >= 1, f"low={(m.get('ratings') or {}).get('low')}")
        low_cluster = next((cl for cl in (ov.get("clusters") or [])
                            if cl.get("key") == "low_rating"), None)
        has_sample = any((s or {}).get("ratingId") == rating.get("id")
                         for s in ((low_cluster or {}).get("samples") or []))
        check("低分聚类(规则聚类)样本含该评分", bool(low_cluster and has_sample))
        check("聚类方法恒标注为规则聚类", all((cl or {}).get("method") == "规则聚类" for cl in (ov.get("clusters") or [])))

        # ---- 3) 创建标注并刷新仍存在 ----
        print("== 验收 3：创建标注，刷新后仍在 ==")
        j = json_of(c.post("/api/annotations", json={
            "runId": rid, "dimensions": {"correctness": 3, "relevance": 3, "completeness": 2,
                                         "safety": 5, "tone": 2, "overall": 2},
            "status": "pending", "annotator": "验收员", "note": "回复没覆盖到数量问题(验收样例)"}))
        ann = j.get("annotation") or {}
        check("POST /api/annotations 返回 ok", j.get("ok") is True)
        check("标注记录字段齐全(runId/维度/状态)", bool(ann.get("id")) and ann.get("runId") == rid
              and (ann.get("dimensions") or {}).get("overall") == 2 and ann.get("status") == "pending")
        j2 = json_of(c.get(f"/api/annotations/{ann.get('id')}"))
        check("GET /api/annotations/<id> 能读到", (j2.get("annotation") or {}).get("id") == ann.get("id"))
        j3 = json_of(c.get("/api/annotations?status=pending"))
        check("刷新(重新 GET 列表)后仍在", any((x or {}).get("id") == ann.get("id") for x in (j3.get("annotations") or [])))
        # 状态流转
        j4 = json_of(c.put(f"/api/annotations/{ann.get('id')}", json={"status": "accepted"}))
        check("标注状态可流转 pending→accepted", (j4.get("annotation") or {}).get("status") == "accepted")

        # ---- 4) 导出 ----
        print("== 验收 4：导出标注数据 ==")
        exp = c.get("/api/annotations/export?format=json")
        check("导出 JSON HTTP 200", exp.status_code == 200)
        rows = json.loads(exp.get_data(as_text=True))
        row0 = next((x for x in rows if x.get("id") == ann.get("id")), None)
        check("JSON 导出含该标注且字段正确",
              bool(row0) and row0.get("runId") == rid and isinstance(row0.get("dimensions"), dict)
              and "question" in row0 and "status" in row0 and "annotator" in row0)
        csvr = c.get("/api/annotations/export?format=csv")
        csvtext = csvr.get_data(as_text=True)
        head_ok = all(h in csvtext for h in ("id", "runId", "status", "annotator",
                                             "correctness", "relevance", "overall", "question"))
        check("CSV 导出表头含 runId/六维度/question 等字段", csvr.status_code == 200 and head_ok)
        check("CSV 正文含中文标注说明", "验收样例" in csvtext)

        # ---- 5) 低分样本沉淀为改进建议 ----
        print("== 验收 5：低分样本进入改进建议 ==")
        j = json_of(c.post("/api/improvements", json={"fromRating": rating.get("id")}))
        imp = j.get("improvement") or {}
        check("POST /api/improvements(fromRating) 返回 ok", j.get("ok") is True)
        check("建议 kind=rating 且关联来源评分", imp.get("kind") == "rating"
              and (imp.get("sourceRef") or {}).get("id") == rating.get("id"))
        check("建议带样例 Run 引用", rid in (imp.get("sampleRunIds") or []))
        check("建议目标推断为 Skill reply（规则法）",
              any((t or {}).get("type") == "skill" and (t or {}).get("id") == "reply"
                  for t in (imp.get("targets") or [])), json.dumps(imp.get("targets"), ensure_ascii=False))
        gl = json_of(c.get("/api/improvements"))
        check("GET /api/improvements 可见该建议", any((x or {}).get("id") == imp.get("id") for x in (gl.get("improvements") or [])))

        # ---- 6) 生成草稿不覆盖生产提示词 ----
        print("== 验收 6：生成草稿不自动覆盖 ==")
        j = json_of(c.post("/api/improvements/generate-prompt", json={"improvementId": imp.get("id")}))
        draft = (j.get("improvement") or {}).get("draft") or {}
        check("generate-prompt 返回规则草稿文本", j.get("ok") is True and bool((draft or {}).get("text")))
        check("草稿 generator 恒标注为规则法", (draft or {}).get("generator") == "规则草稿")
        after_gen = (store.get_skill("reply") or {}).get("prompt") or ""
        check("生成草稿后生产提示词未被改动", after_gen == prompt_before)
        # 无确认应用被拒
        j_rej = json_of(c.post("/api/improvements/apply-prompt", json={"improvementId": imp.get("id"), "confirm": False}))
        after_rej = (store.get_skill("reply") or {}).get("prompt") or ""
        check("未确认(confirm=false)应用被结构化拒绝且不写盘",
              j_rej.get("ok") is False and after_rej == prompt_before)

        # ---- 7) 应用前可创建版本快照 + 应用自动留档 ----
        print("== 验收 7：应用前版本快照 ==")
        n0 = len(store.skill_versions("reply"))
        j = json_of(c.post("/api/skills/reply/snapshot", json={"note": "应用改进前快照(验收样例)"}))
        check("POST /api/skills/<sid>/snapshot 建快照成功",
              j.get("ok") is True and len(store.skill_versions("reply")) == n0 + 1, f"version={j.get('version')}")
        j = json_of(c.post("/api/improvements/apply-prompt", json={"improvementId": imp.get("id"), "confirm": True}))
        applied = (j.get("improvement") or {}).get("applied") or {}
        after_app = (store.get_skill("reply") or {}).get("prompt") or ""
        check("应用草稿：状态=applied 且写盘成功", j.get("ok") is True
              and (j.get("improvement") or {}).get("status") == "applied")
        check("应用前自动留档快照 snapshotVid", bool(applied.get("snapshotVid")))
        check("应用后正文=草稿（内容确实变化）", after_app != prompt_before and after_app == (draft or {}).get("text"))

        # ---- 8) 版本回滚恢复旧正文 ----
        print("== 验收 8：回滚恢复旧正文 ==")
        j = json_of(c.post(f"/api/improvements/{imp.get('id')}/rollback", json={}))
        after_roll = (store.get_skill("reply") or {}).get("prompt") or ""
        check("改进回滚后提示词恢复应用前内容", (j.get("improvement") or {}).get("status") == "resolved"
              and after_roll == prompt_before)
        # Skill 直连回滚也验证一遍
        j2 = json_of(c.post("/api/skills/reply/rollback",
                            json={"vid": applied.get("snapshotVid"), "actor": "ops"}))
        check("Skill 直连版本回滚路由可用且正文仍=应用前", j2.get("ok") is True
              and ((store.get_skill("reply") or {}).get("prompt") or "") == prompt_before)

        # ---- 9) 异常/越界入参 → 结构化错误而非 500 ----
        print("== 验收 9：运营接口失败不 500 / 页面仍可加载 ==")
        bad1 = json_of(c.post("/api/ratings", json={"runId": rid, "score": 99}))
        check("越界评分(99)返回结构化 {ok:false}", bad1.get("ok") is False and "1-5" in str(bad1.get("error")))
        bad2 = json_of(c.post("/api/annotations", json={"status": "bogus", "dimensions": {"overall": 9}}))
        check("非法标注状态/维度返回结构化 {ok:false}", bad2.get("ok") is False)
        bad3 = json_of(c.post("/api/improvements", json={"fromRating": "rt_nope"}))
        check("引用不存在评分返回结构化 {ok:false}", bad3.get("ok") is False)
        bad4 = json_of(c.post("/api/improvements/generate-prompt", json={"improvementId": "im_nope"}))
        check("不存在建议生成草稿返回结构化 {ok:false}", bad4.get("ok") is False)
        bad5 = json_of(c.post("/api/skills/nope/rollback", json={"vid": "v1"}))
        check("不存在 Skill 回滚返回结构化 {ok:false}", bad5.get("ok") is False)
        page = c.get("/ops")
        check("GET /ops 页面返回 200（静态页不因接口故障而崩）", page.status_code == 200
              and "运营中心" in page.get_data(as_text=True))

        # 附加：整体闭环链路真实持久化到 ops_*.json
        import json as _j
        print("== 落盘检查 ==")
        rr = _j.loads((DATA / "ops_ratings.json").read_text(encoding="utf-8")) if (DATA / "ops_ratings.json").exists() else []
        ai = _j.loads((DATA / "ops_annotations.json").read_text(encoding="utf-8")) if (DATA / "ops_annotations.json").exists() else []
        ii = _j.loads((DATA / "ops_improvements.json").read_text(encoding="utf-8")) if (DATA / "ops_improvements.json").exists() else []
        check("评分/标注/改进均真实落盘", any(r.get("id") == rating.get("id") for r in rr)
              and any(a.get("id") == ann.get("id") for a in ai)
              and any(i.get("id") == imp.get("id") for i in ii))

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
