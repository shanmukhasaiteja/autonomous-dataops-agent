"""The agent loop: observe -> decide -> (approve) -> act -> verify, with guardrails.

Guardrails
* only registered tools can run, arguments are validated before execution
* high-risk tools go through an approval policy and are denied by default
* identical state-changing calls are blocked after MAX_REPEATS (loop protection)
* a hard step limit stops runaway runs
* every decision is written to an audit log
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .approvals import Approver
from .environment import ToolError
from .llm import LLMClient
from .tools import HIGH, READ, ToolRegistry

SYSTEM_PROMPT = """You are an autonomous DataOps engineer for a data platform.
Goal: find unhealthy pipelines, diagnose the root cause from logs and metrics,
fix what is safe to fix, verify the fix, and escalate what is not.

Rules:
- Always investigate (logs, status, schema diff) before changing anything.
- Prefer the smallest, most reversible fix. Never repeat a fix that already failed.
- High-risk tools need approval. If an action is denied, do not retry it: escalate to a human instead.
- If the cause is outside your authority (credentials, access, unknown), call escalate_to_human.
- After a fix, verify with get_status. Finish with a short factual summary and send_notification."""

MAX_REPEATS = 2


@dataclass
class RunReport:
    outcome: str  # completed | step_limit
    steps: int
    summary: str
    events: list[dict] = field(default_factory=list)


class AuditLog:
    def __init__(self, path: str | Path | None = None):
        self.entries: list[dict] = []
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **entry) -> dict:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self.entries.append(entry)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        return entry


class DataOpsAgent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        approver: Approver,
        max_steps: int = 40,
        audit: AuditLog | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.llm, self.tools, self.approver = llm, tools, approver
        self.max_steps = max_steps
        self.audit = audit or AuditLog()
        self.on_event = on_event or (lambda event: None)

    def run(self, goal: str = "Check every pipeline and resolve any incidents.") -> RunReport:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": goal}]
        seen: Counter = Counter()
        steps = 0
        while steps < self.max_steps:
            response = self.llm.chat(messages, self.tools.specs())
            if not response.tool_calls:
                return RunReport("completed", steps, response.content or "", self.audit.entries)
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in response.tool_calls
                ],
            })
            for call in response.tool_calls:
                steps += 1
                result = self._execute(call.name, call.arguments, steps, seen)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result)})
                if steps >= self.max_steps:
                    break
        self.audit.record(step=steps, kind="guardrail", detail="step limit reached, run aborted")
        return RunReport("step_limit", steps, "Stopped: step limit reached.", self.audit.entries)

    def _emit(self, **entry) -> None:
        self.on_event(self.audit.record(**entry))

    def _execute(self, name: str, args: dict, step: int, seen: Counter) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            self._emit(step=step, kind="blocked", tool=name, args=args, detail="unknown tool")
            return {"error": f"unknown tool '{name}'", "available": self.tools.names()}
        try:
            tool.validate(args)
        except ToolError as exc:
            self._emit(step=step, kind="blocked", tool=name, args=args, detail=f"invalid arguments: {exc}")
            return {"error": str(exc)}

        key = (name, json.dumps(args, sort_keys=True))
        seen[key] += 1
        if tool.risk != READ and seen[key] > MAX_REPEATS:
            self._emit(step=step, kind="blocked", tool=name, args=args, detail="repeated call blocked")
            return {"error": "this exact action was already attempted several times; choose a different approach"}

        if tool.risk == HIGH:
            decision = self.approver.decide(name, args)
            self._emit(step=step, kind="approval", tool=name, args=args, risk=tool.risk,
                       approved=decision.approved, detail=decision.reason)
            if not decision.approved:
                return {"status": "denied", "reason": decision.reason}

        try:
            result = tool.func(**args)
        except ToolError as exc:
            self._emit(step=step, kind="error", tool=name, args=args, risk=tool.risk, detail=str(exc))
            return {"error": str(exc)}
        self._emit(step=step, kind="action" if tool.risk != READ else "observe", tool=name, args=args,
                   risk=tool.risk, result=result)
        return result
