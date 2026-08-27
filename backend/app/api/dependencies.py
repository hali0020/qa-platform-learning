from collections.abc import AsyncIterator, Callable

from fastapi import Depends, Request

from app.container import ApplicationContainer
from app.core.actor import ActorIdentity, reset_current_actor, set_current_actor
from app.core.errors import AuthenticationError, AuthorizationError
from app.domain.identity import PermissionCode, Principal


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container


async def get_current_principal(
    request: Request,
    container: ApplicationContainer = Depends(get_container),
) -> AsyncIterator[Principal]:
    settings = request.app.state.settings
    if not settings.auth_enabled:
        principal = Principal.test_admin()
        request.state.principal = principal
        yield principal
        return
    else:
        if container.identity is None:
            raise AuthenticationError("身份服务未初始化")
        raw_token = request.cookies.get(settings.session_cookie_name)
        if raw_token is None:
            raise AuthenticationError()
        principal = await container.identity.authenticate_session(raw_token)
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin is not None and origin not in settings.cors_origins:
                raise AuthorizationError("请求来源不在本机允许列表中")
            container.identity.validate_csrf(
                request.cookies.get(settings.csrf_cookie_name),
                request.headers.get("x-csrf-token"),
            )
    token = set_current_actor(
        ActorIdentity(user_id=principal.user_id, username=principal.username)
    )
    request.state.principal = principal
    try:
        yield principal
    finally:
        reset_current_actor(token)


def require_method_permission(
    read_permission: str | PermissionCode,
    write_permission: str | PermissionCode | None = None,
) -> Callable:
    async def dependency(
        request: Request,
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        permission = (
            read_permission
            if request.method.upper() in {"GET", "HEAD", "OPTIONS"}
            else write_permission or read_permission
        )
        if not principal.has_permission(permission):
            raise AuthorizationError()
        return principal

    return dependency


def require_permission(permission: str | PermissionCode) -> Callable:
    async def dependency(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if not principal.has_permission(permission):
            raise AuthorizationError()
        return principal

    return dependency
