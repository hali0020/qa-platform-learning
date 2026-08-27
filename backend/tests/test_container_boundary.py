from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _active_patterns(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _compose_service_block(compose: str, service: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(service)}:\s*$", compose)
    assert match is not None
    next_service = re.search(
        r"(?m)^  [a-zA-Z0-9_-]+:\s*$", compose[match.end() :]
    )
    if next_service is None:
        return compose[match.start() :]
    return compose[match.start() : match.end() + next_service.start()]


def test_root_dockerignore_is_deny_first_and_never_allows_local_state() -> None:
    patterns = _active_patterns(ROOT / ".dockerignore")

    assert patterns[0] == "**"
    forbidden_opt_ins = {
        "!.env",
        "!.env.*",
        "!.git",
        "!.git/**",
        "!.data",
        "!.data/**",
        "!backend/.venv",
        "!backend/.venv/**",
        "!frontend/node_modules",
        "!frontend/node_modules/**",
    }
    assert forbidden_opt_ins.isdisjoint(patterns)


def test_dockerfile_specific_contexts_are_also_deny_first() -> None:
    docker_root = ROOT / "infra" / "docker"
    for name in (
        "backend.Dockerfile.dockerignore",
        "frontend.Dockerfile.dockerignore",
        "object-storage-gateway.Dockerfile.dockerignore",
    ):
        patterns = _active_patterns(docker_root / name)
        assert patterns[0] == "**"
        assert all(not pattern.startswith("!.env") for pattern in patterns)
        assert all(".data" not in pattern for pattern in patterns)
        assert all(".git" not in pattern for pattern in patterns)


def test_internal_service_ports_are_not_published_to_the_host() -> None:
    compose = (ROOT / "infra" / "compose.phase2.yaml").read_text(
        encoding="utf-8"
    )

    for port in (5432, 5672, 15672, 8333, 9333, 8888, 7333, 23646):
        assert f'"127.0.0.1:{port}:{port}"' not in compose
        assert f'"{port}:{port}"' not in compose
    assert "internal: true" in compose


def test_seaweedfs_profile_is_digest_pinned_and_local_only() -> None:
    compose = (ROOT / "infra" / "compose.phase2.yaml").read_text(
        encoding="utf-8"
    )
    core = _compose_service_block(compose, "seaweedfs-core")
    gateway = _compose_service_block(compose, "seaweedfs")

    assert (
        "image: chrislusf/seaweedfs:4.44@sha256:"
        "e67e8c385484120b78bff47ba5f4debbca47fbd27ed1a39f016f47e8baea615b"
    ) in core
    assert 'profiles: ["object-storage"]' in core
    assert "ports:" not in core
    assert 'expose:\n      - "8333"' in core
    assert "- seaweedfs-data:/data" in core
    assert "seaweedfs-data:\n    driver: local" in compose
    assert "S3_BUCKET: qa-artifacts" in core
    assert "AWS_ACCESS_KEY_ID: ${OBJECT_STORAGE_ACCESS_KEY:-}" in core
    assert "AWS_SECRET_ACCESS_KEY: ${OBJECT_STORAGE_SECRET_KEY:-}" in core
    assert 'if [ -z "$${AWS_ACCESS_KEY_ID}" ]' in core
    assert '|| [ -z "$${AWS_SECRET_ACCESS_KEY}" ]' in core
    assert 'if [ "$${AWS_ACCESS_KEY_ID}" = "$${AWS_SECRET_ACCESS_KEY}" ]' in core
    assert "exec /entrypoint.sh mini" in core
    assert "-dir=/data" in core
    assert "-master.telemetry=false" in core
    assert "-webdav=false" in core
    assert "-admin.ui=false" in core
    assert "-s3.port.iceberg=0" in core
    assert "-s3.port.lance=0" in core
    assert "-s3.iam=false" in core
    assert "networks:\n      - object-storage-core" in core
    assert "qa-platform-learning-object-storage-internal" in compose
    assert "qa-platform-learning-object-storage-core-internal" in compose
    assert "read_only: true" in core
    assert "cap_drop:\n      - ALL" in core
    assert "cap_add:\n      - CHOWN\n      - SETGID\n      - SETUID" in core
    assert "no-new-privileges:true" in core

    forbidden_remote_identity_or_tiering = {
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "WEED_TIER",
        "cloud.config",
    }
    assert all(item not in core for item in forbidden_remote_identity_or_tiering)
    assert "minio/" not in compose.lower()

    assert "dockerfile: infra/docker/object-storage-gateway.Dockerfile" in gateway
    assert 'profiles: ["object-storage"]' in gateway
    assert "ports:" not in gateway
    assert 'expose:\n      - "8333"' in gateway
    assert "networks:\n      - object-storage\n      - object-storage-core" in gateway
    assert "AWS_ACCESS_KEY_ID" not in gateway
    assert "AWS_SECRET_ACCESS_KEY" not in gateway
    assert "read_only: true" in gateway
    assert "cap_drop:\n      - ALL" in gateway
    assert "no-new-privileges:true" in gateway

    gateway_dockerfile = (
        ROOT / "infra" / "docker" / "object-storage-gateway.Dockerfile"
    ).read_text(encoding="utf-8")
    assert (
        "FROM ghcr.io/nginx/nginx-unprivileged:1.30.4-alpine3.24@sha256:"
        "93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3"
    ) in gateway_dockerfile
    gateway_config = (
        ROOT / "infra" / "docker" / "object-storage-gateway.conf"
    ).read_text(encoding="utf-8")
    assert "listen 8333;" in gateway_config
    assert "server seaweedfs-core:8333 resolve;" in gateway_config
    assert "resolver 127.0.0.11 ipv6=off valid=10s;" in gateway_config
    assert "proxy_pass http://seaweedfs_s3;" in gateway_config
    assert "proxy_set_header Host $http_host;" in gateway_config
    for forbidden_port in (7333, 8181, 8888, 9101, 9333, 9340, 23646):
        assert str(forbidden_port) not in gateway_config

    backend = _compose_service_block(compose, "backend")
    assert "networks:\n      - default\n      - object-storage" in backend


def test_object_storage_defaults_to_local_filesystem_and_fixed_bucket() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    env_lines = set(env_example.splitlines())
    compose = (ROOT / "infra" / "compose.phase2.yaml").read_text(
        encoding="utf-8"
    )
    backend = _compose_service_block(compose, "backend")

    assert "OBJECT_STORAGE_RUNTIME_MODE=local_filesystem" in env_lines
    assert "COMPOSE_OBJECT_STORAGE_RUNTIME_MODE=local_filesystem" in env_lines
    assert "OBJECT_STORAGE_ENDPOINT_URL=" in env_lines
    assert "OBJECT_STORAGE_ACCESS_KEY=" in env_lines
    assert "OBJECT_STORAGE_SECRET_KEY=" in env_lines
    assert "OBJECT_STORAGE_BUCKET=" in env_lines
    assert "COMPOSE_OBJECT_STORAGE_BUCKET=" in env_lines
    assert "# COMPOSE_OBJECT_STORAGE_BUCKET=qa-artifacts" in env_lines
    assert "OBJECT_STORAGE_REGION=us-east-1" in env_lines
    assert (
        "OBJECT_STORAGE_RUNTIME_MODE: "
        "${COMPOSE_OBJECT_STORAGE_RUNTIME_MODE:-local_filesystem}"
    ) in backend
    assert (
        "OBJECT_STORAGE_ENDPOINT_URL: ${COMPOSE_OBJECT_STORAGE_ENDPOINT_URL:-}"
    ) in backend
    assert "OBJECT_STORAGE_BUCKET: ${COMPOSE_OBJECT_STORAGE_BUCKET:-}" in backend
    assert "OBJECT_STORAGE_REGION: us-east-1" in backend
    assert 'AWS_EC2_METADATA_DISABLED: "true"' in backend
