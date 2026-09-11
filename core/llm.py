# -*- coding: utf-8 -*-
"""大模型统一出口层：把调用委托给当前 Provider（openai-compatible / coze / classroom-fixture）。

本文件保持对外 API 不变（call_llm / extract_json / estimate_cost / LLMError），
真正的网络 / 剧本实现位于 core/providers.py。返回统一增加 provider 键供 RunRecord 记录。
"""
from __future__ import annotations

import json
import re

from .providers import LLMError, resolve_provider  # noqa: F401  （LLMError 从 providers 再导出）


def extract_json(text: str):
    """从模型返回文本中稳健地抽取 JSON 对象（兼容 markdown 代码块 / 前后杂音）。"""
    if not isinstance(text, str):
        raise LLMError("模型返回空内容")
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t).rstrip("`").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            pass
    # 最后一个数组对象
    s2, e2 = t.find("["), t.rfind("]")
    if s2 != -1 and e2 > s2:
        try:
            return json.loads(t[s2 : e2 + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"模型未返回合法 JSON。原始内容预览：{text[:200]}")


def _price_of(model: str):
    # DeepSeek-chat 参考价：输入 2 元 / 百万 token，输出 8 元 / 百万 token（估算）
    return 2.0, 8.0


def estimate_cost(prompt_tokens, completion_tokens, model) -> float:
    pi, po = _price_of(model)
    return round((prompt_tokens * pi + completion_tokens * po) / 1_000_000, 5)


def call_llm(messages, *, json_mode=False, model=None, temperature=None,
             max_tokens=None, timeout=None, provider_hint=None):
    """调用当前 Provider。每次都重新 resolve（配置/环境变量即时生效）。

    返回 {"content":str,"usage":{...},"model":str,"provider":str,
          "latency_ms":int,"cost_yuan":float}
    """
    return resolve_provider().chat(
        messages, json_mode=json_mode, model=model,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        provider_hint=provider_hint,
    )
