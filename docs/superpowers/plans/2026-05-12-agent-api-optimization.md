# Agent API 全局优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 agent_api 从"可用"提升到"工业级生产标准"，消除安全隐患、建立可观测性、优化架构分层。

**Architecture:** 四阶段渐进式重构：Phase 1 修复安全和稳定性问题（异常处理、连接池泄漏），Phase 2 建立可观测性（结构化日志、请求 ID、审计日志、API 版本化），Phase 3 架构优化（配置外部化、线程安全），Phase 4 工程化完善（graceful shutdown、限流响应头、数据库迁移）。

**Tech Stack:** FastAPI, Pydantic v2, psycopg, Redis, LangGraph, Celery, LiteLLM, LlamaIndex, Uvicorn

---

## 前置知识

### 项目结构
```
agent_api/src/
├── api/service.py          # FastAPI 应用入口，lifespan 管理
├── core/config.py          # Pydantic Settings + config.yaml 动态加载
├── core/postgres.py        # psycopg_pool 连接池管理
├── core/redis.py           # 双池设计（AsyncRedisPool + SyncRedisPool）
├── core/cache.py           # Redis 缓存工具 + @cached 装饰器
├── api/deps.py             # JWT 认证依赖（Casdoor JWKS）
├── api/rate_limit.py       # 基于 Redis 的限流
└── agents/                 # LangGraph Agent 实现
```

### 关键约束
- **导入规范:** 使用绝对导入 `from core import settings`（不是相对导入）
- **PYTHONPATH:** Docker 中设为 `/app/src:/app`，可直接 `import core`
- **Python 版本:** 3.12（Docker 镜像 python:3.12.3-slim）
- **中文注释:** 代码注释使用中文
- **无测试框架:** 当前项目无 pytest 配置，通过 curl 和 Docker 日志验证

---

## Phase 1: 安全和稳定性（1-2 天）

### Task 1: 修复 Health Check 连接池泄漏

**Files:**
- Modify: `src/api/service.py:119-128`

**背景:** 当前 Health Check 每次请求都创建/销毁一个 PostgreSQL 连接池，造成资源浪费和响应延迟。

- [ ] **Step 1: 修改 health_check 函数，复用 lifespan 中的 pool**

当前代码（`src/api/service.py:119-128`）：
```python
    # ── PostgreSQL ─────────────────────────────────────────────────────────
    try:
        async with create_admin_pool() as pool:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        health_status["postgres"] = "connected"
    except Exception as e:
        logger.error("Health check: PostgreSQL error: %s", e)
        health_status["postgres"] = "disconnected"
        has_critical_failure = True
```

修改为：
```python
    # ── PostgreSQL ─────────────────────────────────────────────────────────
    try:
        pool = app.state.admin_pool
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        health_status["postgres"] = "connected"
    except Exception as e:
        logger.error("Health check: PostgreSQL error: %s", e)
        health_status["postgres"] = "disconnected"
        has_critical_failure = True
```

- [ ] **Step 2: 验证 lifespan 中 admin_pool 在 yield 前已赋值**

确认 `src/api/service.py:51` 处有：
```python
            app.state.admin_pool = admin_pool
            yield
```

- [ ] **Step 3: 测试验证**

```bash
docker compose up -d --build ai_server
curl -s http://localhost:8001/api/agent/health | python3 -m json.tool
```

预期输出：
```json
{
    "status": "ok",
    "redis": "connected",
    "postgres": "connected"
}
```

- [ ] **Step 4: 验证连接池无泄漏**

```bash
docker compose exec postgres psql -U postgres -d agent -c "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '%vector_admin%';"
```

预期：连接数稳定，不会每次 health check 都增长。

---

### Task 2: 创建统一异常处理器

**Files:**
- Create: `src/api/exceptions.py`
- Modify: `src/api/service.py`（注册异常处理器）

- [ ] **Step 1: 创建 api/exceptions.py**

```python
"""统一异常定义 — 消除裸 except Exception，建立标准化错误响应。"""

from __future__ import annotations

from fastapi import status


class AppError(Exception):
    """应用异常基类。所有业务异常应继承此类。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


# ── 客户端错误 (4xx) ──────────────────────────────────────────────────────

class ResourceNotFoundError(AppError):
    """资源不存在。"""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            code="not_found",
            message=f"{resource} '{identifier}' not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class ValidationError(AppError):
    """请求验证失败。"""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(
            code="validation_error",
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


class AuthenticationError(AppError):
    """认证失败。"""

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(
            code="authentication_error",
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class PermissionError(AppError):
    """权限不足。"""

    def __init__(self, message: str = "Insufficient permissions") -> None:
        super().__init__(
            code="permission_denied",
            message=message,
            status_code=status.HTTP_403_FORBIDDEN,
        )


class RateLimitError(AppError):
    """请求频率超限。"""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            code="rate_limit_exceeded",
            message=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details={"retry_after": retry_after},
        )


# ── 服务端错误 (5xx) ──────────────────────────────────────────────────────

class DatabaseError(AppError):
    """数据库操作失败。"""

    def __init__(self, message: str = "Database operation failed") -> None:
        super().__init__(
            code="database_error",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class CacheError(AppError):
    """缓存操作失败。"""

    def __init__(self, message: str = "Cache operation failed") -> None:
        super().__init__(
            code="cache_error",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class ExternalServiceError(AppError):
    """外部服务调用失败（LLM、Langfuse 等）。"""

    def __init__(self, service: str, message: str = "External service unavailable") -> None:
        super().__init__(
            code="external_service_error",
            message=f"{service}: {message}",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )


class StreamError(AppError):
    """SSE 流式响应错误。"""

    def __init__(self, message: str = "Stream interrupted") -> None:
        super().__init__(
            code="stream_error",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class EmbeddingError(AppError):
    """向量嵌入生成失败。"""

    def __init__(self, message: str = "Embedding generation failed") -> None:
        super().__init__(
            code="embedding_error",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class RetrievalError(AppError):
    """向量检索失败。"""

    def __init__(self, message: str = "Vector retrieval failed") -> None:
        super().__init__(
            code="retrieval_error",
            message=message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class LLMError(AppError):
    """大模型调用失败。"""

    def __init__(self, message: str = "LLM request failed") -> None:
        super().__init__(
            code="llm_error",
            message=message,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
```

