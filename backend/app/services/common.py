from uuid import UUID

from app.core.errors import BusinessValidationError


def parse_uuid(value: str | UUID, field_name: str = "id") -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BusinessValidationError(f"{field_name} 不是有效 UUID: {value}") from exc
