from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.oidc import OidcService

from app.database.session import Database
from app.core.config import Settings
from app.domain.models import (
    AuditEvent,
    Defect,
    Project,
    TestCase,
    TestCaseSnapshot,
    TestExecution,
    TestPlan,
    TestSuite,
)
from app.repositories.base import AsyncRepository
from app.repositories.memory import InMemoryRepository
from app.repositories.sqlalchemy import (
    AuditEventRepository,
    DefectRepository,
    ProjectRepository,
    TestCaseSnapshotRepository,
    TestCaseRepository,
    TestExecutionRepository,
    TestPlanRepository,
    TestSuiteRepository,
)
from app.services.audit import AuditService
from app.services.defects import DefectService
from app.services.executions import ExecutionService
from app.services.projects import ProjectService
from app.services.test_case_snapshots import TestCaseSnapshotService
from app.services.test_cases import TestCaseService
from app.services.test_plans import TestPlanService
from app.services.test_suites import TestSuiteService
from app.repositories.identity import IdentityRepository
from app.repositories.collaboration import AttachmentRepository, CommentRepository
from app.services.attachments import AttachmentService
from app.services.attachment_storage import AttachmentStorage
from app.services.collaboration_targets import CollaborationTargetResolver
from app.services.comments import CommentService
from app.services.identity import IdentityService
from app.services.local_attachment_storage import LocalAttachmentStorage
from app.services.data_transfer import DataTransferService
from app.services.quality import QualityService
from app.runtime.artifacts import ProviderRunArtifactService
from app.runtime.repository import RuntimeRepository