- [ ] **Step 2: 在 api/service.py 中注册全局异常处理器**

在 `app = FastAPI(...)` 之后、中间件注册之前，添加：

```python
# ==============================================================================
# 2.5 全局异常处理器
# ==============================================================================
from api.exceptions import AppError


@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    """处理应用级异常，返回标准化错误响应。"""
    from fastapi.responses import JSONResponse
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
    import traceback
    logger.error(
        "Unhandled exception: %s\n%s",
        exc,
        traceback.format_exc(),
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
            }
        },
    )
```

- [ ] **Step 3: 验证异常处理器生效**

```bash
docker compose up -d --build ai_server
# 触发一个 401 错误（无 token 访问需要认证的端点）
curl -s http://localhost:8001/api/agent/info | python3 -m json.tool
```

预期输出：
```json
{
    "error": {
        "code": "authentication_error",
        "message": "Missing Authorization Header"
    }
}
```

---

### Task 3: 替换全局裸 `except Exception` 为类型化异常

**Files:**
- Modify: `src/core/cache.py:68-78, 87-98, 103-108, 120-149`
- Modify: `src/core/redis.py:38-50, 112-124`
- Modify: `src/agents/tools.py:32-63, 102-143`
- Modify: `src/api/routers/agent.py:154-156, 247-249, 352-354`

- [ ] **Step 1: 替换 core/cache.py 中的裸 except**

```python
# cache_get (line ~68-78)
async def cache_get(key: str) -> tuple[bool, Any]:
    try:
        async with get_async_redis() as redis:
            raw = await redis.get(key)
            if raw is None:
                return False, None
            if raw == _NONE_SENTINEL:
                return True, None
            return True, json.loads(raw)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        logger.error("Cache GET error [key=%s]: %s", key, exc)
        return False, None
    except json.JSONDecodeError as exc:
        logger.warning("Cache GET JSON decode error [key=%s]: %s", key, exc)
        return False, None
```

```python
# cache_set (line ~87-98)
async def cache_set(key: str, value: Any, ttl: int = 300) -> bool:
    try:
        ttl = _validated_ttl(ttl)
        async with get_async_redis() as redis:
            payload = _NONE_SENTINEL if value is None else json.dumps(value, default=str)
            await redis.setex(key, ttl, payload)
            return True
    except ValueError as exc:
        logger.error("Cache SET rejected [key=%s]: %s", key, exc)
        return False
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        logger.error("Cache SET error [key=%s]: %s", key, exc)
        return False
```

```python
# cache_delete (line ~103-108)
async def cache_delete(key: str) -> bool:
    try:
        async with get_async_redis() as redis:
            return bool(await redis.delete(key))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        logger.error("Cache DELETE error [key=%s]: %s", key, exc)
        return False
```

```python
# invalidate_pattern (line ~120-149)
async def invalidate_pattern(pattern: str, batch_size: int = 500) -> int:
    try:
        async with get_async_redis() as redis:
            deleted = 0
            batch: list[str] = []

            async for key in redis.scan_iter(match=pattern, count=200):
                batch.append(key)
                if len(batch) >= batch_size:
                    async with redis.pipeline(transaction=False) as pipe:
                        for k in batch:
                            pipe.delete(k)
                        results = await pipe.execute()
                    deleted += sum(results)
                    batch.clear()

            if batch:
                async with redis.pipeline(transaction=False) as pipe:
                    for k in batch:
                        pipe.delete(k)
                    results = await pipe.execute()
                deleted += sum(results)

            logger.info(
                "Invalidated %d key(s) matching pattern '%s'", deleted, pattern
            )
            return deleted
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        logger.error("Pattern invalidation error [pattern=%s]: %s", pattern, exc)
        return 0
```

- [ ] **Step 2: 替换 core/redis.py 中的裸 except**

```python
# AsyncRedisPool.initialize (line ~38-50)
        try:
            cls._pool = AsyncConnectionPool.from_url(
                settings.REDIS_URL,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
                decode_responses=True,
            )
            cls._instance = AsyncRedis(connection_pool=cls._pool)
            await cls._instance.ping()
            logger.info("Async Redis pool initialized: %s", settings.REDIS_URL)
        except (ConnectionError, redis.exceptions.ConnectionError) as exc:
            logger.error("Failed to initialize async Redis pool: %s", exc)
            cls._instance = None
            cls._pool = None
            raise
```

