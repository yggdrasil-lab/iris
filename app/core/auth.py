import hmac
from typing import Optional
from fastapi import Header, HTTPException, status
from app.core.config import settings


def verify_webhook_secret(
    authorization: Optional[str] = Header(default=None),
) -> bool:
    """Validate Bearer token for inbound webhook requests using constant-time comparison."""
    configured_secret = (settings.IRIS_WEBHOOK_SECRET or "").strip()
    if not configured_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization scheme",
        )

    if not hmac.compare_digest(token.strip(), configured_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    return True


def require_authentik_operator(
    x_authentik_username: Optional[str] = Header(default=None, alias="X-authentik-username"),
    x_authentik_email: Optional[str] = Header(default=None, alias="X-authentik-email"),
) -> str:
    """Require Authentik identity headers in production; return operator label for audit logs."""
    operator = (x_authentik_username or "").strip() or (x_authentik_email or "").strip()
    if (settings.ENV or "").strip().lower() == "production":
        if not operator:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentik operator header required",
            )
        return operator
    return operator or "dev-operator"
