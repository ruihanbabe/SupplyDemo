"""FastAPI assembly: routing, trace correlation and error mapping.

Per ARCHITECTURE.md's API row this layer owns request DTOs, dispatch and error
translation, and nothing else. No arithmetic, no rule evaluation, no orchestration.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import NoResultFound

from api.envelope import envelope, error_envelope
from api.routers import catalog, demands, plans, runs

logger = logging.getLogger("supplyagent.api")

app = FastAPI(
    title="SupplyAgent API",
    version="0.1.0",
    summary="Deterministic procurement core over HTTP",
)


@app.middleware("http")
async def attach_trace_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[JSONResponse]],
) -> JSONResponse:
    """One id per request, echoed in the body and the header for log correlation."""
    trace_id = request.headers.get("x-trace-id") or uuid4().hex
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["x-trace-id"] = trace_id
    return response


def _trace_id(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_envelope(
            "validation_failed",
            "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()),
            trace_id=_trace_id(request),
        ),
    )


@app.exception_handler(NoResultFound)
async def _not_found(request: Request, exc: NoResultFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=error_envelope("not_found", "Resource does not exist",
                               trace_id=_trace_id(request)),
    )


@app.exception_handler(ValueError)
async def _invalid_request(request: Request, exc: ValueError) -> JSONResponse:
    """procurement_core raises ValueError when a business precondition is unmet.

    The message is authored in that layer and is safe to return; it names the rule the
    caller violated instead of leaking storage internals.
    """
    return JSONResponse(
        status_code=400,
        content=error_envelope("invalid_request", str(exc), trace_id=_trace_id(request)),
    )


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    # The detail has to go somewhere: without this the trace id points at nothing.
    logger.exception("Unhandled error on %s %s (trace_id=%s)",
                     request.method, request.url.path, _trace_id(request))
    return JSONResponse(
        status_code=500,
        content=error_envelope("internal_error", "Unexpected server error", retryable=True,
                               trace_id=_trace_id(request)),
    )


@app.get("/health", tags=["ops"])
def health(request: Request) -> dict:
    """Liveness only. It deliberately does not touch the database: a health probe that
    fails on a database blip would report the process as dead when it is merely blocked.
    """
    return envelope({"status": "alive"}, trace_id=_trace_id(request))


app.include_router(catalog.router)
app.include_router(demands.router)
app.include_router(runs.router)
app.include_router(plans.router)
