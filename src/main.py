import asyncio
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
        host="0.0.0.0",
        port=8001,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
        log_config=None,  # 使用自定义日志配置
    )
