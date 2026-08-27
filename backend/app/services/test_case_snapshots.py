from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.models import (
    AuditAction,
    AuditChange,
    Project,
    ProjectStatus,
    SnapshotScopeType,
    TestCase,
    TestCaseSnapshot,
    TestCaseSnapshotItem,
    TestSuite,
)
from app.repositories.base import AsyncRepository
from app.schemas.test_case_snapshots import TestCaseSnapshotCreate
from app.services.audit import AuditService
from app.services.common import parse_uuid


class TestCaseSnapshotService:
    def __init__(
        self,
        snapshots: AsyncRepository[TestCaseSnapshot],
        projects: AsyncRepository[Project],
        test_suites: AsyncRepository[TestSuite],
        test_cases: AsyncRepository[TestCase],
        audit: AuditService,
        business_lock: Lock,
    ) -> None:
        self._snapshots = snapshots
        self._projects = projects
        self._test_suites = test_suites
        self._test_cases = test_cases
        self._audit = audit
        self._business_lock = business_lock

    async def create(self, payload: TestCaseSnapshotCreate) -> TestCaseSnapshot:
        async with self._business_lock:
            project_id = parse_uuid(payload.project_id, "project_id")
            project = await self._projects.get(project_id)
            if project is None:
                raise NotFoundError("项目", project_id)
            if project.status != ProjectStatus.ACTIVE:
                raise InvalidStateError("已归档项目不能创建新的测试用例快照")

            suites = [
                suite
                for suite in await self._test_suites.list()
                if suite.project_id == project_id
            ]
            suites_by_id = {suite.id: suite for suite in suites}
            if payload.suite_id is None:
                scope_type = SnapshotScopeType.PROJECT
                scope_id = project.id
                scope_name = project.name
                allowed_suite_ids: set[UUID] | None = None
            else:
                suite_id = parse_uuid(payload.suite_id, "suite_id")
                suite = suites_by_id.get(suite_id)
                if suite is None:
                    existing = await self._test_suites.get(suite_id)
                    if existing is None:
                        raise NotFoundError("测试套件", suite_id)
                    raise ConflictError("测试套件必须属于快照项目")
                scope_type = SnapshotScopeType.SUITE
                scope_id = suite.id
                scope_name = suite.name
                allowed_suite_ids = self._suite_scope_ids(
                    suite.id,
                    suites,
                    include_descendants=payload.include_descendants,
                )

            cases = [
                test_case
                for test_case in await self._test_cases.list()
                if test_case.project_id == project_id
                and (
                    allowed_suite_ids is None
                    or test_case.suite_id in allowed_suite_ids
                )
            ]
            if not cases:
                raise InvalidStateError("当前范围没有可归档的测试用例")

            ordered_cases = sorted(
                cases,
                key=lambda item: (
                    tuple(
                        part.casefold()
                        for part in self._suite_path(item.suite_id, suites_by_id)
                    ),
                    item.created_at,
                    str(item.id),
                ),
            )
            items = [
                self._snapshot_item(
                    test_case,
                    position,
                    suites_by_id,
                )
                for position, test_case in enumerate(ordered_cases)
            ]
            existing_snapshots = await self._snapshots.list()
            version = 1 + max(
                (
                    snapshot.version
                    for snapshot in existing_snapshots
                    if snapshot.scope_type == scope_type
                    and snapshot.scope_id == scope_id
                ),
                default=0,
            )
            snapshot = TestCaseSnapshot(
                project_id=project_id,
                scope_type=scope_type,
                scope_id=scope_id,
                scope_name=scope_name,
                version=version,
                label=payload.label,
                description=payload.description,
                case_count=len(items),
                items=items,
            )
            created = await self._snapshots.create(snapshot)
            await self._audit._record_unlocked(
                project_id=project_id,
                entity_type="test_case_snapshot",
                entity_id=created.id,
                action=AuditAction.SNAPSHOT_CREATED,
                changes={
                    "scope_type": AuditChange(
                        before=None,
                        after=created.scope_type.value,
                    ),
                    "scope_id": AuditChange(
                        before=None,
                        after=str(created.scope_id),
                    ),
                    "version": AuditChange(before=None, after=created.version),
                    "case_count": AuditChange(
                        before=None,
                        after=created.case_count,
                    ),
                },
                comment="创建测试用例归档快照",
            )
            return created

    async def list(
        self,
        project_id: str | UUID | None = None,
        scope_type: SnapshotScopeType | None = None,
        scope_id: str | UUID | None = None,
    ) -> list[TestCaseSnapshot]:
        async with self._business_lock:
            parsed_project_id = (
                parse_uuid(project_id, "project_id")
                if project_id is not None
                else None
            )
            parsed_scope_id = (
                parse_uuid(scope_id, "scope_id")
                if scope_id is not None
                else None
            )
            if (scope_type is None) != (parsed_scope_id is None):
                raise ConflictError("scope_type 和 scope_id 必须同时提供")
            items = await self._snapshots.list()
            if parsed_project_id is not None:
                items = [
                    item
                    for item in items
                    if item.project_id == parsed_project_id
                ]
            if scope_type is not None:
                items = [
                    item
                    for item in items
                    if item.scope_type == scope_type
                    and item.scope_id == parsed_scope_id
                ]
            return sorted(
                items,
                key=lambda item: (item.created_at, item.version),
                reverse=True,
            )

    async def get(self, snapshot_id: str | UUID) -> TestCaseSnapshot:
        async with self._business_lock:
            parsed_id = parse_uuid(snapshot_id, "snapshot_id")
            snapshot = await self._snapshots.get(parsed_id)
            if snapshot is None:
                raise NotFoundError("测试用例快照", parsed_id)
            return snapshot

    @staticmethod
    def _suite_scope_ids(
        root_id: UUID,
        suites: list[TestSuite],
        *,
        include_descendants: bool,
    ) -> set[UUID]:
        allowed = {root_id}
        if not include_descendants:
            return allowed
        while True:
            descendants = {
                suite.id
                for suite in suites
                if suite.parent_id in allowed
            }
            expanded = allowed | descendants
            if expanded == allowed:
                return allowed
            allowed = expanded

    @classmethod
    def _suite_path(
        cls,
        suite_id: UUID | None,
        suites_by_id: dict[UUID, TestSuite],
    ) -> list[str]:
        names: list[str] = []
        visited: set[UUID] = set()
        current_id = suite_id
        while current_id is not None:
            if current_id in visited:
                raise ConflictError("测试套件层级存在循环，无法创建快照")
            visited.add(current_id)
            suite = suites_by_id.get(current_id)
            if suite is None:
                raise NotFoundError("测试套件", current_id)
            names.append(suite.name)
            current_id = suite.parent_id
        names.reverse()
        return names

    @classmethod
    def _snapshot_item(
        cls,
        test_case: TestCase,
        position: int,
        suites_by_id: dict[UUID, TestSuite],
    ) -> TestCaseSnapshotItem:
        return TestCaseSnapshotItem(
            source_case_id=test_case.id,
            source_suite_id=test_case.suite_id,
            suite_path=cls._suite_path(test_case.suite_id, suites_by_id),
            position=position,
            title=test_case.title,
            preconditions=test_case.preconditions,
            steps=list(test_case.steps),
            priority=test_case.priority,
            case_type=test_case.case_type,
            status=test_case.status,
            tags=list(test_case.tags),
            source_created_at=test_case.created_at,
            source_updated_at=test_case.updated_at,
        )
