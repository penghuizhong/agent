"""API 审计日志中间件 — 记录所有 API 请求的完整审计信息。"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("audit")

# 跳过审计的路径
SKIP_PATHS = {
    "/v1/agent/health",
    "/v1/agent/docs",
    "/v1/agent/openapi.json",
    "/v1/agent/redoc",
    "/favicon.ico",
}


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
