class DomainError(Exception):
    """可安全返回给 API 调用方的业务异常。"""

    def __init__(self, message: str, *, status_code: int, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class NotFoundError(DomainError):
    def __init__(self, resource: str, entity_id: object) -> None:
        super().__init__(
            f"{resource}不存在: {entity_id}",
            status_code=404,
            code=40400,
        )


class ConflictError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409, code=40900)


class InvalidStateError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409, code=40901)


class BusinessValidationError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=400, code=40000)


class AuthenticationError(DomainError):
    def __init__(self, message: str = "登录状态无效或已过期") -> None:
        super().__init__(message, status_code=401, code=40100)


class AuthorizationError(DomainError):
    def __init__(self, message: str = "没有执行该操作的权限") -> None:
        super().__init__(message, status_code=403, code=40300)
