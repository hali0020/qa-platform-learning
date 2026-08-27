import asyncio

from fastapi import APIRouter, Request

from app.schemas.response import ApiResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse)
async def health(request: Request) -> ApiResponse:
    # 主动让出事件循环，作为第一个可观察的异步示例。
    await asyncio.sleep(0)
    settings = request.app.state.settings
    return ApiResponse(
        data={
            "service": settings.app_name,
            "environment": settings.app_env,
            "local_only": settings.local_only,
        }
    )
