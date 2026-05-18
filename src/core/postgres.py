import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.config import settings

logger = logging.getLogger(__name__)


def validate_postgres_config() -> None:
    """
    验证所有必需的PostgreSQL配置是否存在。
    如果缺少任何必需的配置，则引发ValueError。
    """
    required_vars = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ]

    missing = [var for var in required_vars if not getattr(settings, var, None)]
    if missing:
        raise ValueError(
            f"缺少必需的PostgreSQL配置: {', '.join(missing)}。 "
            "必须设置这些环境变量才能使用PostgreSQL持久化。"
        )

    if settings.POSTGRES_MIN_CONNECTIONS_PER_POOL > settings.POSTGRES_MAX_CONNECTIONS_PER_POOL:
        raise ValueError(
            f"POSTGRES_MIN_CONNECTIONS_PER_POOL ({settings.POSTGRES_MIN_CONNECTIONS_PER_POOL}) 必须小于或等于 POSTGRES_MAX_CONNECTIONS_PER_POOL ({settings.POSTGRES_MAX_CONNECTIONS_PER_POOL})"
        )


def get_postgres_connection_string() -> str:
    """从设置中构建并返回PostgreSQL连接字符串。"""
    if settings.POSTGRES_PASSWORD is None:
        raise ValueError("POSTGRES_PASSWORD 未设置")
    return (
        f"postgresql://{settings.POSTGRES_USER}:"
        f"{settings.POSTGRES_PASSWORD.get_secret_value()}@"
        f"{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/"
        f"{settings.POSTGRES_DB}"
    )


@asynccontextmanager
async def get_postgres_saver():
    """
    初始化并返回基于连接池的PostgreSQL保存器实例，以实现更弹性的连接。

    返回:
        AsyncPostgresSaver: 用于保存检查点的PostgreSQL保存器实例

    功能:
        1. 验证PostgreSQL配置
        2. 创建连接池
        3. 初始化AsyncPostgresSaver
        4. 设置检查点表
    """
    validate_postgres_config()
    application_name = settings.POSTGRES_APPLICATION_NAME + "-" + "saver"

    async with AsyncConnectionPool(
        conninfo=get_postgres_connection_string(),
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        timeout=settings.POSTGRES_POOL_OPEN_TIMEOUT,
        max_idle=settings.POSTGRES_POOL_MAX_IDLE_TIME,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "application_name": application_name,
            "connect_timeout": 10,
        },
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        yield checkpointer


@asynccontextmanager
async def get_postgres_store():
    """
    获取基于连接池的PostgreSQL存储实例，以实现更弹性的连接。

    返回:
        AsyncPostgresStore: 可用于异步上下文管理器模式的PostgreSQL存储实例

    功能:
        1. 验证PostgreSQL配置
        2. 创建连接池
        3. 初始化AsyncPostgresStore
        4. 设置存储表
    """
    validate_postgres_config()
    application_name = settings.POSTGRES_APPLICATION_NAME + "-" + "saver"

    async with AsyncConnectionPool(
        conninfo=get_postgres_connection_string(),
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        timeout=settings.POSTGRES_POOL_OPEN_TIMEOUT,
        max_idle=settings.POSTGRES_POOL_MAX_IDLE_TIME,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "application_name": application_name,
            "connect_timeout": 10,
        },
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        store = AsyncPostgresStore(pool)
        await store.setup()
        yield store
    
@asynccontextmanager
async def create_admin_pool():
    """
    为 vector_admin 创建独立的长生命周期连接池。
    与 saver/store 的池隔离，避免互相争抢连接。
    """
    validate_postgres_config()
    base_app_name = settings.POSTGRES_APPLICATION_NAME or "agent_service"

    async with AsyncConnectionPool(
        conninfo=get_postgres_connection_string(),
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        timeout=settings.POSTGRES_POOL_OPEN_TIMEOUT,
        max_idle=settings.POSTGRES_POOL_MAX_IDLE_TIME,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "application_name": f"{base_app_name}-vector_admin",
            "connect_timeout": 10,
        },
    ) as pool:
        yield pool


_async_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None


async def get_async_pool() -> asyncpg.Pool:
    """获取全局 asyncpg 连接池（用于入库流水线）"""
    global _async_pool, _pool_lock
    if _async_pool is None:
        if _pool_lock is None:
            _pool_lock = asyncio.Lock()
        async with _pool_lock:
            if _async_pool is None:
                conn_string = get_postgres_connection_string()
                _async_pool = await asyncpg.create_pool(
                    dsn=conn_string,
                    min_size=2,
                    max_size=10,
                    command_timeout=60,
                )
                logger.info("asyncpg pool initialized")
    return _async_pool


async def close_async_pool() -> None:
    """关闭全局 asyncpg 连接池"""
    global _async_pool
    if _async_pool is not None:
        await _async_pool.close()
        _async_pool = None
        logger.info("asyncpg pool closed")
 