@dataclass(slots=True)
class ApplicationContainer:
    projects: ProjectService
    test_cases: TestCaseService
    test_suites: TestSuiteService
    test_case_snapshots: TestCaseSnapshotService
    test_plans: TestPlanService
    executions: ExecutionService
    defects: DefectService
    audit: AuditService
    data_transfer: DataTransferService
    quality: QualityService
    identity: IdentityService | None = None
    oidc: OidcService | None = None
    comments: CommentService | None = None
    attachments: AttachmentService | None = None
    provider_artifacts: ProviderRunArtifactService | None = None
    attachment_storages: tuple[AttachmentStorage, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    database: Database | None = None

    async def initialize(self) -> None:
        if self.database is not None:
            await self.database.initialize()

    async def shutdown(self) -> None:
        close_operations = [
            *(storage.aclose() for storage in self.attachment_storages),
        ]
        if self.oidc is not None:
            close_operations.append(self.oidc.aclose())
        resource_results = await asyncio.gather(
            *close_operations,
            return_exceptions=True,
        )
        try:
            if self.database is not None:
                await self.database.shutdown()
        finally:
            for result in resource_results:
                if isinstance(result, BaseException):
                    raise result


def build_container(
    database: Database | None = None,
    settings: Settings | None = None,
) -> ApplicationContainer:
    # 无参模式保留给纯 Service 单测；真实应用传入 Database 使用持久化仓储。
    business_lock = asyncio.Lock()
    if database is None:
        project_repository: AsyncRepository[Project] = InMemoryRepository()
        case_repository: AsyncRepository[TestCase] = InMemoryRepository()
        plan_repository: AsyncRepository[TestPlan] = InMemoryRepository()
        execution_repository: AsyncRepository[TestExecution] = (
            InMemoryRepository()
        )
        defect_repository: AsyncRepository[Defect] = InMemoryRepository()
        audit_repository: AsyncRepository[AuditEvent] = InMemoryRepository()
        suite_repository: AsyncRepository[TestSuite] = InMemoryRepository()
        snapshot_repository: AsyncRepository[TestCaseSnapshot] = (
            InMemoryRepository()
        )
    else:
        project_repository = ProjectRepository(database)
        case_repository = TestCaseRepository(database)
        plan_repository = TestPlanRepository(database)
        execution_repository = TestExecutionRepository(database)
        defect_repository = DefectRepository(database)
        audit_repository = AuditEventRepository(database)
        suite_repository = TestSuiteRepository(database)
        snapshot_repository = TestCaseSnapshotRepository(database)

    audit_service = AuditService(audit_repository, business_lock)
    suite_service = TestSuiteService(
        suite_repository,
        project_repository,
        case_repository,
        audit_service,
        business_lock,
    )
    snapshot_service = TestCaseSnapshotService(
        snapshot_repository,
        project_repository,
        suite_repository,
        case_repository,
        audit_service,
        business_lock,
    )
    defect_service = DefectService(
        defect_repository,
        project_repository,
        case_repository,
        execution_repository,
        audit_service,
        business_lock,
    )

    project_service = ProjectService(
        project_repository,
        case_repository,
        plan_repository,
        execution_repository,
        defect_repository,
        suite_repository,
        snapshot_repository,
        business_lock,
    )
    case_service = TestCaseService(
        case_repository,
        project_repository,
        plan_repository,
        suite_repository,
        defect_repository,
        business_lock,
    )
    plan_service = TestPlanService(
        plan_repository,
        project_repository,
        case_repository,
        execution_repository,
        business_lock,
    )
    execution_service = ExecutionService(
        execution_repository,
        case_repository,
        project_repository,
        plan_service,
        defect_repository,
        business_lock,
    )
    data_transfer_service = DataTransferService(
        projects=project_repository,
        test_cases=case_repository,
        test_suites=suite_repository,
        executions=execution_repository,
        defects=defect_repository,
        test_case_writer=case_service,
        defect_writer=defect_service,
    )
    quality_service = QualityService(
        projects=project_repository,
        test_cases=case_repository,
        test_suites=suite_repository,
        executions=execution_repository,
        defects=defect_repository,
        audit_events=audit_repository,
    )
    identity_service: IdentityService | None = None
    oidc_service: OidcService | None = None
    comment_service: CommentService | None = None
    attachment_service: AttachmentService | None = None
    provider_artifact_service: ProviderRunArtifactService | None = None
    attachment_storages: tuple[AttachmentStorage, ...] = ()
    if database is not None and settings is not None:
        identity_repository = IdentityRepository(database)
        comment_repository = CommentRepository(database)
        attachment_repository = AttachmentRepository(database)
        target_resolver = CollaborationTargetResolver(
            project_repository,
            case_repository,
            suite_repository,
            snapshot_repository,
            plan_repository,
            execution_repository,
            defect_repository,
        )
        identity_service = IdentityService(
            identity_repository,
            settings,
            business_lock,
        )
        if settings.auth_runtime_mode == "keycloak_local_container":
            # Keep PyJWT/cryptography and the network client dormant in the
            # default local-password mode. OIDC construction opens no socket.
            from app.services.oidc import OidcService

            oidc_service = OidcService(
                identity_repository,
                identity_service,
                settings,
            )
        comment_service = CommentService(
            comment_repository,
            target_resolver,
            audit_service,
            business_lock,
        )
        local_storage = LocalAttachmentStorage(
            settings.upload_root_path,
            max_bytes=settings.upload_max_bytes,
            max_image_pixels=settings.image_max_pixels,
        )
        storage_registry: dict[str, AttachmentStorage] = {
            local_storage.backend_name: local_storage,
        }
        write_backend = local_storage.backend_name
        if settings.object_storage_runtime_mode == "s3_local_container":
            # Keep this import and the aiobotocore dependency path dormant in
            # the default filesystem mode. Construction is configuration-only;
            # the adapter opens no socket until an attachment operation.
            from app.services.s3_attachment_storage import S3AttachmentStorage

            s3_storage = S3AttachmentStorage(
                app_env=settings.app_env,
                endpoint_url=settings.object_storage_endpoint_url,
                bucket=settings.object_storage_bucket,
                region=settings.object_storage_region,
                access_key=settings.object_storage_access_key,
                secret_key=settings.object_storage_secret_key,
                staging_root=settings.upload_root_path / ".s3-staging",
                max_bytes=settings.upload_max_bytes,
                max_image_pixels=settings.image_max_pixels,
                max_concurrency=settings.object_storage_max_concurrency,
                operation_timeout_seconds=(
                    settings.object_storage_operation_timeout_seconds
                ),
            )
            storage_registry[s3_storage.backend_name] = s3_storage
            write_backend = s3_storage.backend_name
        attachment_storages = tuple(storage_registry.values())
        attachment_service = AttachmentService(
            attachment_repository,
            comment_repository,
            target_resolver,
            storage_registry,
            write_backend,
            audit_service,
            business_lock,
        )
        provider_artifact_service = ProviderRunArtifactService(
            RuntimeRepository(database),
            storage_registry,
            write_backend,
            audit_service,
            business_lock,
        )

    return ApplicationContainer(
        projects=project_service,
        test_cases=case_service,
        test_suites=suite_service,
        test_case_snapshots=snapshot_service,
        test_plans=plan_service,
        executions=execution_service,
        defects=defect_service,
        audit=audit_service,
        data_transfer=data_transfer_service,
        quality=quality_service,
        identity=identity_service,
        oidc=oidc_service,
        comments=comment_service,
        attachments=attachment_service,
        provider_artifacts=provider_artifact_service,
        attachment_storages=attachment_storages,
        database=database,
    )
