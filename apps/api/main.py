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
from apps.api.core.response_headers import SECURITY_HEADERS
from apps.api.core.trace import new_trace_id, set_language_from_header, set_trace_id
from apps.api.db.session import dispose_engine, init_engine
from apps.api.providers.sms import build_sms_provider
from apps.api.routers import auth as auth_routes
from apps.api.routers import health as health_routes
from apps.api.routers import listing as listing_routes
from apps.api.routers import offer as offer_routes
from apps.api.routers import order as order_routes
from apps.api.routers import profile as profile_routes
from apps.api.routers import reference as reference_routes
from apps.api.routers import search as search_routes

logger = logging.getLogger("gebeyalink.api")

API_PREFIX = "/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    # Resolved once, here, rather than inside the lifespan, because the route table has to be
    # fixed now: whether /docs, /redoc and /openapi.json exist at all is a decision made while
    # the application object is built, and a decision made later is a route table that lies
    # about itself.
    resolved = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        # None is how FastAPI is told not to serve a route at all, so one flag decides all
        # three. The description of this API is not a secret - it is what clients are written
        # against, and the specification asks for it to live in the repository as
        # docs/openapi.yaml - but the route that serves it is an invitation to anyone who can
        # reach the port, and nothing has asked for that invitation. Set EXPOSE_DOCS to turn
        # it on; the security scan does, because it reads this description to learn the API.
        docs_url="/docs" if resolved.expose_docs else None,
        redoc_url="/redoc" if resolved.expose_docs else None,
        openapi_url="/openapi.json" if resolved.expose_docs else None,
    )
    register_error_handlers(app)
    # Outside the versioned prefix: the container healthcheck and the security scanner both
    # address /health by a fixed path, and neither should have to know how this API is
    # versioned to ask whether the process is up.
    app.include_router(health_routes.router)
    app.include_router(auth_routes.router, prefix=API_PREFIX)
    app.include_router(reference_routes.router, prefix=API_PREFIX)
    app.include_router(profile_routes.router, prefix=API_PREFIX)
    app.include_router(listing_routes.router, prefix=API_PREFIX)
    app.include_router(offer_routes.router, prefix=API_PREFIX)
    app.include_router(order_routes.router, prefix=API_PREFIX)
    app.include_router(search_routes.router, prefix=API_PREFIX)

    @app.middleware("http")
    async def stamp_request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        set_trace_id(trace_id)
        set_language_from_header(request.headers.get("accept-language"))
        response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        # On every response, and here rather than on the handlers: a header that only appears
        # on the responses somebody remembered to add it to is not a policy. The one response
        # this middleware cannot reach - the 500 from an unhandled exception - is stamped in
        # `error_response`, which is why the policy lives in one module both of them read.
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    return app


# There is deliberately no `app = create_app()` here. Building the application reads the
# configuration, and a module that required configuration at import time could not be
# imported by anything that has not configured the process yet - a test that only wants to
# look at a route table, or a tool that wants to read one setting. Uvicorn is pointed at
# `apps.api.main:create_app --factory` instead, which is the same object built after the
# environment is in place.
