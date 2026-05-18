"""异步 Embedding — 调用 DashScope API，Semaphore 限流"""

import asyncio
import logging

import httpx

from core import settings

logger = logging.getLogger(__name__)

# 默认值（可通过环境变量覆盖）
DEFAULT_EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"
CONCURRENCY_LIMIT = 10
BATCH_SIZE = 10


def _get_embedding_api_url() -> str:
    """获取 Embedding API URL（优先使用环境变量）"""
    return getattr(settings, "DASHSCOPE_EMBEDDING_URL", None) or DEFAULT_EMBEDDING_API_URL


def _get_embedding_model() -> str:
    """获取 Embedding 模型名称（优先使用环境变量）"""
    return getattr(settings, "DASHSCOPE_EMBEDDING_MODEL", None) or DEFAULT_EMBEDDING_MODEL


async def get_embeddings_batch(
    texts: list[str],
    api_key: str,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> list[list[float] | Exception]:
    """
    批量获取 Embedding（受 Semaphore 保护）。
    返回与 texts 等长的列表，成功为 vector，失败为 Exception。
    """
    async with semaphore:
        try:
            resp = await client.post(
                _get_embedding_api_url(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _get_embedding_model(),
                    "input": {"texts": texts},
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["output"]["embeddings"]]
        except Exception as e:
            logger.error("Embedding batch failed: %s", e)
            return [e] * len(texts)


async def embed_documents(
    texts: list[str],
    api_key: str,
) -> list[list[float] | Exception]:
    """
    并发获取所有文档的 Embedding。

    使用 asyncio.Semaphore 限制并发量防 429。
    return_exceptions 风格：失败项在结果列表中标记为 Exception。
    """
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    async with httpx.AsyncClient() as client:
        # 分批
        batches = [texts[i:i + BATCH_SIZE] for i in range(0, len(texts), BATCH_SIZE)]

        tasks = [
            get_embeddings_batch(batch, api_key, semaphore, client)
            for batch in batches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # 展平结果
    flat: list[list[float] | Exception] = []
    for result in results:
        if isinstance(result, Exception):
            flat.extend([result] * BATCH_SIZE)
        else:
            flat.extend(result)

    return flat[:len(texts)]  # 截断到原始长度