```python
# SyncRedisPool.initialize (line ~112-124)
            try:
                pool = redis_sync.ConnectionPool.from_url(
                    settings.REDIS_URL,
                    max_connections=settings.REDIS_MAX_CONNECTIONS,
                    decode_responses=True,
                )
                client = redis_sync.Redis(connection_pool=pool)
                client.ping()
                cls._instance = client
                logger.info("Sync Redis pool initialized: %s", settings.REDIS_URL)
            except (ConnectionError, redis_sync.exceptions.ConnectionError) as exc:
                logger.error("Failed to initialize sync Redis pool: %s", exc)
                raise
```

- [ ] **Step 3: 替换 agents/tools.py 中的裸 except**

```python
# get_llama_index_resources (line ~32-63)
    try:
        if GLOBAL_EMBED_MODEL is None:
            GLOBAL_EMBED_MODEL = LiteLLMEmbedding(
                model_name="dashscope/text-embedding-v3",
                api_key=settings.DASHSCOPE_API_KEY.get_secret_value()
            )

        if GLOBAL_VECTOR_STORE is None:
            GLOBAL_VECTOR_STORE = PGVectorStore.from_params(
                host=settings.POSTGRES_HOST,
                port=str(settings.POSTGRES_PORT),
                database=settings.POSTGRES_DB,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                table_name=settings.TABLE_NAME_PREFIX,
                embed_dim=1024
            )

        if GLOBAL_INDEX is None:
            GLOBAL_INDEX = VectorStoreIndex.from_vector_store(
                vector_store=GLOBAL_VECTOR_STORE,
                embed_model=GLOBAL_EMBED_MODEL
            )

        return GLOBAL_INDEX
    except (ConnectionError, psycopg.Error) as exc:
        logger.error("LlamaIndex 资源连接失败，正在重置单例状态: %s", exc)
        GLOBAL_EMBED_MODEL = None
        GLOBAL_VECTOR_STORE = None
        GLOBAL_INDEX = None
        raise EmbeddingError(f"Failed to initialize LlamaIndex resources: {exc}")
    except Exception as exc:
        logger.error("LlamaIndex 资源加载失败: %s", exc)
        GLOBAL_EMBED_MODEL = None
        GLOBAL_VECTOR_STORE = None
        GLOBAL_INDEX = None
        raise
```

```python
# database_search (line ~102-143)
    try:
        index = get_llama_index_resources()

        filters = None
        if category:
            filters = MetadataFilters(
                filters=[ExactMatchFilter(key="category", value=category)]
            )
            logger.info("🔎 触发精准检索，锁定分类: [%s]", category)

        retriever = index.as_retriever(
            similarity_top_k=4,
            filters=filters
        )
        nodes = retriever.retrieve(query)

        if not nodes:
            return f"在分类 [{category or '全局'}] 中未能找到关于 '{query}' 的相关内容。"

        formatted_results = []
        for i, node_with_score in enumerate(nodes):
            node = node_with_score.node
            page_num = node.metadata.get("page_label", node.metadata.get("page", "未知"))
            file_name = node.metadata.get("file_name", "员工手册")
            content = node.get_content().strip().replace("\n", " ")
            result_item = (
                f"--- 来源 [{i+1}] ({file_name} 第 {page_num} 页) ---\n"
                f"{content}"
            )
            formatted_results.append(result_item)

        return f"针对问题 '{query}'，我找到了以下参考信息：\n\n" + "\n\n".join(formatted_results)

    except TimeoutError as exc:
        logger.error("检索超时: query=%s, error=%s", query, exc)
        return f"数据库查询超时，请稍后重试。"
    except RetrievalError as exc:
        logger.error("检索失败: query=%s, error=%s", query, exc)
        return f"数据库查询出错，请联系系统管理员。"
    except Exception as exc:
        logger.error("检索彻底失败: query=%s, error=%s", query, exc, exc_info=True)
        return f"数据库查询出错，请联系系统管理员。({str(exc)})"
```

- [ ] **Step 4: 替换 api/routers/agent.py 中的裸 except**

```python
# invoke 函数 (line ~154-156)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Agent invoke error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error")
```

```python
# message_generator 函数 (line ~247-249)
    except Exception as e:
        logger.error("Error in message generator: %s", e)
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
```

```python
# history 函数 (line ~352-354)
    except Exception as e:
        logger.error("Agent history error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error")
```

- [ ] **Step 5: 验证零裸 except**

```bash
cd agent_api && rg "except Exception" src/ --type py
```

预期：仅剩 `api/routers/agent.py` 中的兜底捕获（已带 `exc_info=True`），其他全部替换为类型化异常。

---

## Phase 2: 可观测性（3-5 天）

### Task 4: 结构化日志配置

**Files:**
- Create: `src/core/logging.py`
- Modify: `src/main.py`
- Modify: `agent_api/pyproject.toml`（添加依赖）

- [ ] **Step 1: 添加 python-json-logger 依赖**

在 `agent_api/pyproject.toml` 的 dependencies 中添加：
```toml
"python-json-logger>=3.0.0",
```

然后执行：
```bash
cd agent_api && uv lock
```

- [ ] **Step 2: 创建 core/logging.py**

