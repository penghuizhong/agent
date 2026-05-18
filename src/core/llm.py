import logging
import threading

import litellm
from langchain_core.language_models import BaseChatModel
from langchain_litellm.chat_models.litellm_router import ChatLiteLLMRouter

from core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LiteLLM Router 单例
# ---------------------------------------------------------------------------
_router: litellm.Router | None = None
_lock = threading.Lock()


def _get_router() -> litellm.Router:
    """获取（或初始化）全局 LiteLLM Router 单例。"""
    global _router

    if _router is not None:
        return _router

    with _lock:
        if _router is not None:
            return _router

        model_list = settings.LITELLM_MODEL_LIST
        if not model_list:
            raise ValueError("LITELLM_MODEL_LIST 为空，请检查 config.yaml 中的 providers 配置")

        logger.info("🔄 初始化 LiteLLM Router，共 %d 个模型", len(model_list))
        _router = litellm.Router(
            model_list=model_list,
            num_retries=3,
            timeout=60,
        )
        logger.info("✅ LiteLLM Router 初始化完成")
        return _router


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------
def get_model(model_name: str | None = None) -> BaseChatModel:
    """
    返回指定模型的 LangChain ChatLiteLLMRouter 实例。

    - 每次调用都返回绑定到全局 Router 的新实例（Router 本身是单例）。
    - LiteLLM Router 内部负责路由、重试、降级。
    """
    resolved = model_name or settings.DEFAULT_MODEL
    router = _get_router()

    # 验证模型是否在 Router 的 model_list 中
    available = {m["model_name"] for m in router.model_list}
    if resolved not in available:
        raise ValueError(
            f"模型 '{resolved}' 未在 Router 中注册。可用模型: {sorted(available)}"
        )

    # DeepSeek 模型特殊处理：关闭思考模式
    model_kwargs: dict = {}
    if "deepseek" in resolved.lower():
        model_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        logger.info("💡 检测到 DeepSeek 模型 (%s)，已关闭思考模式", resolved)

    return ChatLiteLLMRouter(
        router=router,
        model=resolved,
        streaming=True,
        temperature=0.0,
        max_retries=3,
        model_kwargs=model_kwargs,
    )


def invalidate_model(model_name: str | None = None) -> None:
    """
    重建 LiteLLM Router 单例，使下次 get_model() 使用最新配置。

    适用场景：API Key 轮换、config.yaml 热更新。
    传入 model_name 参数仅为了保持向后兼容，实际会重建整个 Router。
    """
    global _router

    with _lock:
        if _router is None:
            return

        old_models = [m["model_name"] for m in _router.model_list]
        _router = None
        logger.info("🗑️ 已销毁 LiteLLM Router 实例（原模型: %s）", old_models)
