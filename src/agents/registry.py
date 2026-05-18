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
    from schema.schema import AgentInfo
    return [
        AgentInfo(key=agent_id, description=entry.description)
        for agent_id, entry in _registry.items()
    ]


async def load_agent(agent_id: str) -> None:
    """加载指定 Agent（预留接口，当前 Agent 启动时已加载）。"""
    pass