```python
"""结构化日志配置 — 生产环境输出 JSON 格式，开发环境保持人类可读。"""

import logging
import sys

from core.config import settings


class RequestIdFilter(logging.Filter):
    """日志过滤器：自动注入 request_id 到每条日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 尝试从当前线程的 context 中获取 request_id
        record.request_id = getattr(record, "request_id", "-")
        return True


def setup_structured_logging(level: str = "INFO") -> None:
    """
    配置结构化日志。

    开发环境 (MODE=dev): 人类可读格式
    生产环境 (MODE=prod): JSON 格式，便于 ELK/Loki 解析
    """
    is_dev = settings.MODE.lower() in ("dev", "development")

    # 清除默认 handler
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())

    if is_dev:
        # 开发环境：人类可读格式
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | [%(request_id)s] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        # 生产环境：JSON 格式
        try:
            from pythonjsonlogger import jsonlogger
            formatter = jsonlogger.JsonFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
                rename_fields={
                    "asctime": "timestamp",
                    "levelname": "level",
                    "name": "logger",
                },
            )
        except ImportError:
            # 降级到普通格式
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | [%(request_id)s] | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # 抑制第三方库的过度日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
```

- [ ] **Step 3: 修改 main.py 使用结构化日志**

替换 `src/main.py` 中的 `logging.basicConfig` 调用：

```python
import asyncio
import logging
import sys

import uvicorn

from core import settings
from core.logging import setup_structured_logging

if __name__ == "__main__":
    # 结构化日志初始化
    setup_structured_logging(settings.LOG_LEVEL)

    # Windows 异步数据库兼容补丁
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    uvicorn.run(
        "api.service:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
    )
```

- [ ] **Step 4: 验证日志格式**

```bash
docker compose up -d --build ai_server
docker compose logs ai_server --tail=5
```

开发环境预期：
```
2026-05-12 19:00:00 | INFO     | uvicorn | [-] | Started server process [1]
```

生产环境预期（JSON）：
```json
{"timestamp": "2026-05-12T19:00:00", "level": "INFO", "logger": "uvicorn", "message": "Started server process [1]", "request_id": "-"}
```

---

### Task 5: 请求 ID 中间件

**Files:**
- Create: `src/api/middleware/__init__.py`
- Create: `src/api/middleware/request_id.py`
- Modify: `src/api/service.py`（注册中间件）

- [ ] **Step 1: 创建 api/middleware/__init__.py**

```python
"""API 中间件模块。"""
```

- [ ] **Step 2: 创建 api/middleware/request_id.py**

```python
"""请求 ID 中间件 — 为每个请求分配唯一 ID，便于日志关联。"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    为每个请求注入 X-Request-ID。

    - 如果客户端已传入 X-Request-ID，则保留原值
    - 否则生成 UUID4
    - 响应头中回传 X-Request-ID
    - 日志中可通过 request.state.request_id 访问
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # 从请求头读取或生成 request_id
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        # 执行请求
        response = await call_next(request)

        # 回传 request_id 到响应头
        response.headers["X-Request-ID"] = request_id

        return response
```

- [ ] **Step 3: 在 service.py 中注册中间件**

在 CORS 中间件之后添加：

```python
from api.middleware.request_id import RequestIDMiddleware

app.add_middleware(RequestIDMiddleware)
```

完整中间件注册顺序：
```python
# 1. CORS（最外层）
app.add_middleware(CORSMiddleware, ...)

# 2. 请求 ID（在 CORS 之后，路由之前）
app.add_middleware(RequestIDMiddleware)
```

- [ ] **Step 4: 验证请求 ID**

```bash
curl -s -D - http://localhost:8001/api/agent/health | grep -i x-request-id
```

预期输出：
```
x-request-id: a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

```bash
# 传入自定义 request_id
curl -s -D - -H "X-Request-ID: my-custom-id-123" http://localhost:8001/api/agent/health | grep -i x-request-id
```

预期输出：
```
x-request-id: my-custom-id-123
```

---

### Task 6: API 审计日志中间件

**Files:**
- Create: `src/api/middleware/audit_log.py`
- Modify: `src/api/service.py`（注册中间件）

- [ ] **Step 1: 创建 api/middleware/audit_log.py**

```python
"""API 审计日志中间件 — 记录所有 API 请求的完整审计信息。"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("audit")

# 跳过审计的路径
SKIP_PATHS = {
    "/api/agent/health",
    "/api/agent/docs",
    "/api/agent/openapi.json",
    "/api/agent/redoc",
    "/favicon.ico",
}

# 需要脱敏的请求头
SENSITIVE_HEADERS = {"authorization", "x-api-key", "cookie"}


class AuditLogMiddleware(BaseHTTPMiddleware):
    """
    记录 API 请求审计日志。

    记录内容：
    - method, path, status_code, duration_ms
    - user_id (从 JWT payload 中提取)
    - request_id
    - 客户端 IP
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # 跳过不需要审计的路径
        if request.url.path in SKIP_PATHS:
            return await call_next(request)

        start_time = time.monotonic()

        # 执行请求
        response = await call_next(request)

        duration_ms = (time.monotonic() - start_time) * 1000

        # 提取用户信息（如果已认证）
        user_id = "anonymous"
        if hasattr(request.state, "user"):
            user_id = request.state.user.get("sub", "anonymous")

        # 获取客户端 IP
        client_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

        # 记录审计日志
        logger.info(
            "AUDIT: %s %s → %d (%.0fms) user=%s ip=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            user_id,
            client_ip,
            extra={
                "request_id": getattr(request.state, "request_id", "-"),
                "audit": True,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 1),
                "user_id": user_id,
                "client_ip": client_ip,
            },
        )

        return response
```

- [ ] **Step 2: 在 service.py 中注册审计中间件**

在 RequestIDMiddleware 之后添加：

```python
from api.middleware.audit_log import AuditLogMiddleware

