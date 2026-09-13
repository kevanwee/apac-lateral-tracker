"""Pipeline run bookkeeping.

Every stage execution writes a `pipeline_runs` row, whether it succeeds or
not. A run that crashed and left no record is a run nobody can debug.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg


def git_sha() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class RunRecorder:
    conn: psycopg.Connection
    run_id: str
    stage: str
    items_fetched: int = 0
    items_gate_rejected: int = 0
    moves_created: int = 0
    moves_merged: int = 0
    moves_queued_for_review: int = 0
    errors: int = 0
    llm_cost_usd: float = 0.0
    error_detail: list = field(default_factory=list)

    def fail(self, source: str, message: str) -> None:
        self.errors += 1
        self.error_detail.append({"source": source, "error": message[:500]})

    def source_yield(
        self,
        source_id: str,
        *,
        fetched: int,
        new: int,
        gate_passed: int,
        duration_ms: int | None = None,
        error: str | None = None,
        skipped_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO pipeline_run_sources
                (run_id, source_id, items_fetched, items_new, items_gate_passed,
                 duration_ms, error, skipped_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, source_id) DO UPDATE SET
                items_fetched = EXCLUDED.items_fetched,
                items_new = EXCLUDED.items_new,
                items_gate_passed = EXCLUDED.items_gate_passed,
                duration_ms = EXCLUDED.duration_ms,
                error = EXCLUDED.error,
                skipped_reason = EXCLUDED.skipped_reason
            """,
            (self.run_id, source_id, fetched, new, gate_passed,
             duration_ms, error, skipped_reason),
        )

        # Zero yield for three consecutive runs usually means the feed moved,
        # not that the market went quiet. Counter drives source_yield_alerts.
        if fetched > 0:
            self.conn.execute(
                "UPDATE sources SET consecutive_zero_yield_runs = 0 WHERE id = %s",
                (source_id,),
            )
        elif skipped_reason is None:
            self.conn.execute(
                "UPDATE sources SET consecutive_zero_yield_runs = "
                "consecutive_zero_yield_runs + 1 WHERE id = %s",
                (source_id,),
            )


@contextmanager
def record(
    conn: psycopg.Connection,
    stage: str,
    *,
    parent_run_id: str | None = None,
    params: dict | None = None,
):
    """Open a pipeline_runs row, close it whatever happens."""
    row = conn.execute(
        "INSERT INTO pipeline_runs (parent_run_id, stage, params, git_sha) "
        "VALUES (%s, %s, %s, %s) RETURNING run_id",
        (parent_run_id, stage, json.dumps(params or {}), git_sha()),
    ).fetchone()
    conn.commit()

    recorder = RunRecorder(conn=conn, run_id=row["run_id"], stage=stage)
    status = "succeeded"
    try:
        yield recorder
    except BaseException:
        status = "failed"
        raise
    finally:
        conn.execute(
            """
            UPDATE pipeline_runs SET
                status = %s, finished_at = now(),
                items_fetched = %s, items_gate_rejected = %s,
                moves_created = %s, moves_merged = %s,
                moves_queued_for_review = %s, errors = %s,
                error_detail = %s, llm_cost_usd = %s
            WHERE run_id = %s
            """,
            (
                status,
                recorder.items_fetched,
                recorder.items_gate_rejected,
                recorder.moves_created,
                recorder.moves_merged,
                recorder.moves_queued_for_review,
                recorder.errors,
                json.dumps(recorder.error_detail),
                round(recorder.llm_cost_usd, 4),
                recorder.run_id,
            ),
        )
        conn.commit()
