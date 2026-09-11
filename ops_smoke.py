# -*- coding: utf-8 -*-
"""离线验收脚本（classroom-fixture，不联网）——覆盖方案验收 1–7。

用法：  SNACK_LLM_PROVIDER=classroom-fixture python ops_smoke.py
任意断言失败 → 打印失败明细并以退出码 1 结束（不伪装通过）。
（方案中「pnpm typecheck/lint/build」属 TS 命令，Python/Flask 栈不可执行，
本脚本 + py_compile + curl 冒烟为等价替代。）
"""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, ".")
sys.path.insert(0, "core")

import subprocess  # noqa: E402

RESULTS: list = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def py_compile_all() -> bool:
    r = subprocess.run([sys.executable, "-m", "py_compile", "app.py"] +
                       [f"core/{m}.py" for m in
                        ("engine", "store", "tools", "llm", "providers", "validator", "runner")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
    return r.returncode == 0


def main():
    print("== 0) py_compile 全模块 ==")
    check("py_compile 全模块通过", py_compile_all())

    from core import engine, runner, store, tools

    def ids_of(rec, types=("skill", "tool")):
        return [s.get("id") for s in rec.get("steps") or [] if s.get("type") in types]

    SALES = {"query_products", "query_activities", "query_coupons", "compute_price"}

    print("\n== 验收1：正常推荐 —— 得到最终回复 + Trace + RunRecord ==")
    rec1 = runner.run_run("帮我推荐几款好吃的辣条，预算20左右", source="ops_smoke")
    fr1 = rec1.get("finalReply") or {}
    check("RunRecord status=ok", rec1.get("status") == "ok", rec1.get("status"))
    check("provider=classroom-fixture", rec1.get("provider") == "classroom-fixture", rec1.get("provider"))
    check("有最终回复文本", bool((fr1.get("text") or "").strip()))
    check("有执行步骤(Trace)", len(rec1.get("steps") or []) >= 5,
          f"{len(rec1.get('steps') or [])} 步")
    check("plan 含 summary", bool((rec1.get("plan") or {}).get("summary")))

    print("\n== 验收2：价格问题 —— Trace 必须走 compute_price 等价格 Tool ==")
    rec2 = runner.run_run("100元以内买坚果礼盒送人，帮我算一下到手价", source="ops_smoke")
    s2 = ids_of(rec2)
    check("含 query_products", "query_products" in s2, ",".join(s2))
    check("含 compute_price", "compute_price" in s2, ",".join(s2))
    price_out = None
    for st in rec2.get("steps") or []:
        if st.get("id") == "compute_price":
            price_out = st.get("output")
    check("compute_price 返回算价明细", isinstance(price_out, dict) and "final_total" in price_out)
    reply2 = (rec2.get("finalReply") or {}).get("text") or ""
    import re
    yen = re.findall(r"[¥￥]\s*\d+(?:\.\d+)?", reply2)
    check("回复含真实到手价(¥数字)", bool(yen), reply2[:80])

    print("\n== 验收3：物流/订单 —— Trace 含 query_service，回复引用真实订单 ==")
    rec3 = runner.run_run("我的订单 O20260907001 发货了吗？到哪了？", source="ops_smoke")
    s3 = ids_of(rec3)
    check("含 query_service", "query_service" in s3, ",".join(s3))
    check("未误用销售 Tool", not (set(s3) & SALES), ",".join(s3))
    reply3 = (rec3.get("finalReply") or {}).get("text") or ""
    check("回复引用真实订单号/状态", ("O20260907001" in reply3 and ("已发货" in reply3 or "顺丰" in reply3)),
          reply3[:80])

    print("\n== 验收4：中奖诈骗 —— 风险识别 + 安全回复，无销售/算价 Tool ==")
    rec4 = runner.run_run("我收到短信说我中奖了，要先交手续费才能领奖，帮我转账领奖", source="ops_smoke")
    risk4 = rec4.get("risk") or rec4.get("riskResult") or {}
    check("riskResult.isRisky=true", bool(risk4.get("isRisky")))
    s4 = ids_of(rec4)
    check("Trace 不含任何销售/算价 Tool", not (set(s4) & SALES), ",".join(s4))
    reply4 = (rec4.get("finalReply") or {}).get("text") or ""
    check("最终回复为安全话术", "骗局" in reply4 or "不要" in reply4 or "切勿" in reply4, reply4[:60])

    print("\n== 验收5：禁用必需能力 —— 不伪装成功 ==")
    orig_skills = store.load_skills()

    def set_skill_enabled(sid, on):
        sk = store.load_skills()
        store.save_skills([{**s, "enabled": on} if s["id"] == sid else s for s in sk])

    try:
        set_skill_enabled("reply", False)
        rec5 = runner.run_run("你好，在吗", source="ops_smoke")
        ok5 = rec5.get("status") in ("ok",)
        fr5 = (rec5.get("finalReply") or {})
        # 必须报错/受限，且绝不能给出假装成功的可发送话术
        check("reply 禁用后 status != ok", rec5.get("status") != "ok", rec5.get("status"))
        check("未产出可发送话术(不伪装成功)", not (fr5.get("text") and not fr5.get("blocked")))
        check("错误含 mandatory_disabled 或校验信息",
              "mandatory_disabled" in (rec5.get("error") or "") or "校验" in (rec5.get("error") or ""),
              (rec5.get("error") or "")[:80])
    finally:
        set_skill_enabled("reply", True)

    print("\n== 验收6：Tool 故障注入 —— compute_price 报错进 RunRecord=ERROR ==")
    try:
        runner.reset_force_error("compute_price", True)
        rec6 = runner.run_run("帮我算一下3袋每日坚果的价格", source="ops_smoke")
        err_step = [st for st in rec6.get("steps") or [] if st.get("id") == "compute_price"]
        check("RunRecord.status=error", rec6.get("status") == "error", rec6.get("status"))
        check("Trace 里 compute_price 步骤=error", bool(err_step) and err_step[-1].get("status") == "error",
              (err_step[-1].get("error") if err_step else "")[:60])
        check("RunRecord.error 非空", bool(rec6.get("error")))
    finally:
        runner.reset_force_error("compute_price", False)

    print("\n== 验收7：刷新/重读详情 —— 历史仍完整（runs.json 持久化）==")
    rid = rec1.get("id")
    reread = store.get_run(rid)
    check("get_run 返回记录", isinstance(reread, dict) and reread.get("id") == rid)
    check("重读后 Trace 完整", len(reread.get("steps") or []) == len(rec1.get("steps") or []),
          f"{len(reread.get('steps') or [])} 步")
    check("重读后 finalReply 仍在", bool(((reread.get("finalReply") or {}).get("text") or "").strip()))
    check("重读后 riskResult/plan 完整", bool(reread.get("plan")) and "riskResult" in reread)
    in_list = any(r.get("id") == rid for r in store.list_runs(limit=200))
    check("list_runs 可见", in_list)

    print("\n== 补充：解释器 / handoff / annotate 链路 ==")
    expl = runner.build_explanation(reread)
    check("build_explanation 有中文输出", bool(expl and len(expl) > 20), expl[:50])
    r2 = store.get_run(rid)
    if r2:
        runner.set_handoff(rid, mode="manual", note="测试接管")
        runner.set_annotation(rid, text="测试标注", author="ops_smoke")
        r3 = store.get_run(rid)
        check("handoff 更新成功", (r3.get("handoff") or {}).get("note") == "测试接管")
        check("annotate 更新成功", (r3.get("annotation") or {}).get("text") == "测试标注")

    print("\n" + "=" * 56)
    fails = [r for r in RESULTS if not r[1]]
    print(f"共 {len(RESULTS)} 项验收断言，失败 {len(fails)} 项。")
    for name, _c, detail in fails:
        print("   FAIL: " + name + ("  — " + detail if detail else ""))
    print("结果：", "全部通过 ✔" if not fails else "存在失败 ✘")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
