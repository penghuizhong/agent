import logging
import traceback
import warnings
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from langchain_core._api import LangChainBetaWarning
from langfuse import Langfuse

from agents import get_agent, get_all_agent_info, load_agent
from api.exceptions import AppError
from api.middleware.audit_log import AuditLogMiddleware
from api.middleware.request_id import RequestIDMiddleware
from api.rate_limit import check_rate_limit
from api.routers.v1 import agent as agent_v1
from api.routers.v1 import ingestion
from core import settings
from core.postgres import (
    close_async_pool,
    create_admin_pool,
    get_async_pool,
    get_postgres_saver,
    get_postgres_store,
)

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger("uvicorn")


def custom_generate_unique_id(route: APIRoute) -> str:
    return route.name


@asynccontextmanager
async def lifespan(app: FastAPI):
    """资源预热：数据库连接、Agent 插件"""

    try:
        async with (
            get_postgres_saver() as saver,
            get_postgres_store() as store,
            create_admin_pool() as admin_pool,
        ):
            for a in get_all_agent_info():
                try:
                    await load_agent(a.key)
                    loaded_agent = get_agent(a.key)
                    loaded_agent.checkpointer = saver
                    loaded_agent.store = store
                    logger.info("Agent %s 已成功挂载记忆体", a.key)
                except Exception as e:
                    logger.error("Failed to load agent %s: %s", a.key, e)

            app.state.admin_pool = admin_pool

            # 初始化 asyncpg 连接池（用于入库流水线）
            await get_async_pool()

            yield

    except Exception as e:
        logger.error("Error during initialization: %s", e)
        raise

    finally:
        logger.info("🛑 开始优雅关闭...")

        # 关闭 asyncpg pool
        await close_async_pool()

        # 关闭 admin_pool
        if hasattr(app.state, "admin_pool"):
            try:
                await app.state.admin_pool.close()
                logger.info("✅ PostgreSQL admin pool closed")
            except Exception as e:
                logger.error("关闭 admin_pool 失败: %s", e)

        # Flush Langfuse
        if settings.LANGFUSE_TRACING:
            try:
                langfuse = Langfuse()
                langfuse.flush()
                logger.info("✅ Langfuse events flushed")
            except Exception as e:
                logger.error("Langfuse flush 失败: %s", e)

        logger.info("✅ 优雅关闭完成")


# ==============================================================================
# 1. 实例化
# ==============================================================================
docs_url = f"{settings.API_PREFIX_AGENT}/docs" if settings.SHOW_DOCS else None
openapi_url = f"{settings.API_PREFIX_AGENT}/openapi.json" if settings.SHOW_DOCS else None
redoc_url = f"{settings.API_PREFIX_AGENT}/redoc" if settings.SHOW_DOCS else None

app = FastAPI(
    title="方圆智版agent引擎",
    version="1.0.0",
    lifespan=lifespan, 
    generate_unique_id_function=custom_generate_unique_id,
    
    docs_url=docs_url,
    openapi_url=openapi_url,
    redoc_url=redoc_url
)


# ==============================================================================
# 2. 全局异常处理器
# ==============================================================================
@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    """处理应用级异常，返回标准化错误响应。"""
    logger.error(
        "AppError [%s]: %s",
        exc.code,
        exc.message,
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **(exc.details or {}),
            }
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    """兜底处理所有未捕获异常，生产环境不暴露堆栈信息。"""
    logger.error(
        "Unhandled exception: %s\n%s",
        exc,
        traceback.format_exc(),
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
            }
        },
    )


# ==============================================================================
# 3. 中间件
# ==============================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RequestIDMiddleware)

app.add_middleware(AuditLogMiddleware)


# ==============================================================================
# 4. 健康检查（公开，无需鉴权）
# ==============================================================================
@app.get("/v1/agent/health", tags=["System"])
async def health_check():
    health_status: dict = {"status": "ok"}
    has_critical_failure = False

    # PostgreSQL
    try:
        if not hasattr(app.state, "admin_pool"):
            raise RuntimeError("admin_pool not initialized")
        pool = app.state.admin_pool
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        health_status["postgres"] = "connected"
    except Exception as e:
        logger.error("Health check: PostgreSQL error: %s", e)
        health_status["postgres"] = "disconnected"
        has_critical_failure = True

    # Langfuse（非核心，不影响整体状态）
    if settings.LANGFUSE_TRACING:
        try:
            health_status["langfuse"] = (
                "connected" if Langfuse().auth_check() else "disconnected"
            )
        except Exception:
            health_status["langfuse"] = "disconnected"

    # 核心依赖失败时返回 503
    if has_critical_failure:
        health_status["status"] = "degraded"
        return JSONResponse(status_code=503, content=health_status)

    return health_status


# ==============================================================================
# 5. 路由注册
# ==============================================================================
global_dependencies = [Depends(check_rate_limit)]

# v1 路由
app.include_router(agent_v1.router, dependencies=global_dependencies)
app.include_router(ingestion.router, dependencies=global_dependencies)
