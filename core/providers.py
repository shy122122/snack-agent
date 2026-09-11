# -*- coding: utf-8 -*-
"""LLM Provider 抽象：openai-compatible / coze / demo-fixture。

统一接口（engine/llm 之上的唯一出口后面的一层）：
    chat(messages, *, json_mode, model, temperature, max_tokens, timeout,
         provider_hint=None)
        -> {content, usage, model, provider, latency_ms, cost_yuan}
缺必要配置时抛 LLMError（结构化中文错误，含如何切演示模式的提示）。
选择：环境变量 SNACK_LLM_PROVIDER ∈ {openai-compatible(默认), coze, demo-fixture}。
无静默降级：真实 provider 缺 key 直接报错，绝不悄悄回落到演示模式。

说明（交付差异）：
- coze-coding-dev-sdk 是 Node SDK；本 Python 宿主无法 import，故以等价 Coze HTTP
  v3 chat 接口实现同一 chat() 契约。
- demo-fixture 为确定性演示 Provider：provider/model 恒为
  demo-fixture/fixture-demo，输出取自"剧本"，但一律经由与真实模型完全相同的
  plan → execute → 风险审计 → RunRecord 链路，绝不直接把 PASS/FAIL 写进结果。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import store
from .validator import classify_scenario, detect_risk, looks_complaint


class LLMError(Exception):
    pass


_DEMO_HINT = '（若需离线演示，请设置环境变量 SNACK_LLM_PROVIDER=demo-fixture）'


def _est_cost(prompt_tokens, completion_tokens) -> float:
    # 参考价：输入 2 元 / 百万 token，输出 8 元 / 百万 token（与 DeepSeek 近似，仅估算）
    pi, po = 2.0, 8.0
    return round((prompt_tokens * pi + completion_tokens * po) / 1_000_000, 5)


# ---- Provider 目录（/models 后台页与 resolve 共用；只含元信息，不含任何密钥值）----
PROVIDER_CATALOG = [
    {
        "id": "openai-compatible",
        "name": "OpenAI 兼容接口",
        "desc": "通过 OPENAI 兼容的 /chat/completions 协议调用任意大模型（默认指向 DeepSeek）。可用环境变量或本页连接参数配置。",
        "stable_demo": False,
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "qwen-turbo",
                   "gpt-4o-mini", "gpt-4o", "moonshot-v1-8k"],
        "env": [
            {"key": "OPENAI_API_KEY", "label": "API Key", "secret": True},
            {"key": "OPENAI_BASE_URL", "label": "Base URL", "secret": False},
            {"key": "LLM_MODEL", "label": "默认模型", "secret": False},
        ],
        "needs": {"api_key": True},
    },
    {
        "id": "coze",
        "name": "Coze Bot",
        "desc": "调用 Coze 平台 Bot（HTTP v3 chat 等价实现）。需配置 Bot ID 与个人访问令牌。",
        "stable_demo": False,
        "default_model": "",
        "models": [],
        "env": [
            {"key": "COZE_API_KEY", "label": "访问令牌", "secret": True},
            {"key": "COZE_BOT_ID", "label": "Bot ID", "secret": False},
            {"key": "COZE_BASE_URL", "label": "Base URL", "secret": False},
        ],
        "needs": {"api_key": True, "bot_id": True},
    },
    {
        "id": "demo-fixture",
        "name": "演示剧本",
        "desc": "确定性离线演示 Provider：不联网、无需 Key，按问题类型返回稳定剧本；仍完整走 Planner→校验→执行→风控→RunRecord 链路，适合演示/验收。",
        "stable_demo": True,
        "default_model": "fixture-demo",
        "models": ["fixture-demo"],
        "env": [],
        "needs": {},
    },
]

def _provider_cls(name: str):
    """惰性解析 Provider 类（模块级表会在类定义前求值而 NameError，故放函数里运行时解析）。"""
    n = (name or "").strip().lower()
    alias = {"openai": "openai-compatible", "fixture": "demo-fixture"}
    n = alias.get(n, n)
    return {"openai-compatible": OpenAICompatibleProvider,
            "coze": CozeProvider,
            "demo-fixture": DemoFixtureProvider}.get(n)


def effective_provider_name() -> str:
    """当前实际生效的 Provider 名：环境变量 SNACK_LLM_PROVIDER > 后台已选(llm_state) > 默认。"""
    env_name = (os.environ.get("SNACK_LLM_PROVIDER") or "").strip().lower()
    if env_name:
        if _provider_cls(env_name) is not None:
            return "openai-compatible" if env_name == "openai" else env_name
    try:
        saved = (store.load_llm_state() or {}).get("provider") or ""
    except Exception:
        saved = ""
    if _provider_cls(saved) is not None:
        return "openai-compatible" if saved == "openai" else saved
    return "openai-compatible"


def resolve_provider():
    """选择 Provider 实例：env SNACK_LLM_PROVIDER 权威；未设则用后台已选(llm_state)，再默认。"""
    name = effective_provider_name()
    cls = _provider_cls(name)
    if not cls:
        raise LLMError(f"未知的 Provider：{name}（支持：openai-compatible / coze / demo-fixture）")
    return cls()


def _saved_config(name: str) -> dict:
    """取某 Provider 在后台保存的连接参数（llm_state），已填默认键。"""
    try:
        state = store.load_llm_state()
        provs = state.get("providers") or {}
        return dict(provs.get(name) or {})
    except Exception:
        return {}


def _env_var_set(keys) -> dict:
    return {k: bool((os.environ.get(k) or "").strip()) for k in keys}


# env 变量名 → llm_state.providers 里的存储键（用于只读地报告「后台是否已存」布尔）
_ENV_STORED_KEY = {
    "OPENAI_API_KEY": "api_key", "OPENAI_BASE_URL": "base_url", "LLM_MODEL": "model",
    "COZE_API_KEY": "api_key", "COZE_BOT_ID": "bot_id", "COZE_BASE_URL": "base_url",
}


def provider_catalog_status() -> list:
    """/models 页数据：目录元信息 + 每项是否已配置(env / 后台存储 / 旧 config.json)。
    密钥只返回「是否已配置」的布尔，绝不回显原文。"""
    llm_state = store.load_llm_state()
    provs = llm_state.get("providers") or {}
    legacy = store.load_config().get("llm", {})
    out = []
    for cat in PROVIDER_CATALOG:
        pid = cat["id"]
        stored = provs.get(pid) or {}
        env_keys = [e["key"] for e in cat["env"]]
        env_on = _env_var_set(env_keys)
        # env 变量名 → 后台存储键（OPENAI_API_KEY → api_key 等）；避免按变量名查存储导致 stored_set 恒 False
        stored_on = {k: bool((stored.get(_ENV_STORED_KEY.get(k, "")) or "").strip())
                     for k in env_keys if k in _ENV_STORED_KEY}
        # openai-compatible 的旧 config.json 回退
        extra = {}
        if pid == "openai-compatible":
            extra["legacy_key"] = bool((legacy.get("api_key") or "").strip())
            extra["legacy_base_url"] = bool((legacy.get("base_url") or "").strip())
            extra["legacy_model"] = bool((legacy.get("model") or "").strip())
        entry = {
            "id": pid, "name": cat["name"], "desc": cat["desc"],
            "stable_demo": cat["stable_demo"],
            "default_model": cat["default_model"], "models": cat["models"],
            "selected": (llm_state.get("provider") or "openai-compatible") == pid,
            "env_forcing": bool((os.environ.get("SNACK_LLM_PROVIDER") or "").strip()),
            "env": [{"key": e["key"], "label": e["label"], "secret": e["secret"],
                     "env_set": bool(env_on.get(e["key"])),
                     "stored_set": bool(stored_on.get(e["key"]))}
                    for e in cat["env"]],
            "extra": extra,
        }
        entry["ready"] = _provider_ready(pid)
        out.append(entry)
    return out


def _provider_ready(pid: str) -> bool:
    if pid == "demo-fixture":
        return True
    if pid == "coze":
        saved = _saved_config(pid)
        ok = (os.environ.get("COZE_API_KEY") or saved.get("api_key") or "").strip()
        bid = (os.environ.get("COZE_BOT_ID") or saved.get("bot_id") or "").strip()
        return bool(ok and bid)
    # openai-compatible
    saved = _saved_config(pid)
    if (os.environ.get("OPENAI_API_KEY") or saved.get("api_key") or "").strip():
        return True
    legacy = store.load_config().get("llm", {})
    return bool((legacy.get("api_key") or "").strip())


def config_for(pid: str) -> dict:
    """某 Provider 最终生效的连接参数：env > llm_state > 旧 config.json（openai-compatible）。"""
    saved = _saved_config(pid)
    cfg = {"api_key": "", "base_url": "", "model": "", "timeout": 90}
    if pid == "openai-compatible":
        legacy = store.load_config().get("llm", {})
        cfg.update({
            "api_key": (os.environ.get("OPENAI_API_KEY") or saved.get("api_key")
                        or legacy.get("api_key") or "").strip(),
            "base_url": (os.environ.get("OPENAI_BASE_URL") or saved.get("base_url")
                         or legacy.get("base_url") or "https://api.deepseek.com").rstrip("/"),
            "model": (os.environ.get("LLM_MODEL") or saved.get("model")
                      or legacy.get("model") or "deepseek-chat").strip(),
            "timeout": int(saved.get("timeout") or legacy.get("timeout") or 90),
        })
    elif pid == "coze":
        cfg.update({
            "api_key": (os.environ.get("COZE_API_KEY") or saved.get("api_key") or "").strip(),
            "bot_id": (os.environ.get("COZE_BOT_ID") or saved.get("bot_id") or "").strip(),
            "base_url": (os.environ.get("COZE_BASE_URL") or saved.get("base_url")
                         or "https://api.coze.com").rstrip("/"),
            "timeout": int(saved.get("timeout") or 90),
        })
    return cfg


def test_connection(provider: str | None = None) -> dict:
    """给 /api/llm-config/test 用：真实发一次最小请求验证连通与鉴权。
    失败抛 LLMError（结构化中文错误）；成功返回 {ok, provider, model, latency_ms, echo}。
    provider 缺省用 effective_provider_name()（env > 后台已选 > 默认）。"""
    pid = (provider or effective_provider_name()).strip().lower()
    cls = _provider_cls(pid)
    if not cls:
        raise LLMError(f"未知 Provider：{pid}")
    inst = cls()
    msgs = [{"role": "system", "content": "你是连接自检。请只回复两个字：正常。"},
            {"role": "user", "content": "连通性测试"}]
    res = inst.chat(msgs, json_mode=False)
    return {"ok": True, "provider": res.get("provider"), "model": res.get("model"),
            "latency_ms": res.get("latency_ms", 0), "echo": (res.get("content") or "")[:20]}


# ============================================================ openai-compatible
class OpenAICompatibleProvider:
    """读 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL 环境变量；未设置时回落本地 config.json 的 llm 字段。"""
    name = "openai-compatible"

    def chat(self, messages, *, json_mode=False, model=None, temperature=None,
             max_tokens=None, timeout=None, provider_hint=None):
        cfg = config_for(self.name)
        api_key = cfg["api_key"]
        base_url = cfg["base_url"]
        # 仅在纯 env OPENAI_API_KEY 指定（未配 base_url）时指向 OpenAI 官方，避免误打 DeepSeek
        if os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENAI_BASE_URL") \
                and not _saved_config("openai-compatible").get("base_url"):
            base_url = "https://api.openai.com/v1"
        model = model or cfg["model"]
        temperature = (store.load_config().get("llm", {}).get("default_temperature", 0.4)
                       if temperature is None else temperature)
        timeout = cfg["timeout"] if timeout is None else timeout

        if not api_key:
            raise LLMError(
                "尚未配置大模型 API Key：请设置环境变量 OPENAI_API_KEY，"
                f"或在后台「模型设置」填入 Key。{_DEMO_HINT}")

        url = base_url + "/chat/completions"
        body = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}",
                     "User-Agent": "snack-agent/1.0"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:400]
            except Exception:
                pass
            raise LLMError(f"大模型接口返回 HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise LLMError(f"无法连接大模型接口（{url}）：{e.reason}")
        except TimeoutError:
            raise LLMError(f"大模型请求超时（>{timeout}s）。")

        if not data.get("choices"):
            raise LLMError(f"大模型返回异常：{json.dumps(data, ensure_ascii=False)[:300]}")

        content = data["choices"][0]["message"].get("content", "")
        usage = data.get("usage") or {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        return {
            "content": content,
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": usage.get("total_tokens", pt + ct)},
            "model": data.get("model", model),
            "provider": self.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "cost_yuan": _est_cost(pt, ct),
        }


# ============================================================ coze（HTTP 等价）
class CozeProvider:
    """Coze Bot HTTP v3 chat。读 COZE_API_KEY / COZE_BOT_ID / COZE_BASE_URL（默认 https://api.coze.com）。

    Coze v3 为 bot 会话式接口（无法指定 response_format），故 json_mode 通过提示词约束 +
    成功后对返回消息做 JSON 抽取兜底。规划/技能输出统一用「必须输出 JSON」系统提示约束。
    """
    name = "coze"

    def chat(self, messages, *, json_mode=False, model=None, temperature=None,
             max_tokens=None, timeout=None, provider_hint=None):
        cfg = config_for(self.name)
        api_key = cfg.get("api_key", "")
        bot_id = cfg.get("bot_id", "")
        base_url = cfg.get("base_url", "https://api.coze.com")
        timeout = cfg.get("timeout", 90) if timeout is None else timeout
        if not api_key or not bot_id:
            raise LLMError(
                "Coze Provider 缺少配置：请在后台「模型设置」填写访问令牌与 Bot ID，"
                f"或设置环境变量 COZE_API_KEY / COZE_BOT_ID。{_DEMO_HINT}")

        user_msg = {"role": "user", "content_type": "text",
                    "content": messages[-1]["content"] if messages else ""}
        if json_mode:
            user_msg["content"] += "\n（务必只输出一个合法 JSON 对象，不要任何解释或代码块）"
        body = {
            "bot_id": bot_id,
            "user_id": "snack-agent-run",
            "stream": False,
            "auto_save_history": False,
            "additional_messages": [user_msg],
        }
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}",
                   "User-Agent": "snack-agent/1.0"}
        t0 = time.time()
        url = base_url + "/v3/chat"
        try:
            req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:400]
            except Exception:
                pass
            raise LLMError(f"Coze 接口返回 HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise LLMError(f"无法连接 Coze 接口（{url}）：{e.reason}")

        if data.get("code") not in (None, 0):
            raise LLMError(f"Coze 返回错误：{json.dumps(data, ensure_ascii=False)[:300]}")
        chat_id = (data.get("data") or {}).get("id")
        if not chat_id:
            raise LLMError(f"Coze 未返回 chat id：{json.dumps(data, ensure_ascii=False)[:300]}")

        # ---- 轮询拉取运行结果
        content = None
        deadline = time.time() + timeout
        retry_url = base_url + "/v3/chat/retrieve"
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                rq = urllib.request.Request(retry_url + "?chat_id=" + chat_id
                                            + "&conversation_id=" + str((data.get("data") or {}).get("conversation_id", "")),
                                            headers={"Authorization": f"Bearer {api_key}"}, method="GET")
                with urllib.request.urlopen(rq, timeout=timeout) as resp:
                    rd = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            status = ((rd.get("data") or {}).get("status") or "").upper()
            if status in ("COMPLETED", "SUCCESS", "SUCCEEDED"):
                try:
                    # 拉取对话消息取最后一条 assistant
                    msg_url = (base_url + "/v3/chat/message/list?chat_id=" + chat_id
                               + "&conversation_id=" + str((rd.get("data") or {}).get("conversation_id", "")))
                    mq = urllib.request.Request(msg_url,
                                                headers={"Authorization": f"Bearer {api_key}"}, method="GET")
                    with urllib.request.urlopen(mq, timeout=timeout) as resp:
                        md = json.loads(resp.read().decode("utf-8"))
                    for m in reversed((md.get("data") or [])):
                        if m.get("role") == "assistant" and m.get("type") == "answer":
                            content = m.get("content")
                            break
                except Exception:
                    pass
                break
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise LLMError(f"Coze 运行失败，status={status}")
        if content is None:
            raise LLMError(f"Coze 等待结果超时（>{timeout}s），chat_id={chat_id}")

        usage = (data.get("data") or {}).get("usage") or {}
        return {
            "content": content,
            "usage": {"prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                      "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                      "total_tokens": int(usage.get("total_tokens", 0) or 0)},
            "model": bot_id,
            "provider": self.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "cost_yuan": 0.0,
        }


# ============================================================ demo-fixture
class DemoFixtureProvider:
    """确定性演示 Provider：把问题映射到剧本，为 Planner/Skill 产出稳定输出。

    关键约束：fixture 只负责『扮演大模型返回内容』，其输出必须与真实模型同一套 schema，
    仍由 engine（plan/execute/_finalize/风控审计）+ runner（validator/RunRecord）完整编排，
    绝不直接写 PASS/FAIL 进结果。
    """
    name = "demo-fixture"

    def chat(self, messages, *, json_mode=False, model=None, temperature=None,
             max_tokens=None, timeout=None, provider_hint=None):
        t0 = time.time()
        last_user = _last_user_text(messages)
        sys_text = _sys_text(messages)
        user_obj = _try_json(last_user)
        question = _pick_question(user_obj, last_user)
        role = provider_hint or _infer_role(sys_text)
        scenario = classify_scenario(question) if question else "other"
        if scenario in ("other", "service") and looks_complaint(question):
            scenario = "complaint"  # 投诉优先于查物流/查单，演示中走转人工话术

        content = self._produce(role, scenario, question, user_obj, last_user, sys_text)
        if json_mode and role not in ("reply",):
            # 保证 JSON 技能返回合法 JSON 文本
            content = content.strip()
            if not content.startswith("{"):
                content = _force_json(content, role, scenario)
        return {
            "content": content,
            "usage": {"prompt_tokens": 12, "completion_tokens": len(content) // 2, "total_tokens": 60},
            "model": "fixture-demo",
            "provider": self.name,
            "latency_ms": int((time.time() - t0) * 1000),
            "cost_yuan": _est_cost(12, 40),
        }

    # ---------------- 剧本输出 ----------------
    def _produce(self, role, scenario, question, user_obj, last_user, sys_text):
        if role == "planner":
            return self._plan(scenario, question, user_obj)
        if role == "needs":
            return json.dumps(_needs_by_scenario(scenario, question), ensure_ascii=False)
        if role == "recommend":
            return json.dumps(_recommend(user_obj), ensure_ascii=False)
        if role == "reason":
            return json.dumps(_reason(user_obj), ensure_ascii=False)
        if role == "risk":
            risky = bool(detect_risk(question))
            return json.dumps({"isRisky": risky,
                               "riskType": ("中奖诈骗" if "中奖" in (question or "") else ("疑似风险" if risky else None)),
                               "safeReply": _safe_reply(question) if risky else ""}, ensure_ascii=False)
        if role == "moderation":
            reply = (user_obj or {}).get("reply") or ""
            empty = not str(reply or "").strip()
            return json.dumps({"pass": not empty, "level": "pass" if not empty else "high",
                               "issues": [] if not empty else [{"type": "policy", "detail": "无待审回复文本",
                                                                 "evidence": "空"}],
                               "suggestion": "请先生成客服回复再送审"}, ensure_ascii=False)
        # reply（纯文本客服话术）
        return _reply_text(scenario, question, user_obj)

    # ---------------- Planner 剧本 ----------------
    def _plan(self, scenario, question, user_obj):
        abilities = (user_obj or {}).get("abilities") or {}
        en_skills = {s.get("id") for s in abilities.get("skills") or []}
        en_tools = {t.get("id") for t in abilities.get("tools") or []}

        def step_skill(sid, purpose):
            return {"type": "skill", "id": sid, "purpose": purpose} if sid in en_skills else None

        def step_tool(sid, purpose):
            return {"type": "tool", "id": sid, "purpose": purpose} if sid in en_tools else None

        if scenario == "risk":
            steps = [s for s in [step_skill("risk", "识别风险诉求并生成安全回复话术")] if s]
            summary = "检测到疑似风险诉求，仅执行风险识别与安全回复，不做任何销售/收款/链接操作。"
        elif scenario == "complaint":
            steps = [s for s in [
                step_skill("needs", "结构化投诉/不满意图"),
                step_skill("reply", "生成致歉并转人工核实的客服回复"),
                step_skill("moderation", "对最终回复做风控审核"),
            ] if s]
            summary = "投诉/不满诉求：结构化 → 诚恳致歉并转交人工客服核实跟进 → 风控审核。"
        elif scenario == "service":
            steps = [s for s in [
                step_skill("needs", "把订单/物流诉求结构化为意图"),
                step_tool("query_service", "查询订单/物流/售后真实信息"),
                step_skill("reply", "基于真实服务信息生成客服回复"),
                step_skill("moderation", "对最终回复做风控审核"),
            ] if s]
            summary = "先结构化订单/物流诉求，再查真实服务数据并生成客服回复，最后风控审核。"
        elif scenario == "other":
            steps = [s for s in [
                step_skill("needs", "结构化寒暄/通用诉求"),
                step_skill("reply", "生成友好的客服开场回复"),
                step_skill("moderation", "对最终回复做风控审核"),
            ] if s]
            summary = "寒暄/通用诉求：结构化后给出礼貌开场并风控审核。"
        else:  # price / recommend：完整推荐链路，价格必须经 compute_price
            steps = [s for s in [
                step_skill("needs", "结构化推荐/价格需求"),
                step_tool("query_products", "查询真实在售商品"),
                step_tool("query_activities", "查询真实活动"),
                step_tool("query_coupons", "查询真实优惠券"),
                step_skill("recommend", "基于真实数据决定推荐商品"),
                step_tool("compute_price", "用代码精确计算到手价"),
                step_skill("reason", "撰写基于真实字段的推荐理由"),
                step_skill("reply", "生成含真实价格信息的最终客服回复"),
                step_skill("moderation", "对最终回复做风控审核"),
            ] if s]
            summary = "推荐/价格类问题：结构化 → 查真实数据 → 推荐 → 代码算价 → 理由 → 话术 → 风控。"
        return json.dumps({"summary": summary, "steps": steps}, ensure_ascii=False)


# ---------------------------------------------------------------- fixture 剧本辅助
def _last_user_text(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _sys_text(messages):
    for m in messages or []:
        if m.get("role") == "system":
            return m.get("content") or ""
    return ""


def _infer_role(sys_text):
    if "规划器" in sys_text or "planner" in sys_text.lower():
        return "planner"
    if "需求结构化" in sys_text:
        return "needs"
    if "推荐决策" in sys_text:
        return "recommend"
    if "推荐理由撰写" in sys_text:
        return "reason"
    if "风险识别" in sys_text:
        return "risk"
    if "安全合规审核" in sys_text:
        return "moderation"
    if "资深电商客服" in sys_text:
        return "reply"
    return None


def _try_json(text):
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t.startswith("```"):
        import re
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t).rstrip("`").strip()
    for cand in (t,):
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _pick_question(user_obj, last_user):
    if isinstance(user_obj, dict):
        for k in ("user_question", "question", "user_input"):
            if user_obj.get(k):
                return str(user_obj[k])
    return (last_user or "")[:200]


def _force_json(content, role, scenario):
    # 兜底：无法解析时给一个 schema 合法、内容诚实的最小对象（永不写 PASS/FAIL 结论）
    if role == "moderation":
        return json.dumps({"pass": True, "level": "pass", "issues": [], "suggestion": ""}, ensure_ascii=False)
    return "{}"


def _digits(question):
    import re
    nums = re.findall(r"\d+(?:\.\d+)?", question or "")
    return float(nums[0]) if nums else None


def _budget_digits(question):
    # 只把明确表达"预算上限"的数字当 budget：预算词/不超过/控制在 后的数字，或
    # "数字+元+以内/以下/左右/上下" 的金额量词形态；否则问句里的数字多为数量
    # （如"3袋每日坚果的价格"里的 3），误当预算会把真实商品按 ≤3 元过滤成空。
    q = question or ""
    for pat in (r"(?:预算|控制在|不超过|不高于)\s*[是]?\s*(\d+(?:\.\d+)?)",
                r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(以内|以下|左右|上下|内|预算)"):
        m = re.search(pat, q)
        if m:
            return float(m.group(1))
    return None


def _needs_by_scenario(scenario, question):
    q = question or ""
    kw = []
    for w in ("坚果", "辣条", "薯片", "巧克力", "饼干", "肉脯", "糖果", "魔芋", "礼盒"):
        if w in q:
            kw.append(w)
            break
    cats = {"坚果": "坚果炒货", "辣条": "辣味零食", "魔芋": "辣味零食", "薯片": "饼干膨化",
            "饼干": "饼干膨化", "巧克力": "糖果巧克力", "糖果": "糖果巧克力", "肉脯": "肉脯肉干"}
    category = cats.get(kw[0]) if kw else None
    if scenario == "complaint":
        return {"intent": "complaint", "query_keywords": [], "category": None,
                "budget": None, "quantity": None, "occasion": None, "gift": False,
                "constraints": [], "summary": "投诉/不满，需转人工客服跟进处理"}
    if scenario == "service":
        return {"intent": "order_service", "query_keywords": [], "category": None,
                "budget": None, "quantity": None, "occasion": None, "gift": False,
                "constraints": [], "summary": "查询订单/物流/售后信息"}
    if scenario == "risk":
        return {"intent": "other", "query_keywords": [], "category": None,
                "budget": None, "quantity": None, "occasion": None, "gift": False,
                "constraints": [], "summary": "疑似中奖/转账诈骗诉求，需风险识别"}
    if scenario == "price":
        return {"intent": "calculate_price", "query_keywords": kw, "category": category,
                "budget": _budget_digits(q), "quantity": _digits(q) if "款" in q or "个" in q else None,
                "occasion": None, "gift": "送人" in q or "送礼" in q,
                "constraints": [], "summary": "推荐并计算真实到手价"}
    if scenario == "recommend":
        return {"intent": "recommend_product", "query_keywords": kw, "category": category,
                "budget": _budget_digits(q), "quantity": _digits(q) if ("款" in q or "个" in q or "袋" in q) else None,
                "occasion": "聚会" if "聚会" in q else ("送人" if ("送人" in q or "送礼" in q) else None),
                "gift": "送人" in q or "送礼" in q, "constraints": [], "summary": "根据需求推荐零食"}
    return {"intent": "greeting", "query_keywords": [], "category": None, "budget": None,
            "quantity": None, "occasion": None, "gift": False, "constraints": [], "summary": "礼貌开场与通用服务"}


def _recommend(user_obj):
    """从注入的真实 products 中挑前 3 件（qty=1）。只引用真实 product_id。"""
    products = (user_obj or {}).get("products") or []
    items = []
    for p in products[:3]:
        if isinstance(p, dict) and p.get("id"):
            items.append({"product_id": p["id"], "qty": 1,
                          "note": "销量高、人气靠前" if p.get("sales", 0) > 50000 else "经典人气款"})
    return {"items": items, "coupon_id": None, "activity_id": None,
            "reason_summary": "从真实在售商品中挑选人气较高的款式", "remind": ""}


def _reason(user_obj):
    products = {p.get("id"): p for p in (user_obj or {}).get("products") or [] if isinstance(p, dict)}
    items = ((user_obj or {}).get("selection") or {}).get("items") or []
    reasons = []
    for it in items[:3]:
        p = products.get(it.get("product_id"))
        if not p:
            continue
        spec = p.get("spec", "")
        reasons.append({"product_id": p["id"],
                        "title": (p.get("name") or "")[:10],
                        "body": f"「{p.get('name')}」{('规格' + spec) if spec else ''}，"
                                f"标签：{'、'.join((p.get('tags') or [])[:3])}，人气与口碑都靠前，适合尝鲜/自留/送人。"})
    return {"reasons": reasons,
            "tips": "以上为真实在售商品与规格，具体到手价以结算页为准。"}


def _reply_text(scenario, question, user_obj):
    if scenario == "complaint":
        return ("很抱歉给您带来不好的体验。您反馈的情况我已记录，将立即转交人工客服/专员核实跟进，"
                "请留意后续专员联系；方便时可留下订单号或补充经过，以便尽快为您解决。")
    if scenario == "risk":
        return _safe_reply(question)
    if scenario == "service":
        return _service_reply(question, user_obj)
    price = (user_obj or {}).get("price")
    products = (user_obj or {}).get("products") or []
    if isinstance(price, dict) and price.get("items"):
        lines = []
        for it in price["items"]:
            lines.append(f"{it.get('name')} ×{it.get('qty')}")
        total = price.get("final_total")
        total_txt = f"预计到手价 ¥{total}" if isinstance(total, (int, float)) else "到手价以结算页为准"
        return ("亲，为您整理了几款人气零食：" + "；".join(lines) + f"。{total_txt}。"
                "下单前可再看看结算页的优惠券是否可用，具体以结算页为准。请问还需要别的吗？")
    if products:
        names = [f"「{p.get('name')}」" for p in products[:3] if isinstance(p, dict)]
        return "亲，为您整理了几款人气零食：" + "、".join(names) + "。如需价格明细我可以帮您算一下到手价，请问还需要别的吗？"
    if scenario == "other":
        return "亲，您好，很高兴为您服务～请问今天想了解什么零食呢？"
    return "亲，抱歉暂时没有匹配到合适的商品，建议换个关键词或品类再试试。请问还有什么可以帮您？"


_POLICY_WORDS = ("退款", "退货", "售后", "时效", "几天到", "多久", "赔付", "七天",
                 "无理由", "运费", "保障", "规则", "政策", "到账")
_ORDER_WORDS = ("订单", "物流", "快递", "运单", "发货", "签收", "包裹", "到哪",
                "到货", "配送", "派送", "进度", "催")


def _service_reply(question, user_obj):
    svc = (user_obj or {}).get("service")
    orders = (svc.get("orders") or []) if isinstance(svc, dict) else []
    policies = (svc.get("policies") or []) if isinstance(svc, dict) else []
    q = question or ""
    has_order_id = bool(re.search(r"[A-Za-z]?\d{8,}", q))
    has_policy = any(w in q for w in _POLICY_WORDS)
    has_order = has_order_id or any(w in q for w in _ORDER_WORDS)
    # 没给具体订单号、且问的是规则类问题 → 优先答真实售后/物流政策
    if not has_order and has_policy and policies:
        rules = []
        for p in policies[:2]:
            scope = p.get("scope", "")
            rules.append(f"{scope}：{p.get('rule', '')}" if scope else p.get("rule", ""))
        body = "；".join(r for r in rules if r)
        return (f"亲，关于您咨询的规则，目前平台相关政策是：{body}。"
                "具体以订单页/售后页实时显示为准。请问还有什么可以帮您？")
    if orders or policies:
        o = (orders or [{}])[0]
        p = (policies or [{}])[0]
        seg = []
        if o.get("status"):
            seg.append(f"订单 {o.get('order_id', '')} 当前为「{o.get('status')}」")
        if o.get("logistics"):
            seg.append(o.get("logistics"))
        if o.get("eta"):
            seg.append(o.get("eta"))
        if p.get("rule") and not seg:
            seg.append(f"{p.get('scope', '')}：{p.get('rule')}" if p.get("scope") else p.get("rule"))
        body = "；".join(seg) if seg else "已为您查询到相关服务信息"
        return f"亲，已为您查询：{body}。具体进展以订单页为准。请问还有什么可以帮您？"
    return "亲，已为您转达查询需求，订单/物流具体进展请您以订单页显示为准。请问还有什么可以帮您？"


def _safe_reply(question):
    typ = "中奖诈骗" if "中奖" in (question or "") else ("退款/刷单诈骗" if "退款" in (question or "") else "疑似诈骗")
    return (f"亲，您提到的情况疑似「{typ}」：任何要求先转账、交手续费/保证金、加QQ或点链接"
            "才能领奖/返款/理赔的，都是骗局，请千万不要向对方付款或提供验证码。"
            "我们平台客服绝不会要求您向个人账户转账。如已产生损失请立即报警或联系官方渠道核实。"
            "如果您是想正常购买零食，我很乐意继续为您服务。")
