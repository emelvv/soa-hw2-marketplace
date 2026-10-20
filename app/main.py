"""FastAPI application assembled from the authored OpenAPI contract."""

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth import bootstrap_admin
from app.errors import APIError
from generated.routes import router


ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap_admin()
    yield


app = FastAPI(title="Marketplace API", lifespan=lifespan)
app.include_router(router)


def contract_openapi():
    if app.openapi_schema is None:
        app.openapi_schema = yaml.safe_load((ROOT / "openapi" / "openapi.yaml").read_text(encoding="utf-8"))
    return app.openapi_schema


app.openapi = contract_openapi


@app.exception_handler(APIError)
async def api_error(_request: Request, exc: APIError):
    return JSONResponse(status_code=exc.status, content={"error_code": exc.code, "message": exc.message, "details": exc.details})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    violations = [
        {"field": ".".join(str(part) for part in error["loc"]), "violation": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=400, content={"error_code": "VALIDATION_ERROR", "message": "Invalid request", "details": {"violations": violations}})


SENSITIVE_KEYS = {"password", "refresh_token", "access_token", "token"}


def redact(value):
    if isinstance(value, dict):
        return {key: "***" if key.lower() in SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@app.middleware("http")
async def log_api_request(request: Request, call_next):
    request_id = str(uuid4())
    request.state.user_id = None
    started = time.perf_counter()
    body = None
    if request.method in {"POST", "PUT", "DELETE"}:
        raw = await request.body()
        try:
            body = redact(json.loads(raw)) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = "<non-json body>"
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        event = {
            "request_id": request_id,
            "method": request.method,
            "endpoint": request.url.path,
            "status_code": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "user_id": request.state.user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if request.method in {"POST", "PUT", "DELETE"}:
            event["request_body"] = body
        print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
