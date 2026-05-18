"""Agent 管理器 — 从配置动态加载。"""

import logging

from agents.registry import (
    AgentGraph,
    get_agent,
    get_all_agent_info,
    load_agent,
    load_agents_from_config,
)
from core.config import settings

logger = logging.getLogger(__name__)

# 启动时加载所有 Agent
load_agents_from_config()

DEFAULT_AGENT = settings.AGENT_DEFAULT

__all__ = [
    "DEFAULT_AGENT",
    "AgentGraph",
    "get_agent",
    "load_agent",
    "get_all_agent_info",
]
