from fastapi import APIRouter, Depends

from app.api.dependencies import require_method_permission
from app.domain.identity import PermissionCode

from app.api.routes.audit_events import router as audit_events_router
from app.api.routes.attachments import router as attachments_router
from app.api.routes.auth import router as auth_router
from app.api.routes.comments import router as comments_router
from app.api.routes.data_transfer import router as data_transfer_router
from app.api.routes.defects import router as defects_router
from app.api.routes.executions import router as executions_router
from app.api.routes.health import router as health_router
from app.api.routes.projects import router as projects_router
from app.api.routes.quality import router as quality_router
from app.api.routes.test_case_snapshots import router as test_case_snapshots_router
from app.api.routes.test_cases import router as test_cases_router
from app.api.routes.test_plans import router as test_plans_router
from app.api.routes.test_suites import router as test_suites_router
from app.api.routes.users import router as users_router
from app.pipeline.router import router as pipeline_router
from app.runtime.router import runtime_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(
    projects_router,
    dependencies=[
        Depends(
            require_method_permission(
                PermissionCode.QA_READ,
                PermissionCode.QA_WRITE,
            )
        )
    ],
)
for protected_router in (
    test_cases_router,
    test_suites_router,
    test_case_snapshots_router,
    test_plans_router,
    executions_router,
):
    api_router.include_router(
        protected_router,
        dependencies=[
            Depends(
                require_method_permission(
                    PermissionCode.QA_READ,
                    PermissionCode.QA_WRITE,
                )
            )
        ],
    )
api_router.include_router(
    defects_router,
    dependencies=[
        Depends(
            require_method_permission(
                PermissionCode.QA_READ,
                PermissionCode.DEFECTS_MANAGE,
            )
        )
    ],
)
api_router.include_router(
    audit_events_router,
    dependencies=[Depends(require_method_permission(PermissionCode.QA_READ))],
)
api_router.include_router(
    data_transfer_router,
    dependencies=[
        Depends(require_method_permission(PermissionCode.IMPORTS_MANAGE))
    ],
)
api_router.include_router(
    quality_router,
    dependencies=[Depends(require_method_permission(PermissionCode.REPORTS_READ))],
)
for collaboration_router in (comments_router, attachments_router):
    api_router.include_router(
        collaboration_router,
        dependencies=[
            Depends(
                require_method_permission(
                    PermissionCode.QA_READ,
                    PermissionCode.COLLABORATION_WRITE,
                )
            )
        ],
    )
api_router.include_router(
    pipeline_router,
    dependencies=[
        Depends(
            require_method_permission(
                PermissionCode.PIPELINE_READ,
                PermissionCode.PIPELINE_MANAGE,
            )
        )
    ],
)
api_router.include_router(runtime_router)
