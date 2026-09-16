# -*- coding: utf-8 -*-
"""演示重置（demo reset）：把可编辑的演示状态恢复到「已知初始态」。

恢复范围：
- 商品目录    data/products.json       → data_seed/products.json
- 满减活动    data/activities.json     → data_seed/activities.json
- 优惠券      data/coupons.json        → data_seed/coupons.json
- 订单售后    data/service.json        → data_seed/service.json
- 评测集      data/eval_cases.json     → data_seed/eval_cases.json
- Skills      data/skills.json         → data_seed/skills.json
- Skill 版本  data/skill_versions.json → {}（无任何历史快照）
- 评测批次    data/eval_batches.json   → []（无批次记录）
- 运营标注    data/ops_annotations.json  → []
- 改进建议    data/ops_improvements.json → []

不触碰（历史记录 + 安全配置，绝不误删，详见 UNTOUCHED）：
runs.json / run_logs.json、ops_ratings / ops_ab_tests、
planner_config / tool_state / llm_state / config.json。

写入一律经 store.write_json（.tmp + 原子 replace + 锁），保证不产生半截文件；
重复执行结果一致（幂等）：第二次落盘文件与第一次完全一致。

调用方安全约定（本模块不自行判断模式，由上层决定）：
- HTTP 端点：仅演示模式（demo-fixture）放行，且存在 queued/running 批次时拒绝（409）；
- CLI：默认仅演示模式放行，--force 可覆盖（操作者自担，用于重启前清理陈旧 running 批次）。
"""
from __future__ import annotations

import copy
import shutil

from . import eval as eval_mod, ops as ops_mod, store

BUSINESS_RESET_FILES = [
    ("products.json", "商品目录"),
    ("activities.json", "满减活动"),
    ("coupons.json", "优惠券"),
    ("service.json", "订单/物流/售后政策"),
]

RESET_FILES = [
    ("eval_cases.json", "评测集"),
    ("skills.json", "Skills"),
    ("skill_versions.json", "Skill 版本快照"),
    ("eval_batches.json", "评测批次"),
    ("ops_annotations.json", "运营标注"),
    ("ops_improvements.json", "改进建议"),
]

UNTOUCHED = [
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


def _restore_from_seed(name: str, label: str):
    before = _entry_count(name)
    src = store.seed_file(name)
    dst = store.DATA_DIR / name
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    after = _entry_count(name)
    return {"file": name, "label": label, "from": before, "to": after,
            "note": "恢复标准演示数据"}


def reset_demo(*, actor: str = ""):
    """执行演示重置并返回 {ok, actor, restored, untouched} 摘要。

    幂等：任何状态执行一次后达到已知初始态，再执行不改变任何文件内容。
    """
    summary = {"ok": True, "actor": actor, "method": "demo-reset",
               "restored": [], "untouched": [name for name, _ in UNTOUCHED]}

    # 1) 业务基础数据 → 标准演示数据
    for fname, label in BUSINESS_RESET_FILES:
        summary["restored"].append(_restore_from_seed(fname, label))

    # 2) 评测集 → 标准评测用例（优先 data_seed，旧仓库回退内置种子）
    seed_cases = store.read_json(store.seed_file(eval_mod.CASES_FILE), None)
    before = _entry_count(eval_mod.CASES_FILE)
    if isinstance(seed_cases, list) and seed_cases:
        eval_mod._save(copy.deepcopy(seed_cases))
        to_count = len(seed_cases)
        note = "恢复标准评测用例"
    else:
        eval_mod._save(copy.deepcopy(eval_mod.SEED_CASES))
        to_count = len(eval_mod.SEED_CASES)
        note = "恢复内置种子用例"
    summary["restored"].append({"file": eval_mod.CASES_FILE, "label": "评测集",
                                "from": before, "to": to_count, "note": note})

    # 3) Skills → 标准 Skills（优先 data_seed，旧仓库回退默认六技能）
    seed_skills = store.read_json(store.seed_file("skills.json"), None)
    before = _entry_count("skills.json")
    if isinstance(seed_skills, list) and seed_skills:
        store.save_skills(copy.deepcopy(seed_skills))
        to_count = len(seed_skills)
        note = "恢复标准 Skills"
    else:
        store.save_skills(copy.deepcopy(store.DEFAULT_SKILLS))
        to_count = len(store.DEFAULT_SKILLS)
        note = "恢复默认 6 个 Skill"
    summary["restored"].append({"file": "skills.json", "label": "Skills",
                                "from": before, "to": to_count, "note": note})

    # 4) Skill 版本 → {}
    before = _entry_count("skill_versions.json")
    store.write_json(store.DATA_DIR / "skill_versions.json", {})
    summary["restored"].append({"file": "skill_versions.json", "label": "Skill 版本快照",
                                "from": before, "to": 0, "note": "清空历史快照"})

    # 5) 评测批次 → []
    before = _entry_count("eval_batches.json")
    store.write_json(store.DATA_DIR / "eval_batches.json", [])
    summary["restored"].append({"file": "eval_batches.json", "label": "评测批次",
                                "from": before, "to": 0, "note": "清空批次记录"})

    # 6/7) 运营标注 / 改进建议 → []
    for kind, label, fname in (("annotation", "运营标注", "ops_annotations.json"),
                               ("improvement", "改进建议", "ops_improvements.json")):
        before = _entry_count(fname)
        ops_mod._save(kind, [])
        summary["restored"].append({"file": fname, "label": label,
                                    "from": before, "to": 0, "note": "清空"})

    return summary
