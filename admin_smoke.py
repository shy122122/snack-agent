# -*- coding: utf-8 -*-
"""管理后台离线验收脚本（test_client，不联网）——覆盖本段验收。

用法：  python admin_smoke.py
要求：  运行环境不要设 SNACK_LLM_PROVIDER（脚本会通过后台切换 Provider 验证影响，
        env 固定时该部分自动转为「env 权威、切换不生效」断言）。
特点：  只读文件先备份、结束时原样还原，不改动 data/ 下任何持久状态；
        密钥只验证「不回显原文」，测试用的假 Key 绝不写入真实配置。
任意断言失败 → 打印失败明细并以退出码 1 结束（不伪装通过）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，强制 UTF-8 便于阅读
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, ".")
sys.path.insert(0, "core")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOUCHED = ["llm_state.json", "skill_versions.json", "skills.json",
           "tool_state.json", "planner_config.json", "runs.json", "run_logs.json"]
RESULTS: list = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def backup() -> dict:
    return {n: (DATA / n).read_bytes() if (DATA / n).exists() else None for n in TOUCHED}


def restore(saved: dict) -> None:
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
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py", "admin_smoke.py"] +
                       [f"core/{m}.py" for m in
                        ("engine", "store", "tools", "llm", "providers", "validator", "runner")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
    check("py_compile 全模块通过", r.returncode == 0)

    from app import app as flask_app
    from core import providers, runner, store
    c = flask_app.test_client()

    forced = (os.environ.get("SNACK_LLM_PROVIDER") or "").strip().lower()
    print(f"\n== 环境：SNACK_LLM_PROVIDER={forced or '(未设)'} ==")

    print("\n== 验收1：四个管理页可访问（200 + HTML）==")
    for path, word in (("/skills", "技能"), ("/tools", "Tool"), ("/planner", "规划"), ("/models", "模型")):
        resp = c.get(path)
        body = resp.get_data(as_text=True)
        check(f"GET {path} 200 且含品牌文案",
              resp.status_code == 200 and "text/html" in (resp.headers.get("Content-Type") or "") and word in body)

    print("\n== 验收2：GET /api/llm-config —— 目录齐全 + 密钥只显示布尔 ==")
    g = json_of(c.get("/api/llm-config"))
    ids = [x["id"] for x in g.get("catalog", [])]
    check("catalog 含 3 类 Provider", len(ids) == 3 and
          {"openai-compatible", "coze", "demo-fixture"} <= set(ids), ",".join(ids))
    eff = g.get("effective") or {}
    check("effective 含 name/ready/env_forcing 字段", "name" in eff and "ready" in eff and "env_forcing" in eff)
    check("catalog 各项含 env 状态且为布尔", all(
        "env" in x and all(isinstance(e.get("env_set"), bool) and isinstance(e.get("stored_set"), bool)
                           for e in x["env"]) for x in g.get("catalog", [])))
    check("secret 字段在回显中无原文字段", all(
        "api_key" not in (x.get("config") or {}) for x in g.get("catalog", [])))

    print("\n== 验收3：PUT 保存 —— 假密钥入库但绝不回显 ==")
    FAKE = "sk-SMOKESECRET-do-not-leak-9f3c7a"
    put_resp = c.put("/api/llm-config", json={"providers": {
        "openai-compatible": {"api_key": FAKE, "base_url": "https://api.deepseek.com",
                              "model": "deepseek-chat", "timeout": 90}}})
    put = json_of(put_resp)
    check("PUT 返回 ok", put.get("ok") is True)
    check("PUT 响应不回显假密钥", FAKE not in put_resp.get_data(as_text=True))
    stored = store.load_llm_state().get("providers", {}).get("openai-compatible", {})
    check("服务端已持久化 Key（data/llm_state.json）", stored.get("api_key") == FAKE)
    g2 = json_of(c.get("/api/llm-config"))
    check("GET 响应不回显假密钥", FAKE not in g2 and FAKE not in json.dumps(g2, ensure_ascii=False))
    g2_oc = next(x for x in g2["catalog"] if x["id"] == "openai-compatible")
    check("config 仅回显非密钥字段", set((g2_oc.get("config") or {}).keys()) <= {"base_url", "model", "timeout", "bot_id"})
    check("stored_set 反映已存 Key", next(e for e in g2_oc["env"] if e["key"] == "OPENAI_API_KEY")["stored_set"] is True)

    print("\n== 验收4：空 Key 不清空既有密钥 ==")
    json_of(c.put("/api/llm-config", json={"providers": {"openai-compatible": {"api_key": ""}}}))
    stored2 = store.load_llm_state().get("providers", {}).get("openai-compatible", {})
    check("api_key 留空后仍保留旧值", stored2.get("api_key") == FAKE)

    print("\n== 验收5：Tool 测试真实调用服务端 Tool（compute_price 离线算价）==")
    tlist = json_of(c.get("/api/tools"))
    cp = next((x for x in tlist.get("tools", []) if x["id"] == "compute_price"), None)
    check("GET /api/tools 含 compute_price 且带示例", bool(cp and cp.get("sample_input", {}).get("items")))
    test_resp = c.post("/api/tools/compute_price/test", json={"inputs": cp["sample_input"]})
    tj = json_of(test_resp)
    out = tj.get("output") or {}
    check("Tool 测试返回真实算价结果(final_total)",
          test_resp.status_code == 200 and tj.get("ok") and "final_total" in out,
          str(list(out.keys())))
    check("Tool 测试带 latency_ms", "latency_ms" in tj)

    print("\n== 验收6：Provider 切换影响后续 RunRecord ==")
    if forced and forced not in ("fixture", "demo-fixture"):
        check("env 固定(openai)下切换不生效——跳过联网运行", True, f"当前 env 固定 {forced}")
    else:
        # 断言「env 权威」：写 llm_state 切到 fixture 后，若 env 固定则 effective 不变
        sw = json_of(c.post("/api/llm-config/switch", json={"provider": "demo-fixture"}))
        eff_after = (sw.get("effective") or {}).get("name")
        fstate = store.load_llm_state().get("provider")
        if forced:
            check("env 权威：切换被忽略，effective 仍为 env 指定", eff_after == forced, f"effective={eff_after}")
        else:
            check("switch 成功：effective=demo-fixture", eff_after == "demo-fixture", eff_after)
            check("switch 已写盘 data/llm_state.json", fstate == "demo-fixture", fstate)
            rec = runner.run_run("100元以内买坚果礼盒送人，帮我算一下到手价", source="admin_smoke")
            check("切换后 RunRecord.provider=demo-fixture（fixture 影响后续运行）",
                  rec.get("provider") == "demo-fixture", rec.get("provider"))
            check("切换后 RunRecord 无报错且走主链路", rec.get("status") == "ok", rec.get("status"))

    print("\n== 验收7：测试连接 —— 成功(fixture) / 失败结构化中文错(coze) ==")
    ok_t = json_of(c.post("/api/llm-config/test", json={"provider": "demo-fixture"}))
    check("fixture 连通成功", ok_t.get("ok") is True and ok_t.get("provider") == "demo-fixture")
    bad_t = json_of(c.post("/api/llm-config/test", json={"provider": "coze"}))
    check("coze 未配置 → 结构化中文错误(非 HTTP 500)",
          bad_t.get("ok") is False and bool(bad_t.get("error")) and ("Key" in bad_t.get("error") or "Coze" in bad_t.get("error")),
          (bad_t.get("error") or "")[:60])

    print("\n== 验收8：Planner 预览随启用状态改变（需 fixture 确定性规划）==")
    if not forced or forced in ("fixture", "demo-fixture"):
        need_fixture = eff_after != "demo-fixture"
        if need_fixture:
            json_of(c.post("/api/llm-config/switch", json={"provider": "demo-fixture"}))
        Q = "100元以内买坚果礼盒送人，帮我算一下到手价"
        pre1 = json_of(c.post("/api/planner/preview", json={"question": Q}))
        v1 = pre1.get("validation") or {}
        check("预览①：启用态下校验通过",
              pre1.get("ok") is True and v1.get("ok") is True and not v1.get("errors"), pre1.get("error", "")[:80])
        # 关掉 compute_price 再预览 → 计划无法满足价格场景 → 校验失败/降级（不伪装成功）
        json_of(c.put("/api/tools/compute_price", json={"enabled": False}))
        pre2 = json_of(c.post("/api/planner/preview", json={"question": Q}))
        v2 = pre2.get("validation") or {}
        err2 = len(v2.get("errors") or [])
        check("预览②：停用价格 Tool 后校验失败或降级(不伪装 ok)",
              (not pre2.get("ok")) or (v2.get("ok") is False) or err2 >= 1,
              f"ok={pre2.get('ok')} val.ok={v2.get('ok')} errors={err2}")
        json_of(c.put("/api/tools/compute_price", json={"enabled": True}))
    else:
        check("env 固定(openai)下跳过预览（避免联网）", True, forced)

    print("\n== 验收9：Skill 编辑持久化 + 版本快照 ==")
    skills = json_of(c.get("/api/skills")).get("skills", [])
    sid = "recommend"
    cur = next((s for s in skills if s["id"] == sid), None)
    check("GET /api/skills 含字段(id/prompt/enabled/dep_tools)", bool(cur and cur.get("prompt") and "dep_tools" in cur))
    orig_prompt = cur["prompt"]
    marker = orig_prompt + "\n\n# 冒烟注释（admin_smoke 临时追加，将被还原）"
    up = json_of(c.put(f"/api/skills/{sid}", json={"prompt": marker, "note": "admin_smoke 编辑", "actor": "smoke"}))
    check("PUT skill 返回新值", (up.get("skill") or {}).get("prompt") == marker)
    check("内容变化 → 生成了版本(vid)", bool((up.get("version") or {}).get("vid")), str(up.get("version")))
    again = json_of(c.get("/api/skills")).get("skills", [])
    again_p = next((s for s in again if s["id"] == sid), {}).get("prompt")
    check("刷新(重读文件)后编辑仍生效", again_p == marker)
    # 磁盘里的 prompt 会以 JSON 转义(换行→\n)存储，故用不含换行的单行片段做字节级校验
    tail = "# 冒烟注释（admin_smoke 临时追加，将被还原）"
    check("磁盘 data/skills.json 含新 prompt", tail.encode("utf-8") in (DATA / "skills.json").read_bytes())
    vlist = json_of(c.get(f"/api/skills/{sid}/versions")).get("versions", [])
    check("版本列表 ≥1 且最新版本内容=旧 prompt(保存前快照)",
          len(vlist) >= 1 and any(v.get("snapshot", {}).get("prompt") == orig_prompt for v in vlist),
          f"{len(vlist)} 个版本")

    print("\n" + "=" * 56)
    fails = [x for x in RESULTS if not x[1]]
    print(f"共 {len(RESULTS)} 项断言，失败 {len(fails)} 项。")
    for name, _c, detail in fails:
        print("   FAIL: " + name + ("  — " + detail if detail else ""))
    print("结果：", "全部通过 ✔" if not fails else "存在失败 ✘")
    return 1 if fails else 0


if __name__ == "__main__":
    saved = backup()
    code = 1
    try:
        code = main()
    except Exception:
        traceback.print_exc()
    finally:
        restore(saved)
        print("\n[还原] 已恢复 data/ 下受影响的持久文件。")
    sys.exit(code)
