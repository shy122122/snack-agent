# -*- coding: utf-8 -*-
"""演示重置（demo reset）：把可编辑的演示状态恢复到「已知初始态」。

恢复范围（严格按工程化需求 2 枚举，不越界也不含糊）：
- 评测集      data/eval_cases.json     → 仅保留 9 个种子用例（eval.SEED_CASES）
- Skills      data/skills.json         → 全部默认 Skill（store.DEFAULT_SKILLS）
- Skill 版本  data/skill_versions.json → {}（无任何历史快照）
- 评测批次    data/eval_batches.json   → []（无批次记录）
- 运营标注    data/ops_annotations.json  → []
- 改进建议    data/ops_improvements.json → []

不触碰（账目/业务类基础数据 + 安全配置，绝不误删，详见 UNTOUCHED）：
products / activities / coupons / service.json、runs.json / run_logs.json、
ops_ratings / ops_ab_tests、planner_config / tool_state / llm_state / config.json。

写入一律经 store.write_json（.tmp + 原子 replace + 锁），保证不产生半截文件；
重复执行结果一致（幂等）：第二次落盘文件与第一次完全一致。

调用方安全约定（本模块不自行判断模式，由上层决定）：
- HTTP 端点：仅演示模式（demo-fixture）放行，且存在 queued/running 批次时拒绝（409）；
- CLI：默认仅演示模式放行，--force 可覆盖（操作者自担，用于重启前清理陈旧 running 批次）。
"""
from __future__ import annotations

import copy

from . import eval as eval_mod, ops as ops_mod, store

RESET_FILES = [
    ("eval_cases.json", "评测集"),
    ("skills.json", "Skills"),
    ("skill_versions.json", "Skill 版本快照"),
    ("eval_batches.json", "评测批次"),
    ("ops_annotations.json", "运营标注"),
    ("ops_improvements.json", "改进建议"),
]

UNTOUCHED = [
    ("products.json", "商品（业务基础数据）"),
    ("activities.json", "满减活动（业务基础数据）"),
    ("coupons.json", "优惠券（业务基础数据）"),
    ("service.json", "订单/物流/售后政策"),
    ("runs.json", "运行记录（历史只读）"),
    ("run_logs.json", "运行日志"),
    ("ops_ratings.json", "服务评分"),
    ("ops_ab_tests.json", "A/B 实验"),
    ("planner_config.json", "规划器配置"),
    ("tool_state.json", "工具启用态"),
    ("llm_state.json", "Provider 运行时状态"),
    ("config.json", "旧版 LLM 配置"),
]


def _entry_count(path):
    data = store.read_json(store.DATA_DIR / path, None)
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def reset_demo(*, actor: str = ""):
    """执行演示重置并返回 {ok, actor, restored, untouched} 摘要。

    幂等：任何状态执行一次后达到已知初始态，再执行不改变任何文件内容。
    """
    summary = {"ok": True, "actor": actor, "method": "demo-reset",
               "restored": [], "untouched": [name for name, _ in UNTOUCHED]}

    # 1) 评测集 → 仅种子
    before = _entry_count(eval_mod.CASES_FILE)
    eval_mod._save(copy.deepcopy(eval_mod.SEED_CASES))
    summary["restored"].append({"file": eval_mod.CASES_FILE, "label": "评测集",
                                "from": before, "to": len(eval_mod.SEED_CASES),
                                "note": "仅保留 9 个种子用例"})

    # 2) Skills → 默认六技能（深拷贝，避免与 DEFAULT_SKILLS 共享引用）
    before = _entry_count("skills.json")
    store.save_skills(copy.deepcopy(store.DEFAULT_SKILLS))
    summary["restored"].append({"file": "skills.json", "label": "Skills",
                                "from": before, "to": len(store.DEFAULT_SKILLS),
                                "note": "恢复默认 6 个 Skill"})

    # 3) Skill 版本 → {}
    before = _entry_count("skill_versions.json")
    store.write_json(store.DATA_DIR / "skill_versions.json", {})
    summary["restored"].append({"file": "skill_versions.json", "label": "Skill 版本快照",
                                "from": before, "to": 0, "note": "清空历史快照"})

    # 4) 评测批次 → []
    before = _entry_count("eval_batches.json")
    store.write_json(store.DATA_DIR / "eval_batches.json", [])
    summary["restored"].append({"file": "eval_batches.json", "label": "评测批次",
                                "from": before, "to": 0, "note": "清空批次记录"})

    # 5/6) 运营标注 / 改进建议 → []
    for kind, label, fname in (("annotation", "运营标注", "ops_annotations.json"),
                               ("improvement", "改进建议", "ops_improvements.json")):
        before = _entry_count(fname)
        ops_mod._save(kind, [])
        summary["restored"].append({"file": fname, "label": label,
                                    "from": before, "to": 0, "note": "清空"})

    return summary
