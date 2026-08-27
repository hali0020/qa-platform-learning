from __future__ import annotations

import asyncio
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from app.automation.errors import (
    AutomationConflictError,
    AutomationLeaseError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from app.automation.models import (
    ClaimedDeviceLease,
    Device,
    DeviceLease,
    DeviceLeaseStatus,
    DeviceStatus,
    utc_now,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AutomationValidationError("device timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _token_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class InMemoryDeviceManager:
    """Capability matching and exclusive device leases for local lessons."""

    def __init__(self, *, offline_after_seconds: int = 90) -> None:
        if offline_after_seconds < 1:
            raise AutomationValidationError("device offline threshold must be positive")
        self._devices: dict[str, Device] = {}
        self._leases: dict[str, DeviceLease] = {}
        self._names: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._offline_after = timedelta(seconds=offline_after_seconds)

    async def register(
        self,
        *,
        name: str,
        agent_id: str,
        kind: str = "device",
        platform: str = "unknown",
        capabilities: set[str] | None = None,
    ) -> Device:
        async with self._lock:
            if name.casefold() in self._names:
                raise AutomationConflictError("device name already exists")
            if any(device.agent_id == agent_id for device in self._devices.values()):
                raise AutomationConflictError("device agent id already exists")
            device = Device(
                name=name,
                agent_id=agent_id,
                kind=kind,
                platform=platform,
                capabilities=capabilities or set(),
            )
            self._devices[device.id] = device
            self._names[name.casefold()] = device.id
            return self._copy_device(device)

    async def heartbeat(
        self,
        device_id: str,
        agent_id: str,
        *,
        now: datetime | None = None,
    ) -> Device:
        current = _as_utc(now or utc_now())
        async with self._lock:
            device = self._require_device(device_id)
            if not hmac.compare_digest(device.agent_id, agent_id):
                raise AutomationLeaseError("device agent identity does not match")
            device.last_heartbeat_at = current
            if device.status != DeviceStatus.MAINTENANCE:
                if device.active_lease_id is None:
                    device.status = DeviceStatus.IDLE if device.enabled else DeviceStatus.OFFLINE
            device.version += 1
            device.updated_at = current
            return self._effective_copy(device, current)

    async def get(self, device_id: str, *, now: datetime | None = None) -> Device:
        current = _as_utc(now or utc_now())
        async with self._lock:
            self._expire_locked(current)
            return self._effective_copy(self._require_device(device_id), current)

    async def list_devices(self, *, now: datetime | None = None) -> list[Device]:
        current = _as_utc(now or utc_now())
        async with self._lock:
            self._expire_locked(current)
            return [
                self._effective_copy(device, current)
                for device in sorted(self._devices.values(), key=lambda item: item.name.casefold())
            ]

    async def acquire(
        self,
        *,
        task_id: str,
        owner: str,
        required_capabilities: set[str] | None = None,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> ClaimedDeviceLease | None:
        if not task_id or not owner or lease_seconds < 1:
            raise AutomationValidationError("device lease request is invalid")
        current = _as_utc(now or utc_now())
        required = required_capabilities or set()
        async with self._lock:
            self._expire_locked(current)
            candidates = [
                device
                for device in self._devices.values()
                if device.enabled
                and self._effective_status(device, current) == DeviceStatus.IDLE
                and required.issubset(device.capabilities)
            ]
            if not candidates:
                return None
            device = min(candidates, key=lambda item: (item.name.casefold(), item.id))
            token = secrets.token_urlsafe(32)
            lease = DeviceLease(
                device_id=device.id,
                task_id=task_id,
                owner=owner,
                token_hash=_token_hash(token),
                acquired_at=current,
                expires_at=current + timedelta(seconds=lease_seconds),
            )
            self._leases[lease.id] = lease
            device.active_lease_id = lease.id
            device.status = DeviceStatus.RESERVED
            device.version += 1
            device.updated_at = current
            return ClaimedDeviceLease(
                device=self._copy_device(device),
                lease=self._copy_lease(lease),
                lease_token=token,
            )

    async def start_work(
        self,
        lease_id: str,
        owner: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> ClaimedDeviceLease:
        current = _as_utc(now or utc_now())
        async with self._lock:
            lease, device = self._require_lease(lease_id, owner, lease_token, current)
            device.status = DeviceStatus.BUSY
            device.version += 1
            device.updated_at = current
            return ClaimedDeviceLease(
                device=self._copy_device(device),
                lease=self._copy_lease(lease),
                lease_token=lease_token,
            )

    async def renew(
        self,
        lease_id: str,
        owner: str,
        lease_token: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> DeviceLease:
        if lease_seconds < 1:
            raise AutomationValidationError("device lease duration must be positive")
        current = _as_utc(now or utc_now())
        async with self._lock:
            lease, _device = self._require_lease(lease_id, owner, lease_token, current)
            lease.expires_at = current + timedelta(seconds=lease_seconds)
            lease.version += 1
            return self._copy_lease(lease)

    async def release(
        self,
        lease_id: str,
        owner: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> DeviceLease:
        current = _as_utc(now or utc_now())
        async with self._lock:
            lease, device = self._require_lease(lease_id, owner, lease_token, current)
            lease.status = DeviceLeaseStatus.RELEASED
            lease.released_at = current
            lease.version += 1
            device.active_lease_id = None
            device.status = self._available_status(device, current)
            device.version += 1
            device.updated_at = current
            return self._copy_lease(lease)

    async def set_maintenance(
        self,
        device_id: str,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> Device:
        current = _as_utc(now or utc_now())
        async with self._lock:
            device = self._require_device(device_id)
            if enabled and device.active_lease_id is not None:
                raise AutomationConflictError("an actively leased device cannot enter maintenance")
            device.status = (
                DeviceStatus.MAINTENANCE
                if enabled
                else self._available_status(device, current)
            )
            device.version += 1
            device.updated_at = current
            return self._effective_copy(device, current)

    async def expire_leases(self, *, now: datetime | None = None) -> list[DeviceLease]:
        current = _as_utc(now or utc_now())
        async with self._lock:
            return [self._copy_lease(lease) for lease in self._expire_locked(current)]

    def _expire_locked(self, now: datetime) -> list[DeviceLease]:
        expired: list[DeviceLease] = []
        for lease in self._leases.values():
            if lease.status == DeviceLeaseStatus.ACTIVE and lease.expires_at <= now:
                lease.status = DeviceLeaseStatus.EXPIRED
                lease.released_at = now
                lease.version += 1
                device = self._devices[lease.device_id]
                if device.active_lease_id == lease.id:
                    device.active_lease_id = None
                    device.status = self._available_status(device, now)
                    device.version += 1
                    device.updated_at = now
                expired.append(lease)
        return expired

    def _require_lease(
        self,
        lease_id: str,
        owner: str,
        lease_token: str,
        now: datetime,
    ) -> tuple[DeviceLease, Device]:
        try:
            lease = self._leases[lease_id]
        except KeyError as error:
            raise AutomationNotFoundError("device lease was not found") from error
        if (
            lease.status != DeviceLeaseStatus.ACTIVE
            or lease.owner != owner
            or lease.expires_at <= now
            or not hmac.compare_digest(lease.token_hash, _token_hash(lease_token))
        ):
            raise AutomationLeaseError("device lease is invalid or expired")
        device = self._devices[lease.device_id]
        if device.active_lease_id != lease.id:
            raise AutomationLeaseError("device lease is no longer active")
        return lease, device

    def _effective_status(self, device: Device, now: datetime) -> DeviceStatus:
        if device.status == DeviceStatus.MAINTENANCE:
            return DeviceStatus.MAINTENANCE
        if not device.enabled or device.last_heartbeat_at is None:
            return DeviceStatus.OFFLINE
        if device.last_heartbeat_at + self._offline_after <= now:
            return DeviceStatus.OFFLINE
        if device.active_lease_id is not None:
            return device.status
        return DeviceStatus.IDLE

    def _available_status(self, device: Device, now: datetime) -> DeviceStatus:
        if device.status == DeviceStatus.MAINTENANCE:
            return DeviceStatus.MAINTENANCE
        if not device.enabled or device.last_heartbeat_at is None:
            return DeviceStatus.OFFLINE
        return (
            DeviceStatus.IDLE
            if device.last_heartbeat_at + self._offline_after > now
            else DeviceStatus.OFFLINE
        )

    def _effective_copy(self, device: Device, now: datetime) -> Device:
        copied = self._copy_device(device)
        copied.status = self._effective_status(device, now)
        return copied

    def _require_device(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError as error:
            raise AutomationNotFoundError("device was not found") from error

    @staticmethod
    def _copy_device(device: Device) -> Device:
        return device.model_copy(deep=True)

    @staticmethod
    def _copy_lease(lease: DeviceLease) -> DeviceLease:
        return lease.model_copy(deep=True)


__all__ = ["InMemoryDeviceManager"]
