"""Simulated data-platform environment with injectable incidents.

It gives the agent something realistic to operate on without needing a real
cluster. Every incident only resolves when the *right* fix is applied: for
example, restarting a job that ran out of memory simply fails again.
"""
from __future__ import annotations

from dataclasses import dataclass, field

HEALTHY, FAILED, DEGRADED = "healthy", "failed", "degraded"
MAX_MEMORY_MB = 16_384
REQUIRED_MEMORY_MB = 4_096
INCIDENTS = ("schema_drift", "oom", "auth_expired", "null_spike")


class ToolError(Exception):
    """A tool call that is well-formed but cannot be carried out."""


@dataclass
class Pipeline:
    name: str
    status: str = HEALTHY
    freshness_minutes: int = 4
    rows_in: int = 120_000
    rows_out: int = 119_700
    memory_mb: int = 2_048
    version: str = "v42"
    previous_version: str = "v41"
    null_rates: dict[str, float] = field(default_factory=lambda: {"id": 0.0})
    expected_schema: dict[str, str] = field(default_factory=dict)
    actual_schema: dict[str, str] = field(default_factory=dict)
    schema_mappings: dict[str, str] = field(default_factory=dict)
    incident: str | None = None
    logs: list[str] = field(default_factory=list)


class Environment:
    def __init__(self, pipelines: list[Pipeline]):
        self.pipelines = {p.name: p for p in pipelines}
        self.escalations: list[dict] = []
        self.notifications: list[str] = []

    # ---------- helpers ----------
    def _get(self, name: str) -> Pipeline:
        try:
            return self.pipelines[name]
        except KeyError:
            raise ToolError(f"unknown pipeline '{name}'") from None

    @staticmethod
    def _resolve(p: Pipeline) -> None:
        p.status, p.incident, p.freshness_minutes = HEALTHY, None, 4
        p.rows_out = int(p.rows_in * 0.997)
        p.null_rates = {col: 0.0 for col in p.null_rates}
        p.logs.append("INFO run completed successfully")

    @staticmethod
    def _schema_fixed(p: Pipeline) -> bool:
        return all(p.schema_mappings.get(c) == t or p.actual_schema.get(c) == t for c, t in p.expected_schema.items())

    # ---------- read-only tools ----------
    def list_pipelines(self) -> dict:
        return {
            "pipelines": [
                {"name": p.name, "status": p.status, "freshness_minutes": p.freshness_minutes}
                for p in self.pipelines.values()
            ]
        }

    def get_status(self, name: str) -> dict:
        p = self._get(name)
        return {
            "name": p.name,
            "status": p.status,
            "freshness_minutes": p.freshness_minutes,
            "rows_in": p.rows_in,
            "rows_out": p.rows_out,
            "memory_mb": p.memory_mb,
            "version": p.version,
            "max_null_rate": max(p.null_rates.values(), default=0.0),
        }

    def get_logs(self, name: str, tail: int = 10) -> dict:
        return {"pipeline": name, "lines": self._get(name).logs[-max(1, tail):]}

    def get_schema_diff(self, name: str) -> dict:
        p = self._get(name)
        diffs = [
            {"column": col, "expected": exp, "actual": p.actual_schema.get(col)}
            for col, exp in p.expected_schema.items()
            if p.actual_schema.get(col) != exp and p.schema_mappings.get(col) != exp
        ]
        return {"pipeline": name, "differences": diffs}

    def run_quality_check(self, name: str) -> dict:
        p = self._get(name)
        ratio = round(p.rows_out / p.rows_in, 3) if p.rows_in else 0.0
        checks = [
            {"check": "row_count_ratio", "value": ratio, "passed": ratio >= 0.95},
            {"check": "freshness_minutes", "value": p.freshness_minutes, "passed": p.freshness_minutes <= 60},
        ] + [{"check": f"null_rate:{c}", "value": r, "passed": r <= 0.05} for c, r in p.null_rates.items()]
        return {"pipeline": name, "passed": all(c["passed"] for c in checks), "checks": checks}

    # ---------- low-risk actions ----------
    def restart_job(self, name: str) -> dict:
        p = self._get(name)
        if p.incident == "oom" and p.memory_mb < REQUIRED_MEMORY_MB:
            p.logs.append(f"ERROR java.lang.OutOfMemoryError: Java heap space (limit {p.memory_mb} MB)")
            p.status = FAILED
        elif p.incident == "schema_drift" and not self._schema_fixed(p):
            p.logs.append("ERROR SchemaMismatch: source schema no longer matches the target table")
            p.status = FAILED
        elif p.incident == "auth_expired":
            p.logs.append("ERROR 401 Unauthorized: upstream API credentials expired")
            p.status = FAILED
        elif p.incident == "null_spike":
            p.logs.append("WARN null_rate above threshold after the latest deployment")
        else:
            self._resolve(p)
        return {"restarted": True, "status": p.status}

    def send_notification(self, message: str) -> dict:
        self.notifications.append(message)
        return {"sent": True}

    def escalate_to_human(self, name: str, summary: str) -> dict:
        self._get(name)
        ticket = f"INC-{1000 + len(self.escalations) + 1}"
        self.escalations.append({"ticket": ticket, "pipeline": name, "summary": summary})
        return {"ticket": ticket}

    # ---------- high-risk actions (require approval) ----------
    def scale_job_memory(self, name: str, memory_mb: int) -> dict:
        p = self._get(name)
        if memory_mb <= 0 or memory_mb > MAX_MEMORY_MB:
            raise ToolError(f"memory_mb must be between 1 and {MAX_MEMORY_MB}")
        p.memory_mb = memory_mb
        p.logs.append(f"INFO memory limit changed to {memory_mb} MB")
        return {"memory_mb": memory_mb}

    def apply_schema_mapping(self, name: str, column: str, target_type: str) -> dict:
        p = self._get(name)
        if column not in p.actual_schema:
            raise ToolError(f"column '{column}' not found in source schema")
        p.schema_mappings[column] = target_type
        p.logs.append(f"INFO cast mapping applied: {column} -> {target_type}")
        return {"column": column, "target_type": target_type}

    def rollback_deployment(self, name: str, reason: str) -> dict:
        p = self._get(name)
        p.version, p.previous_version = p.previous_version, p.version
        p.logs.append(f"INFO rolled back to {p.version}: {reason}")
        if p.incident == "null_spike":
            self._resolve(p)
        return {"version": p.version}


