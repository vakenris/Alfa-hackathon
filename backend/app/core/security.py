import re
from enum import Enum
from typing import Optional, List
from fastapi import Header, HTTPException, status


class UserRole(str, Enum):
    CEO = "CEO"                  # Генеральный директор / Собственник
    CFO = "CFO"                  # Финансовый директор
    TREASURY = "TREASURY"        # Руководитель казначейства
    ACCOUNTANT = "ACCOUNTANT"    # Главный бухгалтер
    ANALYST = "ANALYST"          # Аналитик / Сотрудник с доступом


ROLE_PERMISSIONS = {
    UserRole.CEO: ["view_strategy", "view_finances", "view_supply_chain", "execute_action"],
    UserRole.CFO: ["view_finances", "view_credit_limits", "view_market_risks", "execute_action"],
    UserRole.TREASURY: ["view_liquidity", "view_interest_rates", "view_hedging", "execute_action"],
    UserRole.ACCOUNTANT: ["view_accounting", "view_tax", "view_reports"],
    UserRole.ANALYST: ["view_analytics", "view_news"],
}


def get_current_user_role(
    x_user_role: Optional[str] = Header(default="CEO", alias="X-User-Role")
) -> UserRole:
    """Extract and validate the user role from request headers (defaults to CEO)."""
    try:
        return UserRole(x_user_role.upper())
    except (ValueError, AttributeError):
        return UserRole.CEO


def require_role(allowed_roles: List[UserRole]):
    def dependency(role: UserRole = Header(default=UserRole.CEO, alias="X-User-Role")):
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied for role '{role.value}'. Allowed: {[r.value for r in allowed_roles]}"
            )
        return role
    return dependency


def mask_sensitive_text(text: str) -> str:
    """Mask sensitive PII patterns like phone numbers, emails, and account numbers."""
    if not text:
        return ""
    # Mask emails: j***@domain.com
    text = re.sub(r'([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]+@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', r'\1***@\2', text)
    # Mask Russian phone numbers: +7 (9**) ***-**-**
    text = re.sub(r'(\+?7|8)[\s\-]??\(?(\d{2})\d{1}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', r'+7 (\2*) ***-**-**', text)
    # Mask bank accounts (20 digits): 40702810*********1234
    text = re.sub(r'(\d{8})\d{8}(\d{4})', r'\1*********\2', text)
    return text


def mask_financial_amount(amount: Optional[float], role: UserRole) -> Optional[float]:
    """Optionally obscure exact turnover for lower-privileged roles."""
    if amount is None:
        return None
    if role in [UserRole.CEO, UserRole.CFO, UserRole.TREASURY]:
        return amount
    # For accountants and analysts, round to nearest billion for privacy
    return round(amount, -8)
