from __future__ import annotations

import pytest

from app.worker.handlers import HandlerFailure, build_safe_handler_registry


@pytest.mark.asyncio
async def test_fixed_registry_contains_only_the_four_local_handlers() -> None:
    registry = build_safe_handler_registry()

    assert registry.task_types == (
        "qa.device.execute",
        "qa.import.validate",
        "qa.pipeline.poll",
        "qa.quality.generate",
    )
    with pytest.raises(HandlerFailure) as raised:
        registry.resolve("os.system")
    assert raised.value.error_code == "worker_unknown_task_type"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_handlers_only_perform_bounded_deterministic_local_work() -> None:
    registry = build_safe_handler_registry()

    imported = await registry.resolve("qa.import.validate")(
        {"rows": [{"case": "login"}, {"case": "payment"}]}
    )
    quality = await registry.resolve("qa.quality.generate")(
        {"passed": 8, "failed": 1, "skipped": 1}
    )
    pipeline = await registry.resolve("qa.pipeline.poll")(
        {"observed_status": "running", "url": "https://must-not-be-opened.invalid"}
    )
    device = await registry.resolve("qa.device.execute")(
        {"steps": ["launch", "tap", "assert"]}
    )

    assert imported == {
        "handler": "qa.import.validate",
        "valid": True,
        "row_count": 2,
    }
    assert quality["total"] == 10
    assert quality["pass_rate"] == 0.8
    assert pipeline == {
        "handler": "qa.pipeline.poll",
        "simulated": True,
        "status": "running",
    }
    assert device == {
        "handler": "qa.device.execute",
        "simulated": True,
        "step_count": 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "payload"),
    [
        ("qa.import.validate", {"rows": "file:///untrusted/data.csv"}),
        ("qa.quality.generate", {"passed": True, "failed": 0, "skipped": 0}),
        ("qa.pipeline.poll", {"observed_status": "open-url"}),
        ("qa.device.execute", {"steps": ["subprocess"]}),
    ],
)
async def test_handlers_reject_unbounded_or_executable_instructions(
    task_type: str,
    payload: dict[str, object],
) -> None:
    handler = build_safe_handler_registry().resolve(task_type)

    with pytest.raises(HandlerFailure) as raised:
        await handler(payload)
    assert raised.value.error_code == "worker_invalid_payload"
    assert raised.value.retryable is False