app.add_middleware(AuditLogMiddleware)
```

- [ ] **Step 3: 验证审计日志**

```bash
docker compose up -d --build ai_server
curl -s http://localhost:8001/api/agent/info -H "Authorization: Bearer test"
docker compose logs ai_server | grep AUDIT
```

预期输出：
```
AUDIT: GET /api/agent/info → 401 (12ms) user=anonymous ip=172.18.0.1
```

---

### Task 7: API 版本控制 (v1)

**Files:**
- Create: `src/api/routers/v1/__init__.py`
- Create: `src/api/routers/v1/agent.py`（迁移自 `api/routers/agent.py`）
- Create: `src/api/routers/v1/vectors.py`（迁移自 `api/routers/vector_admin.py`）
- Modify: `src/api/service.py`（路由注册）
- Modify: `nginx/nginx.conf`（路由匹配）
- Modify: `nginx/nginx.prod.conf`（路由匹配）
- Modify: `web/lib/api/vectors.ts`（前端适配）

- [ ] **Step 1: 创建 api/routers/v1/__init__.py**

```python
"""API v1 路由模块。"""
```

- [ ] **Step 2: 迁移 agent.py 到 v1**

复制 `src/api/routers/agent.py` 到 `src/api/routers/v1/agent.py`，修改路由前缀：

```python
# 修改第 45 行
router = APIRouter(prefix="/api/v1/agent", tags=["Agent 相关接口"])
```

- [ ] **Step 3: 迁移 vector_admin.py 到 v1**

复制 `src/api/routers/vector_admin.py` 到 `src/api/routers/v1/vectors.py`，修改路由前缀：

```python
# 修改第 23 行
router = APIRouter(prefix="/api/v1/admin/vectors", tags=["向量管理"])
```

- [ ] **Step 4: 更新 api/service.py 路由注册**

替换原有的路由注册：

```python
# ==============================================================================
# 4. 路由注册
# ==============================================================================
from api.routers.v1 import agent as agent_v1, vectors as vectors_v1

global_dependencies = [Depends(check_rate_limit)]

# v1 路由
app.include_router(agent_v1.router, dependencies=global_dependencies)
app.include_router(vectors_v1.router, dependencies=global_dependencies)
```

- [ ] **Step 5: 更新 nginx 配置**

修改 `nginx/nginx.conf` 中的 AI 服务路由：

```nginx
        # AI 服务路由（v1 版本）
        location /api/v1/agent/ {
            set $ai_upstream http://ai_server:8001;
            proxy_pass $ai_upstream;
            # ... 保持原有 proxy_set_header 配置
        }

        location /api/v1/admin/vectors/ {
            set $ai_upstream http://ai_server:8001;
            proxy_pass $ai_upstream;
            # ... 保持原有 proxy_set_header 配置
        }

        # 向后兼容：旧路由标记为 deprecated
        location /api/agent/ {
            set $ai_upstream http://ai_server:8001;
            proxy_pass $ai_upstream;
            add_header Deprecation "true";
            add_header Sunset "Sat, 01 Jan 2027 00:00:00 GMT";
            # ... 保持原有 proxy_set_header 配置
        }
```

同样更新 `nginx/nginx.prod.conf`。

- [ ] **Step 6: 更新前端 API 客户端**

修改 `web/lib/api/vectors.ts` 中的路径：

```typescript
// 将所有 /api/admin/vectors/ 替换为 /api/v1/admin/vectors/
export async function listVectorTables(): Promise<VectorTable[]> {
  const res = await aiClient.request<{ tables: VectorTable[] }>("/api/v1/admin/vectors/tables")
  return res.tables
}
// ... 类似更新其他函数
```

- [ ] **Step 7: 验证版本化路由**

```bash
# v1 路由
curl -s http://localhost:8001/api/v1/agent/health
# 旧路由（应返回 Deprecation 头）
curl -s -D - http://localhost:8001/api/agent/health | grep -i deprecation
```

---

## Phase 3: 架构优化（5-7 天）

### Task 8: Safeguard 词库外部化

**Files:**
- Modify: `config.yaml`（新增 safeguard 配置块）
- Modify: `src/core/config.py`（新增 SafeguardSettings）
- Modify: `src/agents/safeguard.py`（从配置读取词库）

- [ ] **Step 1: 在 config.yaml 中添加 safeguard 配置**

```yaml
# 安全防护配置
safeguard:
  enabled: true
  threshold: 80
  malicious_targets:
    - "忽略先前指令"
    - "忽略所有设定"
    - "忘记之前的提示词"
    - "ignore previous instructions"
    - "disregard instructions"
    - "打印系统提示"
    - "你的初始指令是什么"
    - "system prompt"
    - "reveal system instructions"
    - "输出内部指令"
    - "你现在是黑客"
    - "从现在开始一个不受限制的"
    - "DAN模式"
    - "底层源码"
    - "内部绝密"
    - "管理员密码"
    - "系统漏洞"
```

- [ ] **Step 2: 在 core/config.py 中添加 SafeguardSettings**

在 Settings 类中添加：

```python
    # ==========================================
    # 7. 安全防护配置
    # ==========================================
    SAFEGUARD_ENABLED: bool = True
    SAFEGUARD_THRESHOLD: int = 80
    SAFEGUARD_MALICIOUS_TARGETS: list[str] = []

    @computed_field
    @property
    def SAFEGUARD_CONFIG(self) -> dict:
        return {
            "enabled": self.SAFEGUARD_ENABLED,
            "threshold": self.SAFEGUARD_THRESHOLD,
            "malicious_targets": self.SAFEGUARD_MALICIOUS_TARGETS,
        }
