"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import demo, routes
from app.config import get_settings
from app.services.errors import ErrorCode, ToolError

logger = logging.getLogger("support_agent")

UI_DIR = Path(__file__).parent / "ui"

# Which HTTP status each failure code maps to. Access failures deliberately
# share one shape so an order id cannot be probed by comparing responses.
STATUS_FOR_CODE: dict[str, int] = {
    ErrorCode.NOT_AUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.REVIEWER_ROLE_REQUIRED: 403,
    ErrorCode.ORDER_NOT_OWNED: 404,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.TICKET_NOT_FOUND: 404,
    ErrorCode.RUN_NOT_FOUND: 404,
    ErrorCode.PROPOSAL_NOT_FOUND: 404,
    ErrorCode.APPROVAL_NOT_FOUND: 404,
    ErrorCode.APPROVAL_EXPIRED: 409,
    ErrorCode.APPROVAL_ALREADY_DECIDED: 409,
    ErrorCode.APPROVAL_ALREADY_CONSUMED: 409,
    ErrorCode.APPROVAL_NOT_GRANTED: 409,
    ErrorCode.PROPOSAL_CHANGED: 409,
    ErrorCode.AMOUNT_EXCEEDS_REFUNDABLE: 422,
    ErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH: 409,
    ErrorCode.PROVIDER_TIMEOUT: 504,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.STEP_LIMIT_EXCEEDED: 429,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "starting %s (db=%s, model_mode=%s)",
        settings.app_name,
        "postgres" if settings.is_postgres else "sqlite",
        "offline" if not settings.anthropic_api_key else settings.model_name,
    )
    # Startup recovery: anything left in flight by a previous process is
    # reconciled before the first new request is served.
    try:
        from app.workflows.runner import recover_unfinished

        summary = recover_unfinished()
        if summary["reconciled_operations"] or summary["resumed_runs"]:
            logger.warning("startup recovery: %s", summary)
    except Exception:  # noqa: BLE001 - never block startup on recovery
        logger.exception("startup recovery failed")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "An agentic support workflow that investigates a complaint, retrieves "
            "policy, proposes an action, requires human approval for refunds and "
            "executes them idempotently."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(ToolError)
    async def tool_error_handler(_request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(
            status_code=STATUS_FOR_CODE.get(exc.error_code, 400), content=exc.as_dict()
        )

    app.include_router(routes.router)
    app.include_router(demo.router)

    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def dashboard() -> Any:
            return FileResponse(str(UI_DIR / "index.html"))

    return app


app = create_app()
