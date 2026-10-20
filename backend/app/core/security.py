from enum import Enum
from typing import Optional
from fastapi import Header


class UserRole(str, Enum):
    CEO = "CEO"
    CFO = "CFO"
    TREASURER = "TREASURER"
    ACCOUNTANT = "ACCOUNTANT"
    ANALYST = "ANALYST"


def get_current_user_role(
    x_user_role: Optional[str] = Header(default="CEO", alias="X-User-Role")
) -> UserRole:
    """Extract and validate the user role from request headers (defaults to CEO)."""
    try:
        return UserRole(x_user_role.upper())
    except (ValueError, AttributeError):
        return UserRole.CEO
