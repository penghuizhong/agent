import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from api.deps import verify_bearer
from core import settings

logger = logging.getLogger(__name__)


# ── 单例初始化 ────────────────────────────────────────────────────────

def _make_limiter() -> MovingWindowRateLimiter:
    # 使用 MemoryStorage：适用于单实例部署。
    # 如需多实例共享限流计数，改为 RedisStorage。
    storage = MemoryStorage()
    logger.info("已启用进程内 MemoryStorage 限流")
    return MovingWindowRateLimiter(storage)

_limiter: MovingWindowRateLimiter = _make_limiter()


# ── 工具函数 ──────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _resolve_limit_key(user_payload: dict, request: Request) -> tuple[str, str]:
    """返回 (limit_key, limit_str)"""
    user_id = user_payload.get("sub")
    auth_mode = user_payload.get("auth_mode")

    if user_id and auth_mode != "none":
        return f"ratelimit:user:{user_id}", settings.RATE_LIMIT_AUTHENTICATED

    ip = _get_client_ip(request)
    return f"ratelimit:ip:{ip}", settings.RATE_LIMIT_ANONYMOUS


# ── 限流依赖 ──────────────────────────────────────────────────────────

async def check_rate_limit(
    request: Request,
    response: Response,
    user_payload: Annotated[dict, Depends(verify_bearer)],
) -> None:
    """限流依赖：复用鉴权结果，命中限额时返回 429。"""
    if not settings.RATE_LIMIT_ENABLED:
        return

    key, limit_str = _resolve_limit_key(user_payload, request)

    try:
        limit_item = parse(limit_str)
    except ValueError:
        # 配置错误不应该阻断请求，但必须报警
        logger.exception("限流配置格式错误，已跳过限流: %s", limit_str)
        return

    if not _limiter.hit(limit_item, key):
        logger.warning("[RATE_LIMIT] 触发限流: key=%s limit=%s", key, limit_str)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(limit_item.period)},
        )

    # 写入剩余次数响应头，方便客户端感知
    stats = _limiter.get_window_stats(limit_item, key)
    response.headers["X-RateLimit-Remaining"] = str(stats.remaining)
    response.headers["X-RateLimit-Reset"] = str(stats.reset_time)