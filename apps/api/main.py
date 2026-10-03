"""The API process."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from apps.api.core.config import Settings, get_settings
from apps.api.core.error_handlers import register_error_handlers
from apps.api.core.redis_client import close_client, init_client
from apps.api.core.trace import new_trace_id, set_language_from_header, set_trace_id
from apps.api.db.session import dispose_engine, init_engine
from apps.api.providers.sms import build_sms_provider
from apps.api.routers import auth as auth_routes
from apps.api.routers import reference as reference_routes

logger = logging.getLogger("gebeyalink.api")

API_PREFIX = "/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        app.state.settings = resolved
        app.state.sms_provider = build_sms_provider(resolved.sms_provider)
        init_engine(resolved)
        init_client(resolved)
        logger.info("api ready sms_provider=%s", app.state.sms_provider.name)
        try:
            yield
        finally:
            await dispose_engine()
            await close_client()

    app = FastAPI(
        title="GebeyaLink API",
        version="1.0.1",
        description="Agricultural marketplace for Ethiopian traders.",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(auth_routes.router, prefix=API_PREFIX)
    app.include_router(reference_routes.router, prefix=API_PREFIX)

    @app.middleware("http")
    async def stamp_request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        set_trace_id(trace_id)
        set_language_from_header(request.headers.get("accept-language"))
        response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    return app


app = create_app()
