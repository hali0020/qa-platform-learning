import gzip

import httpx
import pytest

from app.pipeline.providers import (
    GitLabPipelineProvider,
    OutboundPolicy,
    ProviderDisabledError,
    ProviderResponseError,
    ProviderSecurityError,
    ProviderTriggerRequest,
    SafeHttpClient,
)
from app.pipeline.providers.security import validate_base_url


async def lab_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("10.20.30.40",)


def lab_policy(
    *,
    max_response_bytes: int = 1_048_576,
    allowed_networks: tuple[str, ...] = ("10.20.30.40/32",),
) -> OutboundPolicy:
    return OutboundPolicy(
        allowed_hosts=("ci.example.test",),
        allowed_networks=allowed_networks,
        max_response_bytes=max_response_bytes,
    )


def test_base_url_requires_exact_https_allowlist() -> None:
    policy = lab_policy()

    assert validate_base_url("https://ci.example.test", policy) == "https://ci.example.test"
    with pytest.raises(ProviderSecurityError):
        validate_base_url("http://ci.example.test", policy)
    with pytest.raises(ProviderSecurityError):
        validate_base_url("https://other.example.test", policy)
    credential_url = (
        "https://" + "test_user" + ":" + "test_password" + "@ci.example.test"
    )
    with pytest.raises(Exception):
        validate_base_url(credential_url, policy)


def test_http_exception_requires_an_exact_allowlisted_ip() -> None:
    policy = OutboundPolicy(
        allowed_hosts=("172.30.60.2",),
        allowed_ports=(8080,),
        allowed_networks=("172.30.60.2/32",),
        allowed_http_hosts=("172.30.60.2",),
    )

    assert (
        validate_base_url("http://172.30.60.2:8080", policy)
        == "http://172.30.60.2:8080"
    )
    with pytest.raises(Exception):
        OutboundPolicy(
            allowed_hosts=("ci-lab",),
            allowed_ports=(8080,),
            allowed_networks=("172.30.60.2/32",),
            allowed_http_hosts=("ci-lab",),
        )
    with pytest.raises(Exception):
        OutboundPolicy(
            allowed_hosts=("172.30.60.2",),
            allowed_ports=(8080,),
            allowed_networks=("172.30.60.3/32",),
            allowed_http_hosts=("172.30.60.2",),
        )


@pytest.mark.asyncio
async def test_private_resolution_is_denied_without_narrow_network_opt_in() -> None:
    async def private_resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("10.20.30.40",)

    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = SafeHttpClient(
        "https://ci.example.test",
        lab_policy(allowed_networks=("10.20.30.41/32",)),
        resolver=private_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderSecurityError):
            await client.request("GET", "/api/status")
        assert called is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_response_body_is_bounded() -> None:
    seen_paths: list[str] = []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest"})

    client = SafeHttpClient(
        "https://ci.example.test",
        lab_policy(),
        resolver=lab_resolver,
        transport=httpx.MockTransport(redirect_handler),
    )
    try:
        response = await client.request("GET", "/redirect")
        assert response.status_code == 302
        assert seen_paths == ["/redirect"]
    finally:
        await client.aclose()

    async def large_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    limited = SafeHttpClient(
        "https://ci.example.test",
        lab_policy(max_response_bytes=4),
        resolver=lab_resolver,
        transport=httpx.MockTransport(large_handler),
    )
    try:
        with pytest.raises(ProviderResponseError, match="size limit"):
            await limited.request("GET", "/large")
    finally:
        await limited.aclose()


@pytest.mark.asyncio
async def test_provider_rejects_compression_and_negative_content_length() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.headers["Accept-Encoding"] == "identity"
        if requests == 1:
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                content=gzip.compress(b"A" * 10_000),
            )
        return httpx.Response(200, headers={"Content-Length": "-1"})

    client = SafeHttpClient(
        "https://ci.example.test",
        lab_policy(max_response_bytes=64),
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderResponseError, match="encoding"):
            await client.request("GET", "/compressed")
        with pytest.raises(ProviderResponseError, match="size limit"):
            await client.request("GET", "/negative-length")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_reads_an_already_consumed_mock_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=b'{"ready":true}')
        response.read()
        return response

    client = SafeHttpClient(
        "https://ci.example.test",
        lab_policy(),
        resolver=lab_resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.request("GET", "/ready")
        assert response.json() == {"ready": True}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_external_provider_is_disabled_before_any_request() -> None:
    requests = 0
    resolutions = 0

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        nonlocal resolutions
        resolutions += 1
        return ("93.184.216.34",)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    provider = GitLabPipelineProvider(
        base_url="https://ci.example.test",
        project_id="learning/qa",
        private_token="not-a-real-token",
        policy=lab_policy(),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderDisabledError):
            await provider.trigger(
                ProviderTriggerRequest(
                    definition_ref="learning/qa",
                    ref="main",
                )
            )
        assert requests == 0
        assert resolutions == 0
    finally:
        await provider.aclose()
