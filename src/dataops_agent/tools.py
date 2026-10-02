"""Tool registry: JSON-schema tool specs, argument validation and risk levels."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .environment import Environment, ToolError

READ, WRITE, HIGH = "read", "write", "high"
_TYPES = {"string": str, "integer": int}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    required: tuple[str, ...]
    func: Callable[..., dict]
    risk: str  # read: observe only | write: low-risk, reversible | high: needs approval

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.risk} risk] {self.description}",
                "parameters": {"type": "object", "properties": self.properties, "required": list(self.required)},
            },
        }

    def validate(self, args: dict) -> None:
        unexpected = set(args) - set(self.properties)
        if unexpected:
            raise ToolError(f"unexpected arguments: {sorted(unexpected)}")
        missing = [r for r in self.required if r not in args]
        if missing:
            raise ToolError(f"missing required arguments: {missing}")
        for key, value in args.items():
            expected = _TYPES[self.properties[key]["type"]]
            if not isinstance(value, expected) or isinstance(value, bool):
                raise ToolError(f"argument '{key}' must be of type {self.properties[key]['type']}")


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)


def _p(description: str = "Pipeline name") -> dict:
    return {"type": "string", "description": description}


def build_registry(env: Environment) -> ToolRegistry:
    return ToolRegistry([
        Tool("list_pipelines", "List every pipeline with its current status.", {}, (), lambda: env.list_pipelines(), READ),
        Tool("get_status", "Get detailed health metrics for one pipeline.", {"pipeline": _p()}, ("pipeline",),
             lambda pipeline: env.get_status(pipeline), READ),
        Tool("get_logs", "Fetch the most recent log lines of a pipeline.",
             {"pipeline": _p(), "tail": {"type": "integer", "description": "Number of lines"}}, ("pipeline",),
             lambda pipeline, tail=10: env.get_logs(pipeline, tail), READ),
        Tool("get_schema_diff", "Compare source and target schema for a pipeline.", {"pipeline": _p()}, ("pipeline",),
             lambda pipeline: env.get_schema_diff(pipeline), READ),
        Tool("run_quality_check", "Run row-count, freshness and null-rate checks.", {"pipeline": _p()}, ("pipeline",),
             lambda pipeline: env.run_quality_check(pipeline), READ),
        Tool("restart_job", "Restart a pipeline run. Safe, but it will fail again if the root cause is not fixed.",
             {"pipeline": _p()}, ("pipeline",), lambda pipeline: env.restart_job(pipeline), WRITE),
        Tool("send_notification", "Post a summary to the data-platform channel.",
             {"message": {"type": "string", "description": "Message text"}}, ("message",),
             lambda message: env.send_notification(message), WRITE),
        Tool("escalate_to_human",
             "Open an incident ticket for a human. Use when the fix is unsafe, unauthorised or unknown.",
             {"pipeline": _p(), "summary": {"type": "string", "description": "What happened and what was tried"}},
             ("pipeline", "summary"), lambda pipeline, summary: env.escalate_to_human(pipeline, summary), WRITE),
        Tool("scale_job_memory", "Change a job's memory limit in MB (costs money, max 16384).",
             {"pipeline": _p(), "memory_mb": {"type": "integer", "description": "New limit in MB"}},
             ("pipeline", "memory_mb"), lambda pipeline, memory_mb: env.scale_job_memory(pipeline, memory_mb), HIGH),
        Tool("apply_schema_mapping", "Add a cast so a drifted source column matches the target type.",
             {"pipeline": _p(), "column": {"type": "string", "description": "Column name"},
              "target_type": {"type": "string", "description": "e.g. DOUBLE, BIGINT"}},
             ("pipeline", "column", "target_type"),
             lambda pipeline, column, target_type: env.apply_schema_mapping(pipeline, column, target_type), HIGH),
        Tool("rollback_deployment", "Roll a pipeline back to its previous version.",
             {"pipeline": _p(), "reason": {"type": "string", "description": "Why the rollback is needed"}},
             ("pipeline", "reason"), lambda pipeline, reason: env.rollback_deployment(pipeline, reason), HIGH),
    ])
