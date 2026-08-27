from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import get_container, get_current_principal
from app.container import ApplicationContainer
from app.core.errors import AuthenticationError, AuthorizationError
from app.domain.identity import Principal
from app.schemas.auth import (
    AuthResult,
    ChangePasswordRequest,
    LoginRequest,
    SetupRequest,
)
from app.schemas.response import ApiResponse
from app.services.identity import IssuedSession


router = APIRouter(prefix="/auth", tags=["authentication"])
Container = Annotated[ApplicationContainer, Depends(get_container)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _require_safe_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in request.app.state.settings.cors_origins:
        raise AuthorizationError("请求来源不在本机允许列表中")


def _set_session_cookies(
    response: Response,
    request: Request,
    issued: IssuedSession,
) -> None:
    settings = request.app.state.settings
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie(
        settings.session_cookie_name,
        issued.raw_token,
        max_age=max_age,
        path="/api/v1",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        issued.csrf_token,
        max_age=max_age,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_session_cookies(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        settings.session_cookie_name,
        path="/api/v1",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def _set_oidc_transaction_cookie(
    response: Response,
    request: Request,
    browser_binding: str,
) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        settings.oidc_transaction_cookie_name,
        browser_binding,
        max_age=settings.oidc_transaction_ttl_seconds,
        path="/api/v1/auth/oidc",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )


def _clear_oidc_transaction_cookie(
    response: Response,
    request: Request,
) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        settings.oidc_transaction_cookie_name,
        path="/api/v1/auth/oidc",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/status", response_model=ApiResponse)
async def auth_status(request: Request, container: Container) -> ApiResponse:
    settings = request.app.state.settings
    raw_token = request.cookies.get(settings.session_cookie_name)
    return ApiResponse(data=await container.identity.status(raw_token))


@router.post("/setup", response_model=ApiResponse)
async def setup(
    payload: SetupRequest,
    request: Request,
    response: Response,
    container: Container,
) -> ApiResponse:
    _require_safe_origin(request)
    issued = await container.identity.setup(payload)
    _set_session_cookies(response, request, issued)
    return ApiResponse(
        data=AuthResult(
            user=await container.identity.principal_view(issued.principal),
            csrf_token=issued.csrf_token,
        )
    )


@router.post("/login", response_model=ApiResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    container: Container,
) -> ApiResponse:
    _require_safe_origin(request)
    old_token = request.cookies.get(request.app.state.settings.session_cookie_name)
    issued = await container.identity.login(payload)
    if old_token:
        await container.identity.logout(old_token)
    _set_session_cookies(response, request, issued)
    return ApiResponse(
        data=AuthResult(
            user=await container.identity.principal_view(issued.principal),
            csrf_token=issued.csrf_token,
        )
    )


@router.get("/oidc/start", response_model=None)
async def oidc_start(
    request: Request,
    container: Container,
) -> RedirectResponse:
    _require_safe_origin(request)
    if container.oidc is None:
        raise AuthorizationError("OIDC 登录未启用")
    started = await container.oidc.start_authorization()
    response = RedirectResponse(
        started.authorization_url,
        status_code=status.HTTP_302_FOUND,
    )
    _set_oidc_transaction_cookie(
        response,
        request,
        started.browser_binding,
    )
    return response


@router.get("/oidc/callback", response_model=None)
async def oidc_callback(
    request: Request,
    container: Container,
) -> RedirectResponse:
    if container.oidc is None:
        raise AuthenticationError("OIDC 登录失败")
    query = request.query_params
    codes = query.getlist("code")
    states = query.getlist("state")
    if (
        len(codes) != 1
        or len(states) != 1
        or query.get("error") is not None
    ):
        raise AuthenticationError("OIDC 登录失败")
    settings = request.app.state.settings
    browser_binding = request.cookies.get(
        settings.oidc_transaction_cookie_name,
        "",
    )
    issued = await container.oidc.complete_authorization(
        code=codes[0],
        state=states[0],
        browser_binding=browser_binding,
    )
    old_token = request.cookies.get(settings.session_cookie_name)
    if old_token:
        await container.identity.logout(old_token)
    response = RedirectResponse(
        settings.oidc_post_login_redirect_uri,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_session_cookies(response, request, issued)
    _clear_oidc_transaction_cookie(response, request)
    response.headers.update(
        {
            "Content-Security-Policy": "default-src 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response


@router.get("/me", response_model=ApiResponse)
async def me(principal: CurrentPrincipal, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.identity.principal_view(principal))


@router.post("/logout", response_model=ApiResponse)
async def logout(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    del principal
    raw_token = request.cookies.get(request.app.state.settings.session_cookie_name)
    if raw_token:
        await container.identity.logout(raw_token)
    _clear_session_cookies(response, request)
    return ApiResponse(data={"logged_out": True})


@router.post("/change-password", response_model=ApiResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    await container.identity.change_password(principal, payload)
    _clear_session_cookies(response, request)
    return ApiResponse(data={"changed": True, "login_required": True})
