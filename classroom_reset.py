# -*- coding: utf-8 -*-
"""课堂重置 CLI —— pnpm classroom:reset 的 Python 等价物。

用法：
    python classroom_reset.py              # 演示模式（SNACK_LLM_PROVIDER=classroom-fixture）下直接重置
    python classroom_reset.py --force      # 真实 Provider / 存在运行中批次时强制重置（自担风险）

恢复内容（到「已知初始态」，幂等，可重复执行）：
    评测集(仅 9 种子) / Skills(默认6) / Skill 版本(空) / 评测批次(空) / 运营标注(空) / 改进建议(空)

绝不触碰：商品/满减/优惠券/售后政策 等业务基础数据，及运行记录、评分、A-B、规划器/工具/模型配置。

安全：默认仅在演示模式放行；真实模型（openai-compatible/coze）环境需 --force，防止误清。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    force = "--force" in sys.argv[1:]
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print(__doc__)
        return 0

    from core import eval_batch as eb, providers, reset

    mode = providers.effective_provider_name()
    if mode != "classroom-fixture" and not force:
        print(f"[拒绝] 当前 Provider={mode}（非演示模式）。课堂重置只允许在演示模式执行，"
              f"请用 SNACK_LLM_PROVIDER=classroom-fixture 启动；若确需在真实模式清空请加 --force。", file=sys.stderr)
        return 2

    act = eb.active_batch_id()
    if act and not force:
        print(f"[拒绝] 存在未终态评测批次 {act}（queued/running）。请先取消或等待完成；"
              f"若是重启前遗留的陈旧批次，可加 --force（会同时清空该记录）。", file=sys.stderr)
        return 2

    summary = reset.reset_classroom(actor="cli")
    print("课堂重置完成（幂等 · 原子写入）")
    for item in summary["restored"]:
        print(f"  · {item['label']:6s} {item['file']:22s} {item['from']:>3d} → {item['to']:>3d}   {item['note']}")
    print(f"  未触碰 {len(summary['untouched'])} 项业务/配置数据（含商品/订单/密钥）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