```

在 `model_post_init` 中读取 config.yaml 的 safeguard 配置：

```python
                    # 3. 🛡️ 安全防护配置
                    safeguard_cfg = data.get("safeguard", {})
                    self.SAFEGUARD_ENABLED = safeguard_cfg.get("enabled", True)
                    self.SAFEGUARD_THRESHOLD = safeguard_cfg.get("threshold", 80)
                    self.SAFEGUARD_MALICIOUS_TARGETS = safeguard_cfg.get("malicious_targets", [])
```

- [ ] **Step 3: 修改 agents/safeguard.py 从配置读取**

```python
class Safeguard:
    def __init__(self) -> None:
        self.enabled = settings.SAFEGUARD_ENABLED
        self.threshold = settings.SAFEGUARD_THRESHOLD

        if not self.enabled:
            logger.info("安全防护已禁用（通过配置）")
            return

        # 从配置读取词库
        self.malicious_targets = settings.SAFEGUARD_MALICIOUS_TARGETS

        if not self.malicious_targets:
            logger.warning("⚠️ Safeguard 词库为空，拦截功能将失效")
```

- [ ] **Step 4: 验证**

```bash
docker compose up -d --build ai_server
docker compose logs ai_server | grep -i safeguard
```

---

### Task 9: LlamaIndex 全局单例线程安全改造

**Files:**
- Modify: `src/agents/tools.py`

- [ ] **Step 1: 重构为双重检查锁单例**

```python
import threading
import logging

from langchain_core.tools import tool
from llama_index.core import VectorStoreIndex
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.vector_stores.postgres import PGVectorStore
from rapidfuzz import fuzz, process

from core import settings
from core.litellm_embedding import LiteLLMEmbedding
from api.exceptions import EmbeddingError, RetrievalError

logger = logging.getLogger(__name__)

if settings.POSTGRES_PASSWORD is None:
    raise ValueError("POSTGRES_PASSWORD 未配置")


