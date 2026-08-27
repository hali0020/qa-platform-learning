from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import (
    CI_LAB_HOST_ADDRESS,
    CI_LAB_PROVIDER_SECRET_NAME,
    Settings,
)
from app.core.errors import AuthorizationError, BusinessValidationError
from app.database.session import Database
from app.pipeline.models import PipelineStatus
from app.pipeline.providers.models import ProviderKind, ProviderRun
from app.runtime.schemas import ProviderConnectionCreate, ProviderTriggerPayload
from app.runtime.service import create_runtime_service


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


class _FakeLearningProvider:
    kind = ProviderKind.LEARNING_CI

    def __init__(self) -> None:
        self.triggered = 0

    async def trigger(self, request):
        self.triggered += 1
        assert request.definition_ref == "local-quality-gate"
        assert request.correlation_id == "lesson-run-1"
        return ProviderRun(
            provider=self.kind,
            external_id="lab-run-1",
            status=PipelineStatus.QUEUED,
            raw_status="queued",
        )

    async def get(self, external_id: str):
        assert external_id == "lab-run-1"
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.RUNNING,
            raw_status="running",
        )

    async def cancel(self, external_id: str):
        assert external_id == "lab-run-1"
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.CANCELLED,
            raw_status="cancelled",
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_learning_ci_runtime_uses_only_the_code_fixed_http_policy(
    tmp_path: Path,
) -> None:
    fake = _FakeLearningProvider()
    builder_calls = 0

    def builder(connection, secret, policy):
        nonlocal builder_calls
        builder_calls += 1
        assert connection.kind == ProviderKind.LEARNING_CI.value
        assert connection.base_url is None
        assert secret == "local-learning-token-value"
        assert policy.allowed_hosts == (CI_LAB_HOST_ADDRESS,)
        assert policy.allowed_ports == (23020,)
        assert policy.allowed_networks == ("127.0.0.1/32",)
        assert policy.allowed_http_hosts == (CI_LAB_HOST_ADDRESS,)
        return fake

    settings = Settings(
        provider_runtime_mode="ci_lab_local",
        provider_secret_env_names=(CI_LAB_PROVIDER_SECRET_NAME,),
    )
    database = Database(_sqlite_url(tmp_path / "learning-runtime.db"))
    service = create_runtime_service(
        database,
        settings,
        environ={CI_LAB_PROVIDER_SECRET_NAME: "local-learning-token-value"},
        provider_builder=builder,
    )
    try:
        connection = await service.create_connection(
            ProviderConnectionCreate(
                name="Owned Learning CI",
                kind="learning_ci",
                definition_ref="local-quality-gate",
                secret_env_var=CI_LAB_PROVIDER_SECRET_NAME,
                enabled=True,
            )
        )
        run = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(
                ref="main",
                variables={"QA_SCENARIO": "smoke"},
                correlation_id="lesson-run-1",
            ),
        )
        refreshed = await service.get_provider_run(connection.id, run.id)
        cancelled = await service.cancel_provider_run(connection.id, run.id)

        assert run.external_id == "lab-run-1"
        assert refreshed.status == PipelineStatus.RUNNING.value
        assert cancelled.status == PipelineStatus.CANCELLED.value
        assert builder_calls == 3
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_learning_ci_cannot_be_armed_through_generic_provider_fields(
    tmp_path: Path,
) -> None:
    database = Database(_sqlite_url(tmp_path / "learning-validation.db"))
    service = create_runtime_service(database)
    try:
        with pytest.raises(BusinessValidationError, match="Learning CI"):
            await service.create_connection(
                ProviderConnectionCreate(
                    name="Unsafe target override",
                    kind="learning_ci",
                    base_url="http://127.0.0.1:23020",
                    definition_ref="local-quality-gate",
                    secret_env_var=CI_LAB_PROVIDER_SECRET_NAME,
                )
            )

        connection = await service.create_connection(
            ProviderConnectionCreate(
                name="Disabled by global mode",
                kind="learning_ci",
                definition_ref="local-quality-gate",
                secret_env_var=CI_LAB_PROVIDER_SECRET_NAME,
                enabled=True,
            )
        )
        with pytest.raises(AuthorizationError, match="ci_lab_local"):
            await service.trigger_provider(
                connection.id,
                ProviderTriggerPayload(correlation_id="lesson-run-2"),
            )
    finally:
        await service.shutdown()
        await database.shutdown()