def build_environment(incidents: tuple[str, ...] | list[str] = INCIDENTS) -> Environment:
    pipelines = {
        "orders_ingest": Pipeline(
            "orders_ingest",
            expected_schema={"order_id": "BIGINT", "amount": "DOUBLE"},
            actual_schema={"order_id": "BIGINT", "amount": "DOUBLE"},
        ),
        "clickstream_agg": Pipeline("clickstream_agg"),
        "inventory_sync": Pipeline("inventory_sync"),
        "users_dim": Pipeline("users_dim", null_rates={"user_id": 0.0, "email": 0.0}),
        "billing_export": Pipeline("billing_export"),
    }
    for incident in incidents:
        if incident not in INCIDENTS:
            raise ValueError(f"unknown incident '{incident}', choose from {INCIDENTS}")

    if "schema_drift" in incidents:
        p = pipelines["orders_ingest"]
        p.actual_schema["amount"] = "STRING"
        p.status, p.incident, p.freshness_minutes, p.rows_out = FAILED, "schema_drift", 95, 0
        p.logs += [
            "INFO starting run orders_ingest v42",
            "ERROR SchemaMismatch: column 'amount' expected DOUBLE but source delivered STRING",
            "ERROR run aborted, 0 rows written",
        ]
    if "oom" in incidents:
        p = pipelines["clickstream_agg"]
        p.status, p.incident, p.freshness_minutes, p.rows_out = FAILED, "oom", 140, 0
        p.logs += [
            "INFO starting run clickstream_agg v42",
            "WARN GC overhead above 85% on executor 3",
            "ERROR java.lang.OutOfMemoryError: Java heap space (limit 2048 MB)",
        ]
    if "auth_expired" in incidents:
        p = pipelines["inventory_sync"]
        p.status, p.incident, p.freshness_minutes, p.rows_out = FAILED, "auth_expired", 240, 0
        p.logs += [
            "INFO starting run inventory_sync v42",
            "ERROR 401 Unauthorized: upstream API credentials expired",
            "ERROR retries exhausted (5/5)",
        ]
    if "null_spike" in incidents:
        p = pipelines["users_dim"]
        p.status, p.incident = DEGRADED, "null_spike"
        p.null_rates["email"] = 0.35
        p.logs += [
            "INFO deployed users_dim v42 (previous: v41)",
            "WARN null_rate for column 'email' is 35% (threshold 5%)",
        ]
    return Environment(list(pipelines.values()))
