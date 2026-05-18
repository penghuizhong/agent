"""
方圆智版 · LiteLLM LlamaIndex Embedding 适配器
提供 LlamaIndex BaseEmbedding 接口的 LiteLLM 实现
"""

from typing import Any

import litellm
from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.embeddings import BaseEmbedding


class LiteLLMEmbedding(BaseEmbedding):
    """LiteLLM 驱动的 LlamaIndex Embedding 适配器"""

    model_name: str = "dashscope/text-embedding-v3"
    api_key: str | None = None
    api_base: str | None = None
    _litellm_model: str = PrivateAttr()

    def __init__(
        self,
        model_name: str = "dashscope/text-embedding-v3",
        api_key: str | None = None,
        api_base: str | None = None,
        embed_batch_size: int = 10,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            api_base=api_base,
            embed_batch_size=embed_batch_size,
            **kwargs,
        )
        # LiteLLM 模型标识：dashscope 提供商使用 openai/ 前缀兼容
        if model_name.startswith("dashscope/"):
            self._litellm_model = f"openai/{model_name.split('/', 1)[1]}"
        else:
            self._litellm_model = model_name

    def _get_query_embedding(self, query: str) -> list[float]:
        response = litellm.embedding(
            model=self._litellm_model,
            input=query,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        return response.data[0].embedding

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._get_query_embedding(text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        response = litellm.embedding(
            model=self._litellm_model,
            input=texts,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        return [item.embedding for item in response.data]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        response = await litellm.aembedding(
            model=self._litellm_model,
            input=query,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        return response.data[0].embedding

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return await self._aget_query_embedding(text)

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        response = await litellm.aembedding(
            model=self._litellm_model,
            input=texts,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        return [item.embedding for item in response.data]
