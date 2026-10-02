"""LLM backends.

* ``OpenAICompatibleClient``: real tool-calling against any OpenAI-compatible
  endpoint (OpenRouter by default).
* ``HeuristicPlanner``: a deterministic, rule-based stand-in that needs no API
  key. It is NOT an LLM. It exists so the agent loop, guardrails and approval
  flow can be demoed and tested offline and in CI.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set (see .env.example), or run with --llm heuristic")

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "tools": tools, "temperature": 0},
            timeout=60,
        )
        if response.status_code != 200:
            raise LLMError(f"LLM request failed ({response.status_code}): {response.text[:200]}")
        message = response.json()["choices"][0]["message"]
        calls = []
        for raw in message.get("tool_calls") or []:
            try:
                arguments = json.loads(raw["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(ToolCall(raw["id"], raw["function"]["name"], arguments))
        return LLMResponse(message.get("content"), calls)


# ---------------------------------------------------------------- offline planner
HIGH_RISK = ("scale_job_memory", "apply_schema_mapping", "rollback_deployment")


def _history(messages: list[dict]) -> list[tuple[str, dict, dict]]:
    """Pair every assistant tool call with its tool result: (name, args, result)."""
    results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m.get("role") == "tool"}
    history = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            args = json.loads(call["function"]["arguments"] or "{}")
            history.append((call["function"]["name"], args, results.get(call["id"], {})))
    return history


def _find(history: list, name: str, pipeline: str | None = None) -> list[dict]:
    return [r for n, a, r in history if n == name and (pipeline is None or a.get("pipeline") == pipeline)]


class HeuristicPlanner:
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        history = _history(messages)
        step = self._next(history)
        if step is None:
            return LLMResponse(content=self._summary(history))
        name, args = step
        call = ToolCall(f"call_{len(history) + 1}", name, args)
        return LLMResponse(tool_calls=[call])

    def _next(self, history: list) -> tuple[str, dict] | None:
        listing = _find(history, "list_pipelines")
        if not listing:
            return "list_pipelines", {}
        unhealthy = [p["name"] for p in listing[0]["pipelines"] if p["status"] != "healthy"]
        for name in unhealthy:
            step = self._remediate(name, history)
            if step:
                return step
        if unhealthy and not _find(history, "send_notification"):
            return "send_notification", {"message": self._summary(history)}
        return None

    def _remediate(self, p: str, history: list) -> tuple[str, dict] | None:
        if _find(history, "escalate_to_human", p):
            return None
        logs = _find(history, "get_logs", p)
        if not logs:
            return "get_logs", {"pipeline": p, "tail": 10}

        for tool in HIGH_RISK:
            if any(r.get("status") == "denied" for r in _find(history, tool, p)):
                return "escalate_to_human", {
                    "pipeline": p,
                    "summary": f"Proposed fix '{tool}' was not approved and needs a human decision.",
                }

        text = "\n".join(logs[0]["lines"])
        status = _find(history, "get_status", p)
        verify_failed = len(status) >= 2 and status[-1]["status"] != "healthy"
        if verify_failed:
            return "escalate_to_human", {"pipeline": p, "summary": "Automated fix did not restore the pipeline."}

        if "OutOfMemoryError" in text:
            if not status:
                return "get_status", {"pipeline": p}
            if not _find(history, "scale_job_memory", p):
                return "scale_job_memory", {"pipeline": p, "memory_mb": min(status[0]["memory_mb"] * 2, 16_384)}
            if not _find(history, "restart_job", p):
                return "restart_job", {"pipeline": p}
            return ("get_status", {"pipeline": p}) if len(status) < 2 else None

        if "SchemaMismatch" in text:
            diff = _find(history, "get_schema_diff", p)
            if not diff:
                return "get_schema_diff", {"pipeline": p}
            if not _find(history, "apply_schema_mapping", p):
                d = diff[0]["differences"][0]
                return "apply_schema_mapping", {"pipeline": p, "column": d["column"], "target_type": d["expected"]}
            if not _find(history, "restart_job", p):
                return "restart_job", {"pipeline": p}
            return ("get_status", {"pipeline": p}) if not status else None

        if "401" in text or "credentials" in text.lower():
            return "escalate_to_human", {
                "pipeline": p,
                "summary": "Upstream credentials expired. Rotating secrets is outside the agent's authority.",
            }

        if "null_rate" in text.lower():
            if not _find(history, "run_quality_check", p):
                return "run_quality_check", {"pipeline": p}
            if not _find(history, "rollback_deployment", p):
                return "rollback_deployment", {"pipeline": p, "reason": "null-rate spike introduced by latest deploy"}
            return ("get_status", {"pipeline": p}) if not status else None

        return "escalate_to_human", {"pipeline": p, "summary": "Unrecognised failure pattern."}

    @staticmethod
    def _summary(history: list) -> str:
        action_tools = {"restart_job", "escalate_to_human", *HIGH_RISK}
        acted = [f"{n}({a.get('pipeline', '')})" for n, a, r in history
                 if n in action_tools and r.get("status") != "denied" and "error" not in r]
        denied = [f"{n}({a.get('pipeline', '')})" for n, a, r in history if r.get("status") == "denied"]
        text = "Incident sweep finished. Actions: " + (", ".join(acted) or "none") + "."
        if denied:
            text += " Denied by approver: " + ", ".join(denied) + "."
        return text
