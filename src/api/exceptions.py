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


class PermissionDeniedError(AppError):
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
