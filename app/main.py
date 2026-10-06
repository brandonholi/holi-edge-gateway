import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .observability.logger import setup_logging
from .clients.redis_client import init_redis_pools, close_redis_pools, get_cache_redis, get_state_redis
from .core.errors import instalar_manejadores
from .routers import catalog, search, cart, checkout, auth, master_data, perfil
from .schemas.common import RESPUESTAS_ERROR

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_redis_pools()
    yield
    # Shutdown
    await close_redis_pools()


app = FastAPI(
    title="HOLI Supermercado Edge Gateway",
    description="Edge Gateway providing high-performance read APIs from Redis and secure checkout to Odoo 18.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_and_timing_middleware(request: Request, call_next):
    """Injects X-Request-Id and logs request duration."""
    req_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    # Routes and error handlers read it from here (core/deps.RequestId).
    request.state.request_id = req_id
    start_time = time.time()

    response = await call_next(request)

    duration_ms = (time.time() - start_time) * 1000.0
    response.headers["X-Request-Id"] = req_id
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
    return response


# RFC 7807 problem documents for every failure path (contrato-api-v1.md, section 4)
instalar_manejadores(app)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check validating Redis cache and state connectivity."""
    cache_ok = False
    state_ok = False
    try:
        cache_redis = get_cache_redis()
        cache_ok = await cache_redis.ping()
    except Exception:
        pass

    try:
        state_redis = get_state_redis()
        state_ok = await state_redis.ping()
    except Exception:
        pass

    status = "healthy" if (cache_ok and state_ok) else "degraded"
    return {
        "status": status,
        "redis_cache": cache_ok,
        "redis_state": state_ok,
    }


# Include Routers. Every public route documents the shared error format.
for modulo in (catalog, search, cart, checkout, auth, master_data, perfil):
    app.include_router(modulo.router, responses=RESPUESTAS_ERROR)
