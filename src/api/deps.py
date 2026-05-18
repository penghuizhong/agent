import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core import settings

logger = logging.getLogger("uvicorn")

security = HTTPBearer()


async def verify_bearer(
    request: Request,
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
) -> dict:
    """
    验证 Bearer token，返回解密后的用户 payload。
    使用 HS256 对称加密算法，由 Next.js BFF 层签发。
    """
    if not http_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Header",
        )

    try:
        payload = jwt.decode(
            http_auth.credentials,
            settings.AUTH_SECRET.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["sub"]},
        )
        request.state.user = payload
        return payload
    except jwt.ExpiredSignatureError:
        logger.info("[AUTH] 拦截请求：Token 已过期")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已过期",
        )
    except jwt.InvalidTokenError as e:
        logger.warning(f"[AUTH] JWT 验证失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或遭篡改",
        )


# 类型别名：路由函数里直接用  
CurrentUser = Annotated[dict, Depends(verify_bearer)]
