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
