from datetime import datetime, timedelta, timezone

import pytest

from app.automation import (
    AutomationConflictError,
    AutomationLeaseError,
    DeviceLeaseStatus,
    DeviceStatus,
    InMemoryDeviceManager,
)


BASE = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_device_capability_matching_and_exclusive_lease() -> None:
    manager = InMemoryDeviceManager(offline_after_seconds=30)
    device = await manager.register(
        name="Android-Local-01",
        agent_id="fake-agent-1",
        platform="android",
        capabilities={"android", "gpu", "api-35"},
    )
    assert device.status == DeviceStatus.OFFLINE

    online = await manager.heartbeat(
        device.id,
        "fake-agent-1",
        now=BASE,
    )
    assert online.status == DeviceStatus.IDLE

    claimed = await manager.acquire(
        task_id="task-1",
        owner="worker-a",
        required_capabilities={"android", "api-35"},
        now=BASE,
        lease_seconds=10,
    )
    assert claimed is not None
    assert claimed.device.status == DeviceStatus.RESERVED
    assert (
        await manager.acquire(
            task_id="task-2",
            owner="worker-b",
            required_capabilities={"android"},
            now=BASE,
        )
        is None
    )

    busy = await manager.start_work(
        claimed.lease.id,
        "worker-a",
        claimed.lease_token,
        now=BASE + timedelta(seconds=1),
    )
    assert busy.device.status == DeviceStatus.BUSY
    with pytest.raises(AutomationLeaseError):
        await manager.release(
            claimed.lease.id,
            "worker-a",
            "wrong-token",
            now=BASE + timedelta(seconds=2),
        )
    with pytest.raises(AutomationConflictError):
        await manager.set_maintenance(
            device.id,
            True,
            now=BASE + timedelta(seconds=2),
        )

    released = await manager.release(
        claimed.lease.id,
        "worker-a",
        claimed.lease_token,
        now=BASE + timedelta(seconds=3),
    )
    assert released.status == DeviceLeaseStatus.RELEASED
    maintenance = await manager.set_maintenance(
        device.id,
        True,
        now=BASE + timedelta(seconds=4),
    )
    assert maintenance.status == DeviceStatus.MAINTENANCE


@pytest.mark.asyncio
async def test_expired_device_lease_is_reclaimed_and_stale_heartbeat_is_offline() -> None:
    manager = InMemoryDeviceManager(offline_after_seconds=10)
    device = await manager.register(name="PC-01", agent_id="agent-pc", capabilities={"pc"})
    await manager.heartbeat(device.id, "agent-pc", now=BASE)
    claimed = await manager.acquire(
        task_id="task-1",
        owner="worker-a",
        now=BASE,
        lease_seconds=5,
    )
    assert claimed is not None

    expired = await manager.expire_leases(now=BASE + timedelta(seconds=5))
    assert expired[0].status == DeviceLeaseStatus.EXPIRED
    assert (await manager.get(device.id, now=BASE + timedelta(seconds=6))).status == DeviceStatus.IDLE
    assert (await manager.get(device.id, now=BASE + timedelta(seconds=10))).status == DeviceStatus.OFFLINE
