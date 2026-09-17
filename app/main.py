"""FastAPI application: immutable config versions + batch flag evaluation."""

from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db as db_mod
from .comparison import compare_versions
from .evaluator import FlagNotFound, evaluate_flag
from .schemas import (
    BatchEvalRequest,
    BatchEvalResponse,
    ConfigComparisonRequest,
    ConfigComparisonResponse,
    ConfigResponse,
    PublishConfigRequest,
    PublishResponse,
    VersionInfo,
)
from .validation import ConfigValidationError, validate_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db_mod.connect()
    try:
        db_mod.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Feature Flag Decision Service",
    version="1.0.0",
    description=(
        "Versioned feature-flag evaluation with prerequisites, ordered "
        "attribute rules and stable user_id percentage rollouts."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — uniform envelope: {"error": {code, message, errors?}}
# ---------------------------------------------------------------------------


def _error_response(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return JSONResponse(status_code=status_code, content={"error": detail})


@app.exception_handler(ConfigValidationError)
async def config_validation_handler(request: Request, exc: ConfigValidationError) -> JSONResponse:
    return _error_response(
        422,
        "invalid_config",
        "configuration validation failed",
        errors=exc.errors,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(
        422,
        "invalid_request",
        "request validation failed",
        errors=exc.errors(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------


@app.post("/api/v1/configs", response_model=PublishResponse, status_code=201)
def publish_config(body: PublishConfigRequest) -> Any:
    config = body.model_dump()
    # Semantic validation: cycles, percentage bounds, dangling prerequisites.
    validate_config(config)

    conn = db_mod.connect()
    try:
        created_at = db_mod.insert_version(conn, body.version, config)
        if created_at is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_exists",
                    "message": f"version {body.version!r} already exists; versions are immutable",
                },
            )
        return PublishResponse(
            version=body.version, flag_count=len(body.flags), created_at=created_at
        )
    finally:
        conn.close()


@app.get("/api/v1/configs", response_model=list[VersionInfo])
def list_configs() -> Any:
    conn = db_mod.connect()
    try:
        return [VersionInfo(**row) for row in db_mod.list_versions(conn)]
    finally:
        conn.close()


@app.get("/api/v1/configs/{version}", response_model=ConfigResponse)
def get_config(version: str) -> Any:
    conn = db_mod.connect()
    try:
        row = db_mod.get_version(conn, version)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {version!r} not found",
                },
            )
        return ConfigResponse(**row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Version comparison (impact analysis)
# ---------------------------------------------------------------------------


@app.post("/api/v1/config-comparisons", response_model=ConfigComparisonResponse)
def compare_configs(body: ConfigComparisonRequest) -> Any:
    conn = db_mod.connect()
    try:
        from_row = db_mod.get_version(conn, body.from_version)
        if from_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.from_version!r} not found",
                },
            )
        to_row = db_mod.get_version(conn, body.to_version)
        if to_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.to_version!r} not found",
                },
            )
    finally:
        conn.close()

    analysis = compare_versions(
        from_row["config"].get("flags", {}),
        to_row["config"].get("flags", {}),
    )
    return ConfigComparisonResponse(
        from_version=body.from_version,
        to_version=body.to_version,
        **analysis,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@app.post("/api/v1/evaluate", response_model=BatchEvalResponse)
def batch_evaluate(body: BatchEvalRequest) -> Any:
    conn = db_mod.connect()
    try:
        row = db_mod.get_version(conn, body.version)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.version!r} not found",
                },
            )
    finally:
        conn.close()

    flags = row["config"]["flags"]
    attributes = dict(body.attributes)
    if body.user_id is not None and "user_id" not in attributes:
        attributes["user_id"] = body.user_id

    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for flag_name in body.flags:
        try:
            results.append(
                evaluate_flag(flag_name, flags, attributes=attributes)
            )
        except FlagNotFound:
            missing.append(flag_name)
            results.append(
                {
                    "flag": flag_name,
                    "value": False,
                    "hit": False,
                    "reason": "flag_not_found",
                    "rule_id": None,
                    "rollout": None,
                    "chain": [],
                }
            )

    context = dict(attributes)
    return BatchEvalResponse(
        version=body.version, context=context, results=results, missing_flags=missing
    )


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "feature-flag-decision-service",
        "docs": "/docs",
        "health": "/healthz",
        "endpoints": [
            "POST /api/v1/configs",
            "GET /api/v1/configs",
            "GET /api/v1/configs/{version}",
            "POST /api/v1/config-comparisons",
            "POST /api/v1/evaluate",
        ],
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
