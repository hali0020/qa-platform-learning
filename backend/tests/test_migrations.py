from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def migrated_database(database_path: Path) -> sqlite3.Connection:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes.update(
        {
            "database_url": (
                f"sqlite+aiosqlite:///{database_path.as_posix()}"
            ),
            "configure_logger": False,
        }
    )
    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def project_values(key: str) -> tuple[str, str, str, str, str, str, str]:
    return (
        f"00000000-0000-0000-0000-{key.lower():0>12}",
        key,
        f"{key} project",
        "",
        "active",
        "2026-08-27T00:00:00+00:00",
        "2026-08-27T00:00:00+00:00",
    )


PROJECT_INSERT = """
    INSERT INTO projects (
        id, key, name, description, status, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def test_migration_schema_enforces_transactions_and_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "constraints.db"
    connection = migrated_database(database_path)
    try:
        connection.execute("BEGIN")
        connection.execute(PROJECT_INSERT, project_values("ROLLBACK"))
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM projects WHERE key = 'ROLLBACK'"
        ).fetchone() == (0,)

        connection.execute(PROJECT_INSERT, project_values("UNIQUE"))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(PROJECT_INSERT, project_values("UNIQUE"))
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO pipeline_runtime_trigger_keys (
                    idempotency_key, run_id, request_fingerprint
                ) VALUES ('orphan-key', 'missing-run', 'fingerprint')
                """
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO pipeline_runtime_runs (id, snapshot_json, updated_at)
            VALUES ('run-1', '{}', '2026-08-27T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO pipeline_runtime_trigger_keys (
                idempotency_key, run_id, request_fingerprint
            ) VALUES ('key-1', 'run-1', 'fingerprint')
            """
        )
        connection.execute(
            "DELETE FROM pipeline_runtime_runs WHERE id = 'run-1'"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_runtime_trigger_keys"
        ).fetchone() == (0,)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_connections (
                    id, name, kind, base_url, definition_ref, config,
                    secret_env_var, enabled, version, created_at, updated_at
                ) VALUES (
                    'invalid-provider', 'invalid', 'remote', NULL,
                    'local-only', '{}', NULL, 1, 0,
                    '2026-08-27T00:00:00+00:00',
                    '2026-08-27T00:00:00+00:00'
                )
                """
            )
        connection.rollback()

        role_count = connection.execute("SELECT COUNT(*) FROM roles").fetchone()
        permission_count = connection.execute(
            "SELECT COUNT(*) FROM role_permissions"
        ).fetchone()
        assert role_count is not None and role_count[0] >= 5
        assert permission_count is not None and permission_count[0] > 0
    finally:
        connection.close()


