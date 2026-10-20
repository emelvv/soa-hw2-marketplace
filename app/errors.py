"""Stable error catalog published by the OpenAPI contract."""

from typing import Any


STATUS = {
    "PRODUCT_NOT_FOUND": 404,
    "PRODUCT_INACTIVE": 409,
    "ORDER_NOT_FOUND": 404,
    "ORDER_LIMIT_EXCEEDED": 429,
    "ORDER_HAS_ACTIVE": 409,
    "INVALID_STATE_TRANSITION": 409,
    "INSUFFICIENT_STOCK": 409,
    "PROMO_CODE_INVALID": 422,
    "PROMO_CODE_MIN_AMOUNT": 422,
    "ORDER_OWNERSHIP_VIOLATION": 403,
    "VALIDATION_ERROR": 400,
    "TOKEN_EXPIRED": 401,
    "TOKEN_INVALID": 401,
    "REFRESH_TOKEN_INVALID": 401,
    "ACCESS_DENIED": 403,
}


class APIError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        if code not in STATUS:
            raise ValueError(f"Unknown API error code: {code}")
        self.code = code
        self.message = message
        self.details = details
        self.status = STATUS[code]
        super().__init__(message)


def fail(code: str, message: str, details: dict[str, Any] | None = None):
    raise APIError(code, message, details)
