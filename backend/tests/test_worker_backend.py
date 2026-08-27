from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.worker.backend import RuntimeTaskLeaseBackend


class FakeRuntimeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def claim_task(
        self,
        worker_id: str,
        queues: list[str],
        lease_seconds: int,
    ) -> Any:
        self.calls.append(("claim", (worker_id, queues, lease_seconds)))
        return SimpleNamespace(
            task=SimpleNamespace(
                id="task-1",
                task_type="qa.quality.generate",
                payload={"passed": 1, "failed": 0, "skipped": 0},
            ),
            lease_token="lease-token-for-adapter-test",
        )

    async def heartbeat_task(self, *args: Any) -> Any:
        self.calls.append(("heartbeat", args))
        return SimpleNamespace(cancel_requested=True)

    async def complete_task(self, *args: Any) -> None:
        self.calls.append(("complete", args))

    async def fail_task(self, *args: Any) -> None:
        self.calls.append(("fail", args))


@pytest.mark.asyncio
async def test_runtime_adapter_preserves_lease_authority_and_cancel_signal() -> None:
    service = FakeRuntimeService()
    backend = RuntimeTaskLeaseBackend(service)  # type: ignore[arg-type]

    claimed = await backend.claim("worker-1", ("default",), 30)
    assert claimed is not None
    assert claimed.task_id == "task-1"
    assert claimed.lease_token == "lease-token-for-adapter-test"
    rendered = repr(claimed)
    assert "lease-token-for-adapter-test" not in rendered
    assert "passed" not in rendered

    cancel_requested = await backend.heartbeat(claimed, "worker-1", 30)
    await backend.complete(claimed, "worker-1", {"ok": True})
    await backend.fail(claimed, "worker-1", "simulated", True)

    assert cancel_requested is True
    assert [name for name, _args in service.calls] == [
        "claim",
        "heartbeat",
        "complete",
        "fail",
    ]
