import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.auth.router import router as auth_router
from app.api.automations import router as automations_router
from app.api.chat.router import router as chat_router
from app.api.health.router import router as health_router
from app.api.integrations.shopify import router as shopify_router
from app.api.knowledge.router import router as knowledge_router
from app.api.organizations.router import router as organizations_router
from app.api.public_chat.router import router as public_chat_router
from app.api.webhooks.shopify import router as shopify_webhooks_router
from app.core.body_limit import RequestBodyLimitMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.public_http import PublicApiMiddleware
from app.core.request_context import RequestIdMiddleware
from app.core.startup_checks import enforce_startup
from app.services.integrations.shopify.factory import check_api_version

logger = logging.getLogger(__name__)

# What the dashboard (a browser) may send to the API. The visitor-facing widget API has its own,
# wider CORS in core.public_http, and the frontend server calls the API server-to-server.
DASHBOARD_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
DASHBOARD_HEADERS = ["Authorization", "Content-Type", "X-Organization-ID", "X-Request-ID"]


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    enforce_startup(settings)  # production: refuse to start with an unsafe configuration
    if settings.shopify_configured:
        check_api_version(settings.shopify_api_version)

    docs = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    # Innermost to outermost (the last one added sees the request first).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=DASHBOARD_METHODS,
        allow_headers=DASHBOARD_HEADERS,
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    # Answers CORS preflights and caps request bodies for the public API (see its docs).
    app.add_middleware(PublicApiMiddleware, max_body_bytes=settings.public_chat_max_body_bytes)
    app.add_middleware(RequestIdMiddleware)

    def dependency_unavailable(request: Request, exc: Exception) -> JSONResponse:
        """The database or Redis is down, slow or out of connections: say so predictably (503 and
        a Retry-After) instead of a 500. Nothing about the cause reaches the client."""
        logger.warning("dependency unavailable type=%s", type(exc).__name__)
        return JSONResponse(
            {
                "detail": "Service temporarily unavailable. Please retry.",
                "request_id": _request_id(request),
            },
            status_code=503,
            headers={"Retry-After": "5"},
        )

    def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """Anything unexpected: a generic body with the request id (the traceback is in the server
        log, under the same id), never exception text."""
        request_id = _request_id(request)
        return JSONResponse(
            {"detail": "Internal server error", "request_id": request_id},
            status_code=500,
            headers={"X-Request-ID": request_id} if request_id else None,
        )

    for exc_type in (OperationalError, InterfaceError, PoolTimeoutError, RedisError):
        app.add_exception_handler(exc_type, dependency_unavailable)
    app.add_exception_handler(Exception, internal_error)

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api")
    app.include_router(automations_router, prefix="/api")
    app.include_router(organizations_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(public_chat_router, prefix="/api")
    app.include_router(shopify_router, prefix="/api")
    app.include_router(shopify_webhooks_router, prefix="/api")
    return app


app = create_app()
