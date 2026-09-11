# -*- coding: utf-8 -*-
"""最终工程化收尾 · 新增能力离线验收脚本（test_client，不联网，classroom-fixture）。

覆盖本轮新增/补齐项：
1) /catalog 商品目录页 + /api/data/{products,activities,coupons} 可打开、数据非空
2) 课堂重置 HTTP：只清评测集/Skills/Skill版本/评测批次/运营标注/改进建议 → 已知初始态；
   幂等（连续两次字节一致）；业务基础数据(商品/订单/服务等)与配置不动
3) 课堂重置守卫：有 queued/running 批次 → 409(带 activeBatchId)；非演示 Provider → 403
4) classroom_reset.py CLI：真实模式默认拒绝(exit 2)；演示模式成功(exit 0)，可重复
5) Skill 版本快照 API：PUT 改 prompt → /versions 自动新增旧版快照，快照内容=改动前旧值
6) Provider 缺配置 → 结构化中文错误（非 500、不静默伪装成演示成功）

特点：先备份 data 下触及文件，结束时原样还原，不改动任何持久状态。
用法： python engineering_smoke.py
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
# 可能被本轮改动触碰的文件（含 reset 六目标 + 快照测试 + run）→ 全部备份还原
TOUCHED = ["eval_cases.json", "skills.json", "skill_versions.json", "eval_batches.json",
           "ops_annotations.json", "ops_improvements.json", "ops_ratings.json",
           "ops_ab_tests.json", "products.json", "activities.json", "coupons.json",
           "service.json", "runs.json", "run_logs.json", "tool_state.json",
           "planner_config.json", "llm_state.json"]
BUSINESS = ["products.json", "activities.json", "coupons.json", "service.json",
            "ops_ratings.json", "ops_ab_tests.json", "tool_state.json",
            "planner_config.json", "llm_state.json", "runs.json", "run_logs.json"]
RESULTS: list = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def backup():
    out = {}
    for n in TOUCHED:
        p = DATA / n
        out[n] = p.read_bytes() if p.exists() else None
    return out


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


def text_of(resp):
    try:
        return resp.get_data(as_text=True)
    except Exception:
        return ""


def main():
    print("== 0) py_compile ==")
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py",
                        "classroom_reset.py", "engineering_smoke.py"] +
                       [f"core/{m}.py" for m in ("providers", "reset")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stderr[-3000:])
    check("py_compile 全模块通过", r.returncode == 0)

    print("== 准备：备份 ==")
    saved = backup()
    try:
        from app import app  # noqa: F401 触发 ensure_seed
        from core import eval as eval_mod, providers
        c = app.test_client()

        # ---- 0) 演示 Provider 基态 ----
        st = json_of(c.get("/api/provider/status"))
        check("基态：classroom-fixture 演示模式(llm_ready/demo/stable_demo)",
              st.get("provider") == "classroom-fixture" and st.get("demo") is True
              and st.get("llm_ready") is True and st.get("stable_demo") is True,
              st.get("label"))

        # ---- 1) /catalog 页面 + 数据接口 ----
        print("== 验收 1：/catalog 商品目录 ==")
        page = c.get("/catalog")
        html = text_of(page)
        check("GET /catalog 200 + 页面关键标记", page.status_code == 200
              and "商品目录" in html and "fa-sitemap" in html and "/api/data/products" in html)
        pr = json_of(c.get("/api/data/products"))
        ac = json_of(c.get("/api/data/activities"))
        cp = json_of(c.get("/api/data/coupons"))
        check("商品数据接口 ok 且有在售商品",
              pr.get("ok") is True and len(pr.get("rows") or []) > 0
              and any(p.get("status") == "在售" for p in pr.get("rows") or []))
        check("活动/券接口 ok", ac.get("ok") is True and cp.get("ok") is True)

        # ---- 2) 主链路仍真实可跑（fixture）----
        print("== 验收 2：Agent 主链路真实可跑 ==")
        runj = json_of(c.post("/api/agent/run", json={"question": "来点追剧吃的麻辣零食",
                                                      "stream": False}))
        check("主链路真实可跑(status=ok + compute_price 步骤)",
              runj.get("ok") is True and runj.get("status") == "ok"
              and any((s or {}).get("id") == "compute_price" for s in (runj.get("steps") or [])))

        # ---- 3) Skill 版本快照 API ----
        print("== 验收 3：Skill 版本快照（编辑自动归档旧值）==")
        sk = json_of(c.get("/api/skills"))
        reply = next((s for s in (sk.get("skills") or []) if s.get("id") == "reply"), None)
        old_prompt = (reply or {}).get("prompt", "")
        v0 = (reply or {}).get("versions_count") or 0
        upd = json_of(c.put("/api/skills/reply", json={
            "prompt": old_prompt + "\n（engineering_smoke 临时追加，结束即还原）",
            "note": "engineering_smoke"}))
        v_new = (upd.get("version") or {})
        vlist = json_of(c.get("/api/skills/reply/versions"))
        vers = vlist.get("versions") or []
        last = vers[-1] if vers else {}
        snap_prompt = ((last.get("snapshot") or {}).get("prompt") or "")
        check("编辑 prompt → 自动生成版本快照(v{v0+1}) 且快照内容=改动前旧值",
              bool(v_new.get("vid")) and v_new.get("vid") == f"v{v0 + 1}"
              and len(vers) == v0 + 1 and snap_prompt == old_prompt,
              f"vid={v_new.get('vid')} old_len={len(old_prompt)} snap_len={len(snap_prompt)}")

        # ---- 业务/配置基线（重置前抓取，用于断言不动）----
        before_biz = {n: (DATA / n).read_bytes() if (DATA / n).exists() else None for n in BUSINESS}

        # ---- 4) 课堂重置 · 409 守卫 ----
        print("== 验收 3：课堂重置 ==")
        fake_eb = DATA / "eval_batches.json"
        orig_eb = fake_eb.read_bytes() if fake_eb.exists() else None
        fake_eb.write_text(json.dumps([{"id": "eb_fake_active", "status": "queued",
                                        "createdAt": "2026-09-07T00:00:00"}],
                                      ensure_ascii=False), encoding="utf-8")
        try:
            r409 = json_of(c.post("/api/system/classroom-reset"))
            check("有 queued 批次 → 409 拒绝并带 activeBatchId",
                  r409.get("ok") is False and r409.get("activeBatchId") == "eb_fake_active",
                  json.dumps(r409, ensure_ascii=False))
        finally:
            if orig_eb is None:
                fake_eb.unlink(missing_ok=True)
            else:
                fake_eb.write_bytes(orig_eb)

        # ---- 5) 课堂重置 · 首次 + 幂等 ----
        r1 = json_of(c.post("/api/system/classroom-reset"))
        check("POST classroom-reset → ok + 6 项 restored",
              r1.get("ok") is True and len(r1.get("restored") or []) == 6,
              json.dumps(r1.get("restored"), ensure_ascii=False))
        # 各文件已知初始态
        cases = json.loads((DATA / "eval_cases.json").read_text(encoding="utf-8"))
        seed_ids = {s.get("id") for s in eval_mod.SEED_CASES}
        check("评测集 → 仅 9 种子", len(cases) == len(seed_ids)
              and {x.get("id") for x in cases} == seed_ids)
        skills = json.loads((DATA / "skills.json").read_text(encoding="utf-8"))
        default_ids = {"needs", "recommend", "reason", "reply", "risk", "moderation"}
        check("Skills → 默认 6 且全启用",
              len(skills) == 6 and {s.get("id") for s in skills} == default_ids
              and all(s.get("enabled") for s in skills))
        check("Skill 版本 → 空对象", json.loads((DATA / "skill_versions.json").read_text(encoding="utf-8")) == {})
        check("评测批次 → 空数组", json.loads((DATA / "eval_batches.json").read_text(encoding="utf-8")) == [])
        check("运营标注/改进建议 → 空数组",
              json.loads((DATA / "ops_annotations.json").read_text(encoding="utf-8")) == []
              and json.loads((DATA / "ops_improvements.json").read_text(encoding="utf-8")) == [])

        snap1 = {n: (DATA / n).read_bytes() if (DATA / n).exists() else None
                 for n in ["eval_cases.json", "skills.json", "skill_versions.json",
                           "eval_batches.json", "ops_annotations.json", "ops_improvements.json"]}
        r2 = json_of(c.post("/api/system/classroom-reset"))
        check("二次重置 → ok 且幂等（六文件字节与首次一致）",
              r2.get("ok") is True and snap1 == {n: (DATA / n).read_bytes() if (DATA / n).exists() else None
                                                  for n in snap1})
        check("业务基础数据/配置未被重置触碰",
              all(before_biz.get(n) == ((DATA / n).read_bytes() if (DATA / n).exists() else None)
                  for n in BUSINESS))
        pr2 = json_of(c.get("/api/data/products"))
        check("重置后商品数据仍完整", (pr2.get("rows") or []) == (pr.get("rows") or []))

        # ---- 6) 非演示 Provider → 403（monkeypatch 模拟真实模式）----
        orig_eff = providers.effective_provider_name
        try:
            providers.effective_provider_name = lambda: "openai-compatible"
            r403 = json_of(c.post("/api/system/classroom-reset"))
            check("真实 Provider 模式 → 403 结构化拒绝（防误清）",
                  r403.get("ok") is False and "演示模式" in (r403.get("error") or ""),
                  (r403.get("error") or ""))
        finally:
            providers.effective_provider_name = orig_eff

        # ---- 7) Provider 缺配置 → 结构化错误（非 500 / 非静默成功）----
        print("== 验收 4：Provider 缺配置 = 结构化报错 ==")
        orig_eff = providers.effective_provider_name
        orig_cfg = providers.config_for
        try:
            providers.effective_provider_name = lambda: "openai-compatible"
            providers.config_for = lambda pid: {"api_key": "", "base_url": "https://api.deepseek.com",
                                                "model": "deepseek-chat", "timeout": 90}
            resp = c.post("/api/agent/run", json={"question": "来点辣的", "stream": False})
            jj = json_of(resp)
            err_text = (jj.get("error") or "") + " " + (jj.get("blocked_reason") or "")
            check("真实 Provider 缺 Key → 结构化错误(非500/非静默成功)",
                  resp.status_code != 500 and jj.get("ok") is False and "openai-compatible" in "openai-compatible"
                  and ("API Key" in err_text or "演示模式" in err_text or "配置" in err_text),
                  f"code={resp.status_code} error={jj.get('error')}")
        finally:
            providers.effective_provider_name = orig_eff
            providers.config_for = orig_cfg

        # ---- 8) CLI：真实模式拒绝 / 演示模式成功 ----
        print("== 验收 5：classroom_reset.py CLI ==")
        env_no = dict(os.environ)
        env_no["SNACK_LLM_PROVIDER"] = "openai-compatible"  # 显式真实模式 → CLI 应拒绝
        rc_no = subprocess.run([sys.executable, "classroom_reset.py"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", env=env_no)
        check("真实模式(无 env) → 拒绝 exit=2 + 中文提示",
              rc_no.returncode == 2 and "拒绝" in (rc_no.stderr or ""),
              (rc_no.stderr or "").strip().splitlines()[:1][0] if rc_no.stderr else "")
        rc_yes = subprocess.run([sys.executable, "classroom_reset.py"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", env=os.environ)
        check("演示模式 → exit=0 且完成提示",
              rc_yes.returncode == 0 and "课堂重置完成" in (rc_yes.stdout or ""),
              (rc_yes.stdout or "").strip().splitlines()[1] if rc_yes.stdout else "")
        rc_yes2 = subprocess.run([sys.executable, "classroom_reset.py"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", env=os.environ)
        check("CLI 二次执行幂等 exit=0", rc_yes2.returncode == 0)

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
