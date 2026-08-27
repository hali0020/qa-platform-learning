from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from app.core.errors import ConflictError, NotFoundError
from app.database.base import Base
from app.database.models import (
    AuditEventRecord,
    CaseExecutionResultRecord,
    DefectRecord,
    ProjectRecord,
    TestCaseSnapshotItemRecord,
    TestCaseSnapshotRecord,
    TestCaseRecord,
    TestExecutionRecord,
    TestPlanCaseRecord,
    TestPlanRecord,
    TestSuiteRecord,
)
from app.database.session import Database
from app.domain.models import (
    AuditAction,
    AuditChange,
    AuditEvent,
    CaseExecutionResult,
    CaseResultStatus,
    Defect,
    DefectPriority,
    DefectSeverity,
    DefectStatus,
    ExecutionStatus,
    Project,
    ProjectStatus,
    SnapshotScopeType,
    TestCase,
    TestCaseSnapshot,
    TestCaseSnapshotItem,
    TestCasePriority,
    TestCaseStatus,
    TestCaseType,
    TestExecution,
    TestPlan,
    TestPlanStatus,
    TestStep,
    TestSuite,
    TestSuiteStatus,
)

EntityT = TypeVar("EntityT", bound=BaseModel)
RecordT = TypeVar("RecordT", bound=Base)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SqlAlchemyRepository(ABC, Generic[EntityT, RecordT]):
    """与现有异步内存仓储保持相同 CRUD 契约的 SQL 仓储基类。"""

    record_type: type[RecordT]
    eager_options: tuple = ()

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT:
        async with self._database.session() as session:
            await self._ensure_unique(session, entity, unique_fields)
            session.add(self._to_record(entity))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise self._conflict_error(entity, unique_fields) from exc
        return deepcopy(entity)

    async def get(self, entity_id: UUID) -> EntityT | None:
        statement = self._select_statement().where(
            self.record_type.id == str(entity_id)
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            return self._to_entity(record) if record is not None else None

    async def list(self) -> list[EntityT]:
        async with self._database.session() as session:
            records = (await session.scalars(self._select_statement())).all()
            return [self._to_entity(record) for record in records]

    async def update(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT:
        entity_id = getattr(entity, "id")
        statement = self._select_statement().where(
            self.record_type.id == str(entity_id)
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            if record is None:
                raise NotFoundError("实体", entity_id)
            await self._ensure_unique(
                session,
                entity,
                unique_fields,
                exclude_id=entity_id,
            )
            self._apply_entity(record, entity)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise self._conflict_error(entity, unique_fields) from exc
        return deepcopy(entity)

    async def delete(self, entity_id: UUID) -> bool:
        statement = self._select_statement().where(
            self.record_type.id == str(entity_id)
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            if record is None:
                return False
            await session.delete(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("实体仍被其他数据引用，不能删除") from exc
            return True

    async def clear(self) -> None:
        async with self._database.session() as session:
            records = (await session.scalars(self._select_statement())).all()
            for record in records:
                await session.delete(record)
            await session.commit()

    def _select_statement(self):
        statement = select(self.record_type)
        if self.eager_options:
            statement = statement.options(*self.eager_options)
        return statement

    async def _ensure_unique(
        self,
        session,
        entity: EntityT,
        fields: tuple[str, ...],
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        for field in fields:
            column: InstrumentedAttribute = getattr(self.record_type, field)
            statement = select(self.record_type.id).where(
                column == getattr(entity, field)
            )
            if exclude_id is not None:
                statement = statement.where(
                    self.record_type.id != str(exclude_id)
                )
            if (await session.scalar(statement)) is not None:
                raise ConflictError(
                    f"字段 {field} 的值已存在: {getattr(entity, field)}"
                )

    @staticmethod
    def _conflict_error(
        entity: EntityT,
        fields: tuple[str, ...],
    ) -> ConflictError:
        if fields:
            field = fields[0]
            return ConflictError(
                f"字段 {field} 的值已存在: {getattr(entity, field)}"
            )
        return ConflictError("数据违反唯一性或关联约束")

    @abstractmethod
    def _to_record(self, entity: EntityT) -> RecordT:
        raise NotImplementedError

    @abstractmethod
    def _to_entity(self, record: RecordT) -> EntityT:
        raise NotImplementedError

    @abstractmethod
    def _apply_entity(self, record: RecordT, entity: EntityT) -> None:
        raise NotImplementedError


class ProjectRepository(SqlAlchemyRepository[Project, ProjectRecord]):
    record_type = ProjectRecord

    def _to_record(self, entity: Project) -> ProjectRecord:
        return ProjectRecord(
            id=str(entity.id),
            key=entity.key,
            name=entity.name,
            description=entity.description,
            status=entity.status.value,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    def _to_entity(self, record: ProjectRecord) -> Project:
        return Project(
            id=UUID(record.id),
            key=record.key,
            name=record.name,
            description=record.description,
            status=ProjectStatus(record.status),
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
        )

    def _apply_entity(self, record: ProjectRecord, entity: Project) -> None:
        record.key = entity.key
        record.name = entity.name
        record.description = entity.description
        record.status = entity.status.value
        record.created_at = entity.created_at
        record.updated_at = entity.updated_at


class TestCaseRepository(SqlAlchemyRepository[TestCase, TestCaseRecord]):
    record_type = TestCaseRecord

    def _to_record(self, entity: TestCase) -> TestCaseRecord:
        return TestCaseRecord(
            id=str(entity.id),
            project_id=str(entity.project_id),
            suite_id=str(entity.suite_id) if entity.suite_id is not None else None,
            title=entity.title,
            preconditions=entity.preconditions,
            steps=[step.model_dump() for step in entity.steps],
            priority=entity.priority.value,
            case_type=entity.case_type.value,
            status=entity.status.value,
            tags=list(entity.tags),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    def _to_entity(self, record: TestCaseRecord) -> TestCase:
        return TestCase(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            suite_id=UUID(record.suite_id) if record.suite_id is not None else None,
            title=record.title,
            preconditions=record.preconditions,
            steps=[TestStep.model_validate(step) for step in record.steps],
            priority=TestCasePriority(record.priority),
            case_type=TestCaseType(record.case_type),
            status=TestCaseStatus(record.status),
            tags=list(record.tags),
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
        )

    def _apply_entity(self, record: TestCaseRecord, entity: TestCase) -> None:
        record.project_id = str(entity.project_id)
        record.suite_id = str(entity.suite_id) if entity.suite_id is not None else None
        record.title = entity.title
        record.preconditions = entity.preconditions
        record.steps = [step.model_dump() for step in entity.steps]
        record.priority = entity.priority.value
        record.case_type = entity.case_type.value
        record.status = entity.status.value
        record.tags = list(entity.tags)
        record.created_at = entity.created_at
        record.updated_at = entity.updated_at


class TestPlanRepository(SqlAlchemyRepository[TestPlan, TestPlanRecord]):
    record_type = TestPlanRecord
    eager_options = (selectinload(TestPlanRecord.case_links),)

    def _to_record(self, entity: TestPlan) -> TestPlanRecord:
        return TestPlanRecord(
            id=str(entity.id),
            project_id=str(entity.project_id),
            name=entity.name,
            description=entity.description,
            status=entity.status.value,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            case_links=[
                TestPlanCaseRecord(case_id=str(case_id), position=position)
                for position, case_id in enumerate(entity.case_ids)
            ],
        )

    def _to_entity(self, record: TestPlanRecord) -> TestPlan:
        return TestPlan(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            name=record.name,
            description=record.description,
            case_ids=[UUID(link.case_id) for link in record.case_links],
            status=TestPlanStatus(record.status),
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
        )

    def _apply_entity(self, record: TestPlanRecord, entity: TestPlan) -> None:
        record.project_id = str(entity.project_id)
        record.name = entity.name
        record.description = entity.description
        record.status = entity.status.value
        record.created_at = entity.created_at
        record.updated_at = entity.updated_at
        existing = {link.case_id: link for link in record.case_links}
        record.case_links = [
            self._plan_case_link(existing, case_id, position)
            for position, case_id in enumerate(entity.case_ids)
        ]

    @staticmethod
    def _plan_case_link(
        existing: dict[str, TestPlanCaseRecord],
        case_id: UUID,
        position: int,
    ) -> TestPlanCaseRecord:
        key = str(case_id)
        link = existing.get(key) or TestPlanCaseRecord(case_id=key)
        link.position = position
        return link


class TestExecutionRepository(
    SqlAlchemyRepository[TestExecution, TestExecutionRecord]
):
    record_type = TestExecutionRecord
    eager_options = (selectinload(TestExecutionRecord.results),)

    def _to_record(self, entity: TestExecution) -> TestExecutionRecord:
        return TestExecutionRecord(
            id=str(entity.id),
            plan_id=str(entity.plan_id),
            project_id=str(entity.project_id),
            status=entity.status.value,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            started_at=entity.started_at,
            completed_at=entity.completed_at,
            results=[
                self._result_record(result, position)
                for position, result in enumerate(entity.results)
            ],
        )

    def _to_entity(self, record: TestExecutionRecord) -> TestExecution:
        return TestExecution(
            id=UUID(record.id),
            plan_id=UUID(record.plan_id),
            project_id=UUID(record.project_id),
            status=ExecutionStatus(record.status),
            results=[
                CaseExecutionResult(
                    case_id=UUID(result.case_id),
                    case_title=result.case_title,
                    status=CaseResultStatus(result.status),
                    actual_result=result.actual_result,
                    comment=result.comment,
                    executed_at=_as_utc(result.executed_at),
                )
                for result in record.results
            ],
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
            started_at=_as_utc(record.started_at),
            completed_at=_as_utc(record.completed_at),
        )

    def _apply_entity(
        self,
        record: TestExecutionRecord,
        entity: TestExecution,
    ) -> None:
        record.plan_id = str(entity.plan_id)
        record.project_id = str(entity.project_id)
        record.status = entity.status.value
        record.created_at = entity.created_at
        record.updated_at = entity.updated_at
        record.started_at = entity.started_at
        record.completed_at = entity.completed_at
        existing = {result.case_id: result for result in record.results}
        record.results = [
            self._apply_result(existing, result, position)
            for position, result in enumerate(entity.results)
        ]

    @staticmethod
    def _result_record(
        result: CaseExecutionResult,
        position: int,
    ) -> CaseExecutionResultRecord:
        return CaseExecutionResultRecord(
            case_id=str(result.case_id),
            position=position,
            case_title=result.case_title,
            status=result.status.value,
            actual_result=result.actual_result,
            comment=result.comment,
            executed_at=result.executed_at,
        )

    @classmethod
    def _apply_result(
        cls,
        existing: dict[str, CaseExecutionResultRecord],
        result: CaseExecutionResult,
        position: int,
    ) -> CaseExecutionResultRecord:
        key = str(result.case_id)
        record = existing.get(key) or cls._result_record(result, position)
        record.position = position
        record.case_title = result.case_title
        record.status = result.status.value
        record.actual_result = result.actual_result
        record.comment = result.comment
        record.executed_at = result.executed_at
        return record


class DefectRepository(SqlAlchemyRepository[Defect, DefectRecord]):
    record_type = DefectRecord

    def _to_record(self, entity: Defect) -> DefectRecord:
        return DefectRecord(
            id=str(entity.id),
            project_id=str(entity.project_id),
            case_id=str(entity.case_id) if entity.case_id is not None else None,
            execution_id=(
                str(entity.execution_id) if entity.execution_id is not None else None
            ),
            title=entity.title,
            description=entity.description,
            severity=entity.severity.value,
            priority=entity.priority.value,
            status=entity.status.value,
            reporter=entity.reporter,
            assignee=entity.assignee,
            environment=entity.environment,
            reproduction_steps=list(entity.reproduction_steps),
            expected_result=entity.expected_result,
            actual_result=entity.actual_result,
            resolution=entity.resolution,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            resolved_at=entity.resolved_at,
            closed_at=entity.closed_at,
        )

    def _to_entity(self, record: DefectRecord) -> Defect:
        return Defect(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            case_id=UUID(record.case_id) if record.case_id is not None else None,
            execution_id=(
                UUID(record.execution_id)
                if record.execution_id is not None
                else None
            ),
            title=record.title,
            description=record.description,
            severity=DefectSeverity(record.severity),
            priority=DefectPriority(record.priority),
            status=DefectStatus(record.status),
            reporter=record.reporter,
            assignee=record.assignee,
            environment=record.environment,
            reproduction_steps=list(record.reproduction_steps),
            expected_result=record.expected_result,
            actual_result=record.actual_result,
            resolution=record.resolution,
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
            resolved_at=_as_utc(record.resolved_at),
            closed_at=_as_utc(record.closed_at),
        )

    def _apply_entity(self, record: DefectRecord, entity: Defect) -> None:
        replacement = self._to_record(entity)
        for field in (
            "project_id",
            "case_id",
            "execution_id",
            "title",
            "description",
            "severity",
            "priority",
            "status",
            "reporter",
            "assignee",
            "environment",
            "reproduction_steps",
            "expected_result",
            "actual_result",
            "resolution",
            "created_at",
            "updated_at",
            "resolved_at",
            "closed_at",
        ):
            setattr(record, field, getattr(replacement, field))


class AuditEventRepository(SqlAlchemyRepository[AuditEvent, AuditEventRecord]):
    record_type = AuditEventRecord

    def _to_record(self, entity: AuditEvent) -> AuditEventRecord:
        return AuditEventRecord(
            id=str(entity.id),
            project_id=(
                str(entity.project_id) if entity.project_id is not None else None
            ),
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            action=entity.action.value,
            actor=entity.actor,
            actor_user_id=(
                str(entity.actor_user_id)
                if entity.actor_user_id is not None
                else None
            ),
            changes={
                key: value.model_dump(mode="json")
                for key, value in entity.changes.items()
            },
            comment=entity.comment,
            created_at=entity.created_at,
        )

    def _to_entity(self, record: AuditEventRecord) -> AuditEvent:
        return AuditEvent(
            id=UUID(record.id),
            project_id=(
                UUID(record.project_id) if record.project_id is not None else None
            ),
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            action=AuditAction(record.action),
            actor=record.actor,
            actor_user_id=(
                UUID(record.actor_user_id)
                if record.actor_user_id is not None
                else None
            ),
            changes={
                key: AuditChange.model_validate(value)
                for key, value in record.changes.items()
            },
            comment=record.comment,
            created_at=_as_utc(record.created_at),
        )

    def _apply_entity(
        self,
        _record: AuditEventRecord,
        _entity: AuditEvent,
    ) -> None:
        raise RuntimeError("审计事件是不可变记录，不能更新")


class TestSuiteRepository(SqlAlchemyRepository[TestSuite, TestSuiteRecord]):
    record_type = TestSuiteRecord

    def _to_record(self, entity: TestSuite) -> TestSuiteRecord:
        return TestSuiteRecord(
            id=str(entity.id),
            project_id=str(entity.project_id),
            parent_id=str(entity.parent_id) if entity.parent_id is not None else None,
            name=entity.name,
            description=entity.description,
            status=entity.status.value,
            position=entity.position,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    def _to_entity(self, record: TestSuiteRecord) -> TestSuite:
        return TestSuite(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            parent_id=(UUID(record.parent_id) if record.parent_id is not None else None),
            name=record.name,
            description=record.description,
            status=TestSuiteStatus(record.status),
            position=record.position,
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
        )

    def _apply_entity(self, record: TestSuiteRecord, entity: TestSuite) -> None:
        record.project_id = str(entity.project_id)
        record.parent_id = (
            str(entity.parent_id) if entity.parent_id is not None else None
        )
        record.name = entity.name
        record.description = entity.description
        record.status = entity.status.value
        record.position = entity.position
        record.created_at = entity.created_at
        record.updated_at = entity.updated_at


class TestCaseSnapshotRepository(
    SqlAlchemyRepository[TestCaseSnapshot, TestCaseSnapshotRecord]
):
    record_type = TestCaseSnapshotRecord
    eager_options = (selectinload(TestCaseSnapshotRecord.items),)

    def _to_record(self, entity: TestCaseSnapshot) -> TestCaseSnapshotRecord:
        return TestCaseSnapshotRecord(
            id=str(entity.id),
            project_id=str(entity.project_id),
            scope_type=entity.scope_type.value,
            scope_id=str(entity.scope_id),
            scope_name=entity.scope_name,
            version=entity.version,
            label=entity.label,
            description=entity.description,
            case_count=entity.case_count,
            created_at=entity.created_at,
            items=[self._item_to_record(item) for item in entity.items],
        )

    def _to_entity(self, record: TestCaseSnapshotRecord) -> TestCaseSnapshot:
        return TestCaseSnapshot(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            scope_type=SnapshotScopeType(record.scope_type),
            scope_id=UUID(record.scope_id),
            scope_name=record.scope_name,
            version=record.version,
            label=record.label,
            description=record.description,
            case_count=record.case_count,
            items=[self._item_to_entity(item) for item in record.items],
            created_at=_as_utc(record.created_at),
        )

    def _apply_entity(
        self,
        _record: TestCaseSnapshotRecord,
        _entity: TestCaseSnapshot,
    ) -> None:
        raise RuntimeError("测试用例快照是不可变记录，不能更新")

    @staticmethod
    def _item_to_record(item: TestCaseSnapshotItem) -> TestCaseSnapshotItemRecord:
        return TestCaseSnapshotItemRecord(
            source_case_id=str(item.source_case_id),
            source_suite_id=(
                str(item.source_suite_id)
                if item.source_suite_id is not None
                else None
            ),
            suite_path=list(item.suite_path),
            position=item.position,
            title=item.title,
            preconditions=item.preconditions,
            steps=[step.model_dump() for step in item.steps],
            priority=item.priority.value,
            case_type=item.case_type.value,
            status=item.status.value,
            tags=list(item.tags),
            source_created_at=item.source_created_at,
            source_updated_at=item.source_updated_at,
        )

    @staticmethod
    def _item_to_entity(record: TestCaseSnapshotItemRecord) -> TestCaseSnapshotItem:
        return TestCaseSnapshotItem(
            source_case_id=UUID(record.source_case_id),
            source_suite_id=(
                UUID(record.source_suite_id)
                if record.source_suite_id is not None
                else None
            ),
            suite_path=list(record.suite_path),
            position=record.position,
            title=record.title,
            preconditions=record.preconditions,
            steps=[TestStep.model_validate(step) for step in record.steps],
            priority=TestCasePriority(record.priority),
            case_type=TestCaseType(record.case_type),
            status=TestCaseStatus(record.status),
            tags=list(record.tags),
            source_created_at=_as_utc(record.source_created_at),
            source_updated_at=_as_utc(record.source_updated_at),
        )
