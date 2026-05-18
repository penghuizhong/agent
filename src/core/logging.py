"""结构化日志配置 — 生产环境输出 JSON 格式，开发环境保持人类可读。"""

import logging
import sys

from core.config import settings


class RequestIdFilter(logging.Filter):
    """日志过滤器：自动注入 request_id 到每条日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
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