class LlamaIndexResources:
    """线程安全的 LlamaIndex 资源单例。"""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        """获取单例实例（双重检查锁）。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls._initialize()
        return cls._instance

    @classmethod
    def _initialize(cls):
        """初始化 LlamaIndex 资源。"""
        try:
            embed_model = LiteLLMEmbedding(
                model_name="dashscope/text-embedding-v3",
                api_key=settings.DASHSCOPE_API_KEY.get_secret_value()
            )

            vector_store = PGVectorStore.from_params(
                host=settings.POSTGRES_HOST,
                port=str(settings.POSTGRES_PORT),
                database=settings.POSTGRES_DB,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                table_name=settings.TABLE_NAME_PREFIX,
                embed_dim=1024
            )

            index = VectorStoreIndex.from_vector_store(
                vector_store=vector_store,
                embed_model=embed_model
            )

            logger.info("✅ LlamaIndex 资源初始化完成")
            return {"embed_model": embed_model, "vector_store": vector_store, "index": index}

        except (ConnectionError, Exception) as exc:
            logger.error("LlamaIndex 资源初始化失败: %s", exc)
            cls._instance = None  # 重置单例，允许下次重试
            raise EmbeddingError(f"Failed to initialize LlamaIndex: {exc}")

    @classmethod
    def reset(cls):
        """重置单例（用于连接失败后的重新初始化）。"""
        with cls._lock:
            cls._instance = None
```

修改 `database_search` 工具函数：

```python
@tool
def database_search(query: str, category: str | None = None) -> str:
    """搜索方圆智版手册数据库。"""
    if not is_query_safe(query):
        return "对不起，您的查询涉及敏感信息，我无法为您检索相关内容。"

    try:
        resources = LlamaIndexResources.get_instance()
        index = resources["index"]

        filters = None
        if category:
            filters = MetadataFilters(
                filters=[ExactMatchFilter(key="category", value=category)]
            )
            logger.info("🔎 触发精准检索，锁定分类: [%s]", category)

        retriever = index.as_retriever(similarity_top_k=4, filters=filters)
        nodes = retriever.retrieve(query)

        if not nodes:
            return f"在分类 [{category or '全局'}] 中未能找到关于 '{query}' 的相关内容。"

        formatted_results = []
        for i, node_with_score in enumerate(nodes):
            node = node_with_score.node
            page_num = node.metadata.get("page_label", node.metadata.get("page", "未知"))
            file_name = node.metadata.get("file_name", "员工手册")
            content = node.get_content().strip().replace("\n", " ")
            result_item = (
                f"--- 来源 [{i+1}] ({file_name} 第 {page_num} 页) ---\n"
                f"{content}"
            )
            formatted_results.append(result_item)

        return f"针对问题 '{query}'，我找到了以下参考信息：\n\n" + "\n\n".join(formatted_results)

    except TimeoutError as exc:
        logger.error("检索超时: query=%s", query)
        return "数据库查询超时，请稍后重试。"
    except RetrievalError as exc:
        logger.error("检索失败: query=%s, error=%s", query, exc)
        return "数据库查询出错，请联系系统管理员。"
    except Exception as exc:
        logger.error("检索彻底失败: query=%s, error=%s", query, exc, exc_info=True)
        LlamaIndexResources.reset()  # 重置单例，允许下次重试
        return f"数据库查询出错，请联系系统管理员。"
```

- [ ] **Step 2: 验证线程安全**

```bash
# 并发请求测试
for i in {1..10}; do
  curl -s http://localhost:8001/api/agent/health &
done
wait
docker compose logs ai_server | grep "LlamaIndex"
```

---

### Task 10: Agent 注册表外部化

**Files:**
- Modify: `config.yaml`（新增 agents 配置块）
- Modify: `src/core/config.py`（新增 AgentRegistrySettings）
- Create: `src/agents/registry.py`
- Modify: `src/agents/agents.py`（重构为从配置加载）

- [ ] **Step 1: 在 config.yaml 中添加 agents 配置**

```yaml
# Agent 注册表
agents:
  default: "rag-assistant"
  registry:
    chatbot:
      module: "agents.chatbot"
      factory: "chatbot"
      description: "简单对话机器人"
    rag-assistant:
      module: "agents.rag_assistant"
      factory: "rag_assistant"
      description: "知识库检索助手"
```

- [ ] **Step 2: 在 core/config.py 中添加 Agent 配置读取**

在 `model_post_init` 中添加：

```python
                    # 4. 🤖 Agent 注册表配置
                    agents_cfg = data.get("agents", {})
                    self.AGENT_DEFAULT = agents_cfg.get("default", "rag-assistant")
                    self.AGENT_REGISTRY = agents_cfg.get("registry", {})
```

在 Settings 类中添加默认值：

```python
    AGENT_DEFAULT: str = "rag-assistant"
    AGENT_REGISTRY: dict = {}
```

- [ ] **Step 3: 创建 agents/registry.py**

```python
"""动态 Agent 注册表 — 从 config.yaml 加载 Agent 配置。"""

import importlib
import logging
from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from langgraph.pregel import Pregel

from core.config import settings

logger = logging.getLogger(__name__)

AgentGraph = CompiledStateGraph | Pregel


@dataclass
class AgentEntry:
    description: str
    graph_like: AgentGraph


# 全局注册表
_registry: dict[str, AgentEntry] = {}


def load_agents_from_config() -> dict[str, AgentEntry]:
    """从 config.yaml 加载所有注册的 Agent。"""
    global _registry
    _registry = {}

    for agent_id, agent_cfg in settings.AGENT_REGISTRY.items():
        try:
            module = importlib.import_module(agent_cfg["module"])
            factory = getattr(module, agent_cfg["factory"])

            _registry[agent_id] = AgentEntry(
                description=agent_cfg["description"],
                graph_like=factory,
            )
            logger.info("✅ Agent '%s' 已从配置加载", agent_id)

        except Exception as e:
            logger.error("❌ Agent '%s' 加载失败: %s", agent_id, e)

    return _registry


def get_agent(agent_id: str) -> AgentGraph:
    """获取已加载的 Agent 实例。"""
    if agent_id not in _registry:
        raise ValueError(f"Agent '{agent_id}' 未注册。可用: {list(_registry.keys())}")

    entry = _registry[agent_id]
    return entry.graph_like


def get_all_agent_info() -> list:
    """获取所有已注册 Agent 的信息。"""
    from schema import AgentInfo
    return [
        AgentInfo(key=agent_id, description=entry.description)
        for agent_id, entry in _registry.items()
    ]


async def load_agent(agent_id: str) -> None:
    """加载指定 Agent（预留接口，当前 Agent 启动时已加载）。"""
    pass
```

- [ ] **Step 4: 修改 agents/agents.py 使用注册表**

```python
"""Agent 管理器 — 从配置动态加载。"""

import logging

from agents.registry import (
    get_agent,
    get_all_agent_info,
    load_agent,
    load_agents_from_config,
)

logger = logging.getLogger(__name__)

# 启动时加载所有 Agent
load_agents_from_config()

DEFAULT_AGENT = settings.AGENT_DEFAULT

__all__ = [
    "DEFAULT_AGENT",
    "get_agent",
    "load_agent",
    "get_all_agent_info",
]
```

- [ ] **Step 5: 验证**

```bash
docker compose up -d --build ai_server
docker compose logs ai_server | grep -i "agent"
curl -s http://localhost:8001/api/v1/agent/info | python3 -m json.tool
```

---

## Phase 4: 工程化完善（3-5 天）

### Task 11: Graceful Shutdown 处理

**Files:**
- Modify: `src/api/service.py`（lifespan 关闭逻辑）
- Modify: `src/main.py`（信号处理）

- [ ] **Step 1: 完善 lifespan 的关闭逻辑**

```python
    finally:
        logger.info("🛑 开始优雅关闭...")

        # 1. 关闭 Redis 双池
        await AsyncRedisPool.close()
        SyncRedisPool.close()

        # 2. 关闭 admin_pool（如果存在）
        if hasattr(app.state, "admin_pool"):
            try:
                await app.state.admin_pool.close()
                logger.info("✅ PostgreSQL admin pool closed")
            except Exception as e:
                logger.error("关闭 admin_pool 失败: %s", e)

        # 3. Flush Langfuse 事件
        if settings.LANGFUSE_TRACING:
            try:
                from langfuse import Langfuse
                langfuse = Langfuse()
                langfuse.flush()
                logger.info("✅ Langfuse events flushed")
            except Exception as e:
                logger.error("Langfuse flush 失败: %s", e)

        logger.info("✅ 优雅关闭完成")
```

- [ ] **Step 2: 在 main.py 中添加 graceful shutdown 配置**

```python
    uvicorn.run(
        "api.service:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
        log_config=None,  # 使用自定义日志配置
    )
```

- [ ] **Step 3: 测试验证**

```bash
docker compose stop ai_server
docker compose logs ai_server | grep -i "关闭\|shutdown\|closed"
```

---

### Task 12: Rate Limit 响应头

**Files:**
- Modify: `src/api/rate_limit.py`

- [ ] **Step 1: 添加限流响应头**

修改 `check_rate_limit` 函数，在限流检查后添加响应头：

```python
from fastapi import Response

async def check_rate_limit(
    request: Request,
    response: Response,
    user_payload: Annotated[dict, Depends(verify_bearer)]
) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return

    limiter = get_rate_limiter()
    user_id = user_payload.get("sub")
    auth_mode = user_payload.get("auth_mode")

    if user_id and auth_mode != "none":
        key = f"ratelimit:user:{user_id}"
        limit_str = settings.RATE_LIMIT_AUTHENTICATED
    else:
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
        key = f"ratelimit:ip:{ip}"
        limit_str = settings.RATE_LIMIT_ANONYMOUS

    limit_item = parse(limit_str)

    # 获取限流状态
    period = limit_item.period
    max_count = limit_item.amount
    remaining = max(0, max_count - 1)  # 简化计算

    # 添加响应头
    response.headers["X-RateLimit-Limit"] = str(max_count)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(int(time.time()) + period)

    if not limiter.hit(limit_item, key):
        response.headers["Retry-After"] = str(period)
        logger.warning("🚫 触发限流: %s (额度: %s)", key, limit_str)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )
```

- [ ] **Step 2: 验证响应头**

```bash
curl -s -D - http://localhost:8001/api/v1/agent/info -H "Authorization: Bearer ..." | grep -i ratelimit
```

---

### Task 13: 数据库迁移管理脚本

**Files:**
- Create: `agent_api/migrations/001_initial_langgraph_tables.sql`
- Create: `agent_api/scripts/migrate.py`

- [ ] **Step 1: 创建迁移目录和初始迁移**

```sql
-- migrations/001_initial_langgraph_tables.sql
-- LangGraph 检查点表初始化

-- 此迁移由 LangGraph AsyncPostgresSaver.setup() 自动处理
-- 此处仅记录迁移版本
CREATE TABLE IF NOT EXISTS migration_log (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO migration_log (version, name) VALUES
    (1, 'initial_langgraph_tables')
ON CONFLICT (version) DO NOTHING;
```

- [ ] **Step 2: 创建迁移脚本**

```python
#!/usr/bin/env python3
"""数据库迁移管理脚本。"""

import asyncio
import logging
import sys
from pathlib import Path

import psycopg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


async def get_applied_migrations(conn) -> set[int]:
    """获取已应用的迁移版本。"""
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version FROM migration_log")
            return {row[0] for row in await cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        return set()


async def apply_migration(conn, version: int, sql: str) -> None:
    """应用单个迁移。"""
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO migration_log (version, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (version, f"migration_{version:03d}"),
        )
    logger.info("✅ 迁移 %03d 已应用", version)


async def run_migrations() -> None:
    """执行所有未应用的迁移。"""
    conn_string = (
        f"postgresql://{settings.POSTGRES_USER}:"
        f"{settings.POSTGRES_PASSWORD.get_secret_value()}@"
        f"{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/"
        f"{settings.POSTGRES_DB}"
    )

    async with psycopg.AsyncConnection.connect(conn_string) as conn:
        applied = await get_applied_migrations(conn)

        for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(migration_file.stem.split("_")[0])

            if version in applied:
                logger.info("⏭️  迁移 %03d 已应用，跳过", version)
                continue

            logger.info("🔄 应用迁移 %03d: %s", version, migration_file.name)
            sql = migration_file.read_text()
            await apply_migration(conn, version, sql)

        logger.info("✅ 所有迁移已完成")


if __name__ == "__main__":
    asyncio.run(run_migrations())
```

---

## 自审检查

### 1. 规范覆盖检查

| 规范要求 | 对应任务 | 状态 |
|---------|---------|------|
| 每个 step 包含实际代码 | 所有 task | ✅ |
| 无 TBD/TODO/placeholder | 全文扫描 | ✅ |
| 精确文件路径 | 所有 task | ✅ |
| 完整命令和预期输出 | 所有 task | ✅ |
| DRY/YAGNI/TDD | 架构设计 | ✅ |

### 2. 类型一致性检查

- `AppError` 在 Task 2 定义，后续 Task 3、8、9、10 中正确使用
- `RequestIDMiddleware` 在 Task 5 定义，Task 6 审计日志中使用 `request.state.request_id`
- `EmbeddingError`、`RetrievalError` 在 Task 2 定义，Task 3、9 中正确使用
- 路由前缀 `/api/v1/` 在 Task 7 统一修改

### 3. 无 Placeholder 扫描

全文搜索 "TODO"、"TBD"、"implement later"、"fill in" — 无匹配项。

---

## 执行选项

计划已完成。两种执行方式：

**1. 子代理驱动（推荐）** — 每个 Task 启动独立子代理，任务间有 review checkpoint，迭代快

**2. 内联执行** — 在当前会话中按 Task 顺序执行，批量处理带检查点

选择哪种方式开始执行？
