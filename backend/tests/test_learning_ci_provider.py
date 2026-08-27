import json

import httpx
import pytest

from app.pipeline.models import PipelineStatus
from app.pipeline.providers import (
    LearningCiPipelineProvider,
    OutboundPolicy,
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTriggerRequest,
)


async def loopback_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("127.0.0.1",)


def lab_policy() -> OutboundPolicy:
    return OutboundPolicy(
        allowed_hosts=("127.0.0.1",),
        allowed_ports=(23020,),
        allowed_networks=("127.0.0.1/32",),
        allowed_http_hosts=("127.0.0.1",),
    )


@pytest.mark.asyncio
async def test_learning_ci_uses_fixed_contract_and_normalizes_statuses() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii").split("?", maxsplit=1)[0]
        seen.append((request.method, raw_path))
        assert request.headers["Authorization"] == (
            "Bearer fake-learning-token-0123456789abcdef"
        )
        assert request.headers["Accept-Encoding"] == "identity"
        if request.method == "POST" and raw_path.endswith("/runs"):
            assert request.headers["Idempotency-Key"] == "qa-run-001"
            assert json.loads(request.content) == {
                "ref": "main",
                "variables": {"QA_RUN_ID": "local-1"},
            }
            return httpx.Response(
                201,
                json={
                    "id": "run_001",
                    "definition": "quality/gate",
                    "definition_revision": 3,
                    "status": "waiting_approval",
                    "web_url": None,
                    "message": "waiting for local approval",
                    "metadata": {"source": "learning"},
                    "replayed": False,
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "run_001",
                    "definition": "quality/gate",
                    "status": "running",
                    "web_url": None,
                    "message": None,
                    "metadata": {},
                    "replayed": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "run_001",
                "definition": "quality/gate",
                "status": "cancelled",
                "web_url": None,
                "message": "cancelled in the local lab",
                "metadata": {},
                "replayed": True,
            },
        )

    provider = LearningCiPipelineProvider(
        base_url="http://127.0.0.1:23020",
        definition_id="quality/gate",
        bearer_token="fake-learning-token-0123456789abcdef",
        policy=lab_policy(),
        enabled=True,
        resolver=loopback_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = await provider.trigger(
            ProviderTriggerRequest(
                definition_ref="quality/gate",
                ref="main",
                variables={"QA_RUN_ID": "local-1"},
                correlation_id="qa-run-001",
            )
        )
        running = await provider.get(created.external_id)
        cancelled = await provider.cancel(created.external_id)

        assert created.status == PipelineStatus.QUEUED
        assert created.raw_status == "waiting_approval"
        assert created.metadata == {
            "source": "learning",
            "definition_revision": 3,
            "replayed": False,
        }
        assert running.status == PipelineStatus.RUNNING
        assert cancelled.status == PipelineStatus.CANCELLED
        assert cancelled.metadata["replayed"] is True
        assert seen == [
            ("POST", "/api/v1/definitions/quality%2Fgate/runs"),
            ("GET", "/api/v1/runs/run_001"),
            ("POST", "/api/v1/runs/run_001/cancel"),
        ]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_learning_ci_rejects_unbound_or_non_idempotent_trigger() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = LearningCiPipelineProvider(
        base_url="http://127.0.0.1:23020",
        definition_id="quality-gate",
        bearer_token="fake-learning-token-0123456789abcdef",
        policy=lab_policy(),
        enabled=True,
        resolver=loopback_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderConfigurationError, match="definition"):
            await provider.trigger(
                ProviderTriggerRequest(
                    definition_ref="different-definition",
                    correlation_id="qa-run-001",
                )
            )
        with pytest.raises(ProviderConfigurationError, match="correlation"):
            await provider.trigger(
                ProviderTriggerRequest(definition_ref="quality-gate")
            )
        assert calls == 0
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_learning_ci_rejects_unknown_status_and_mismatched_definition() -> None:
    responses = iter(
        (
            {
                "id": "run_001",
                "definition": "quality-gate",
                "status": "mystery",
            },
            {
                "id": "run_001",
                "definition": "other-definition",
                "status": "queued",
            },
        )
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    provider = LearningCiPipelineProvider(
        base_url="http://127.0.0.1:23020",
        definition_id="quality-gate",
        bearer_token="fake-learning-token-0123456789abcdef",
        policy=lab_policy(),
        enabled=True,
        resolver=loopback_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderResponseError, match="unknown run status"):
            await provider.get("run_001")
        with pytest.raises(ProviderConfigurationError, match="definition"):
            await provider.get("run_001")
    finally:
        await provider.aclose()
