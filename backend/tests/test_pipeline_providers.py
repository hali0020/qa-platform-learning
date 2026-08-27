import json

import httpx
import pytest

from app.pipeline.models import PipelineStatus
from app.pipeline.providers import (
    BkCiPipelineProvider,
    GitLabPipelineProvider,
    JenkinsPipelineProvider,
    LocalPipelineProvider,
    OutboundPolicy,
    ProviderConflictError,
    ProviderTriggerRequest,
)


async def lab_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("10.20.30.40",)


def policy() -> OutboundPolicy:
    return OutboundPolicy(
        allowed_hosts=("ci.example.test",),
        allowed_networks=("10.20.30.40/32",),
    )


@pytest.mark.asyncio
async def test_local_provider_is_idempotent_and_never_requires_http() -> None:
    provider = LocalPipelineProvider()
    request = ProviderTriggerRequest(
        definition_ref="quality-gate",
        ref="main",
        correlation_id="local-event-1",
    )

    created = await provider.trigger(request)
    replayed = await provider.trigger(request)
    running = await provider.set_status(created.external_id, PipelineStatus.RUNNING)
    cancelled = await provider.cancel(created.external_id)

    assert created.external_id == replayed.external_id
    assert running.status == PipelineStatus.RUNNING
    assert cancelled.status == PipelineStatus.CANCELLED

    changed = request.model_copy(update={"ref": "release"})
    with pytest.raises(ProviderConflictError):
        await provider.trigger(changed)
    await provider.aclose()


@pytest.mark.asyncio
async def test_gitlab_provider_uses_v4_pipeline_contract_and_normalizes() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.raw_path.decode("ascii").split("?", maxsplit=1)[0],
            )
        )
        assert request.headers["PRIVATE-TOKEN"] == "fake-gitlab-token"
        if request.method == "POST" and request.url.path.endswith("/pipelines"):
            assert request.url.params["ref"] == "main"
            payload = json.loads(request.content)
            assert payload["variables"][0]["key"] == "QA_RUN_ID"
            return httpx.Response(
                201,
                json={
                    "id": 42,
                    "status": "pending",
                    "ref": "main",
                    "sha": "a" * 40,
                    "web_url": "https://ci.example.test/group/project/-/pipelines/42",
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json={"id": 42, "status": "running"})
        return httpx.Response(200, json={"id": 42, "status": "canceled"})

    provider = GitLabPipelineProvider(
        base_url="https://ci.example.test",
        project_id="group/project",
        private_token="fake-gitlab-token",
        policy=policy(),
        enabled=True,
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = await provider.trigger(
            ProviderTriggerRequest(
                definition_ref="group/project",
                ref="main",
                variables={"QA_RUN_ID": "local-1"},
            )
        )
        running = await provider.get(created.external_id)
        cancelled = await provider.cancel(created.external_id)

        assert created.status == PipelineStatus.QUEUED
        assert created.web_url == "https://ci.example.test/group/project/-/pipelines/42"
        assert running.status == PipelineStatus.RUNNING
        assert cancelled.status == PipelineStatus.CANCELLED
        assert seen == [
            ("POST", "/api/v4/projects/group%2Fproject/pipelines"),
            ("GET", "/api/v4/projects/group%2Fproject/pipelines/42"),
            ("POST", "/api/v4/projects/group%2Fproject/pipelines/42/cancel"),
        ]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_discards_untrusted_run_links() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": 43,
                "status": "pending",
                "web_url": "javascript:alert(document.domain)",
            },
        )

    provider = GitLabPipelineProvider(
        base_url="https://ci.example.test",
        project_id="group/project",
        private_token="fake-gitlab-token",
        policy=policy(),
        enabled=True,
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = await provider.trigger(
            ProviderTriggerRequest(definition_ref="group/project", ref="main")
        )
        assert created.web_url is None
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_jenkins_provider_tracks_queue_then_build_and_cancels() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.path.endswith("/buildWithParameters"):
            return httpx.Response(
                201,
                headers={"Location": "https://ci.example.test/queue/item/7/"},
            )
        if request.url.path == "/queue/item/7/api/json":
            return httpx.Response(200, json={"executable": {"number": 11}})
        if request.url.path.endswith("/11/api/json"):
            return httpx.Response(
                200,
                json={
                    "building": True,
                    "result": None,
                    "url": "https://ci.example.test/job/team/job/quality/11/",
                },
            )
        if request.url.path.endswith("/11/stop"):
            return httpx.Response(200)
        raise AssertionError(f"unexpected Jenkins path: {request.url.path}")

    provider = JenkinsPipelineProvider(
        base_url="https://ci.example.test",
        job_name="team/quality",
        username="qa-bot",
        api_token="fake-jenkins-token",
        policy=policy(),
        enabled=True,
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = await provider.trigger(
            ProviderTriggerRequest(
                definition_ref="team/quality",
                variables={"QA_RUN_ID": "local-1"},
            )
        )
        running = await provider.get(created.external_id)
        cancelled = await provider.cancel(created.external_id)

        assert created.external_id == "queue:7"
        assert running.status == PipelineStatus.RUNNING
        assert running.metadata["build_number"] == 11
        assert cancelled.status == PipelineStatus.CANCELLED
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_bkci_provider_uses_user_build_resource_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-DEVOPS-UID"] == "local-learner"
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"status": 0, "data": {"id": "b-100"}})
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={"status": 0, "data": {"status": "RUNNING"}},
            )
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"status": 0, "data": True})
        raise AssertionError(f"unexpected BK-CI path: {request.url.path}")

    provider = BkCiPipelineProvider(
        base_url="https://ci.example.test",
        project_id="learning-project",
        pipeline_id="quality-gate",
        user_id="local-learner",
        bearer_token="fake-bkci-token",
        policy=policy(),
        enabled=True,
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = await provider.trigger(
            ProviderTriggerRequest(
                definition_ref="quality-gate",
                variables={"qa_run_id": "local-1"},
            )
        )
        running = await provider.get(created.external_id)
        cancelled = await provider.cancel(created.external_id)

        assert created.external_id == "b-100"
        assert running.status == PipelineStatus.RUNNING
        assert cancelled.status == PipelineStatus.CANCELLED
    finally:
        await provider.aclose()