def test_phase6b_schema_enforces_orchestration_invariants_and_permissions(
    tmp_path: Path,
) -> None:
    connection = migrated_database(tmp_path / "phase6b-constraints.db")
    try:
        connection.execute(
            """
            INSERT INTO provider_connections (
                id, name, kind, base_url, definition_ref, config,
                secret_env_var, webhook_secret_env_var, enabled, version,
                created_at, updated_at
            ) VALUES (
                'connection-1', 'local provider', 'local', NULL, 'local-demo',
                '{}', NULL, NULL, 1, 0,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO provider_runs (
                id, connection_id, external_id, status, raw_status, web_url,
                message, metadata, correlation_id, request_fingerprint,
                dispatch_status, quality_gate_status, last_provider_sequence,
                reconciliation_required, triggered_by_name, version,
                created_at, updated_at
            ) VALUES (
                'run-1', 'connection-1', NULL, 'queued', 'pending', NULL,
                'waiting for gate', '{}', 'correlation-1', ?, 'pending',
                'waiting_approval', 0, 0, 'QA Lead', 0,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
            ("a" * 64,),
        )
        connection.commit()

        for statement in (
            "UPDATE provider_runs SET dispatch_status = 'bypassed' WHERE id = 'run-1'",
            "UPDATE provider_runs SET quality_gate_status = 'skipped' WHERE id = 'run-1'",
            "UPDATE provider_runs SET last_provider_sequence = -1 WHERE id = 'run-1'",
            "UPDATE provider_runs SET version = -1 WHERE id = 'run-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_run_approvals (
                    id, run_id, event_id, decision, request_fingerprint,
                    actor_user_id, actor_name, comment, created_at
                ) VALUES (
                    'approval-invalid', 'run-1', 'event-invalid', 'skip', ?,
                    NULL, 'QA Lead', '', '2026-08-28T00:00:00+00:00'
                )
                """,
                ("b" * 64,),
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO provider_run_approvals (
                id, run_id, event_id, decision, request_fingerprint,
                actor_user_id, actor_name, comment, created_at
            ) VALUES (
                'approval-1', 'run-1', 'event-1', 'approve', ?, NULL,
                'QA Lead', 'approved locally',
                '2026-08-28T00:00:00+00:00'
            )
            """,
            ("c" * 64,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_run_approvals (
                    id, run_id, event_id, decision, request_fingerprint,
                    actor_user_id, actor_name, comment, created_at
                ) VALUES (
                    'approval-2', 'run-1', 'event-2', 'reject', ?, NULL,
                    'Other Lead', 'second decision',
                    '2026-08-28T00:01:00+00:00'
                )
                """,
                ("d" * 64,),
            )
        connection.rollback()

        invalid_rows = (
            """
            INSERT INTO provider_run_artifacts (
                id, run_id, kind, status, original_filename, size_bytes,
                created_by_name, created_at, updated_at
            ) VALUES (
                'artifact-invalid', 'run-1', 'artifact', 'ready', 'bad.bin',
                -1, 'QA Lead', '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
            """
            INSERT INTO provider_webhook_events (
                id, connection_id, run_id, event_id, external_id, body_sha256,
                sequence, occurred_at, normalized_status, result, received_at,
                processed_at
            ) VALUES (
                'webhook-invalid', 'connection-1', 'run-1', 'event-webhook',
                'external-1', 'eeee', 0, '2026-08-28T00:00:00+00:00',
                'running', 'applied', '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
            """
            INSERT INTO provider_trigger_intents (
                id, run_id, connection_id, connection_version,
                request_payload, idempotency_key, request_fingerprint, status,
                attempts, max_attempts, available_at, created_at, updated_at
            ) VALUES (
                'intent-invalid', 'run-1', 'connection-1', 0, '{}',
                'idempotency-1', 'ffff', 'pending', 0, 0,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
        )
        for statement in invalid_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()

        approver_roles = connection.execute(
            """
            SELECT role_key FROM role_permissions
            WHERE permission_code = 'pipeline.approve'
            ORDER BY role_key
            """
        ).fetchall()
        assert approver_roles == [("qa_lead",), ("system_admin",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE code = 'pipeline.approve'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_phase6c_schema_enforces_scheduler_and_outbox_claim_shapes(
    tmp_path: Path,
) -> None:
    connection = migrated_database(tmp_path / "phase6c-constraints.db")
    try:
        connection.execute(
            """
            INSERT INTO schedules (
                id, name, task_type, payload, queue, priority, max_attempts,
                cron, timezone, misfire_policy, overlap_policy,
                misfire_grace_seconds, catch_up_limit, enabled, next_run_at,
                last_run_at, version, created_at, updated_at
            ) VALUES (
                'schedule-1', 'schedule one', 'qa.quality.generate', '{}',
                'default', 50, 3, '* * * * *', 'UTC', 'fire_once', 'forbid',
                60, 3, 1, '2026-08-28T00:01:00+00:00', NULL, 0,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO automation_tasks (
                id, task_type, payload, queue, priority, status,
                request_fingerprint, attempts, max_attempts, available_at,
                cancel_requested, created_at
            ) VALUES (
                'task-1', 'qa.quality.generate', '{}', 'default', 50,
                'queued', ?, 0, 3, '2026-08-28T00:00:00+00:00', 0,
                '2026-08-28T00:00:00+00:00'
            )
            """,
            ("a" * 64,),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE schedules SET claim_owner = 'scheduler-1' "
                "WHERE id = 'schedule-1'"
            )
        connection.rollback()

        invalid_outbox_rows = (
            """
            INSERT INTO automation_task_wakeup_outbox (
                id, task_id, generation, status, publish_attempts,
                available_at, version, created_at, updated_at
            ) VALUES (
                'outbox-claimed', 'task-1', 0, 'claimed', 1,
                '2026-08-28T00:00:00+00:00', 1,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
            """
            INSERT INTO automation_task_wakeup_outbox (
                id, task_id, generation, status, publish_attempts,
                available_at, version, created_at, updated_at
            ) VALUES (
                'outbox-published', 'task-1', 0, 'published', 1,
                '2026-08-28T00:00:00+00:00', 2,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """,
        )
        for statement in invalid_outbox_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()

        connection.execute(
            """
            INSERT INTO automation_task_wakeup_outbox (
                id, task_id, generation, status, publish_attempts,
                available_at, version, created_at, updated_at
            ) VALUES (
                'outbox-valid', 'task-1', 0, 'pending', 0,
                '2026-08-28T00:00:00+00:00', 0,
                '2026-08-28T00:00:00+00:00',
                '2026-08-28T00:00:00+00:00'
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO automation_task_wakeup_outbox (
                    id, task_id, generation, status, publish_attempts,
                    available_at, version, created_at, updated_at
                ) VALUES (
                    'outbox-duplicate', 'task-1', 0, 'pending', 0,
                    '2026-08-28T00:00:00+00:00', 0,
                    '2026-08-28T00:00:00+00:00',
                    '2026-08-28T00:00:00+00:00'
                )
                """
            )
    finally:
        connection.close()
