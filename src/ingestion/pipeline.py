"""入库流水线编排 — 整合解析、Embedding、写入"""

import asyncio
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from uuid import UUID

import asyncpg

from core.config import settings
from ingestion import embedder, tasks, writer
from ingestion.md5_checker import IncrementalPlan

logger = logging.getLogger(__name__)


def _init_embedder():
    """初始化 LlamaIndex Embedding 模型"""
    from llama_index.core import Settings

    from core.config import settings as app_settings
    from core.litellm_embedding import LiteLLMEmbedding

    dashscope_key = app_settings.DASHSCOPE_API_KEY.get_secret_value()
    embed_model = LiteLLMEmbedding(
        model_name="dashscope/text-embedding-v3",
        api_key=dashscope_key,
        embed_batch_size=10,
    )
    Settings.embed_model = embed_model


def _create_extension_mapping():
    """配置文件解析器扩展映射"""
    from llama_index.readers.file import UnstructuredReader

    try:
        unstructured_reader = UnstructuredReader()
        return {
            ext: unstructured_reader
            for ext in [".pdf", ".docx", ".doc", ".md", ".txt", ".html"]
        }
    except ImportError:
        return None


def _categorize_file(file_path: str, category_rules: dict) -> str:
    """根据文件路径匹配分类"""
    fpath_lower = file_path.lower()
    for target_category, keywords in category_rules.items():
        if any(kw in fpath_lower for kw in keywords):
            return target_category
    return "通用"


def _inject_metadata(doc, category_rules: dict, timestamp: int) -> None:
    """为文档注入 metadata"""
    fpath = doc.metadata.get("file_path", "")
    doc.metadata["category"] = _categorize_file(fpath, category_rules)
    doc.metadata["timestamp"] = timestamp
    doc.metadata.pop("file_path", None)
    doc.metadata.pop("creation_date", None)
    doc.metadata.pop("last_modified_date", None)


def _group_files_by_dir(file_results: list[dict]) -> dict[str, list[str]]:
    """按目录分组文件路径"""
    dir_files: dict[str, list[str]] = {}
    for f in file_results:
        parent = str(Path(f["path"]).parent)
        dir_files.setdefault(parent, []).append(f["path"])
    return dir_files


def parse_and_chunk_sync(file_results: list[dict]) -> list[dict]:
    """
    在 ProcessPoolExecutor 中运行：解析文件 + 分块 + 注入 metadata。

    接收序列化的文件列表（Path 不可 pickle，传 dict），
    返回序列化的文档列表。
    """
    from llama_index.core import SimpleDirectoryReader

    from core.config import settings as app_settings

    _init_embedder()
    extension_mapping = _create_extension_mapping()
    dir_files = _group_files_by_dir(file_results)

    all_docs = []
    current_timestamp = int(time.time())
    category_rules = app_settings.CATEGORY_RULES

    for dir_path, file_paths in dir_files.items():
        reader = SimpleDirectoryReader(
            input_dir=dir_path,
            file_extractor=extension_mapping,
        )
        documents = reader.load_data()

        file_names = {Path(p).name for p in file_paths}
        documents = [d for d in documents if d.metadata.get("file_name") in file_names]

        for doc in documents:
            _inject_metadata(doc, category_rules, current_timestamp)

            if doc.text and doc.text.strip():
                all_docs.append({
                    "text": doc.text,
                    "metadata": doc.metadata,
                })

    return all_docs


async def run_ingestion(
    pool: asyncpg.Pool,
    task_id: UUID,
    plan: IncrementalPlan,
) -> None:
    """
    执行入库流水线。

    1. ProcessPoolExecutor 解析/分块
    2. asyncio.gather + Semaphore 获取 Embedding
    3. asyncpg 批量写入
    """
    api_key = settings.DASHSCOPE_API_KEY.get_secret_value()

    try:
        # Step 1: 解析分块（CPU 密集型 → 进程池）
        await tasks.update_task_status(pool, task_id, "processing")

        file_dicts = [
            {"path": str(f.path), "name": f.name, "md5": f.md5}
            for f in plan.to_process
        ]

        loop = asyncio.get_event_loop()
        with ProcessPoolExecutor(max_workers=2) as executor:
            docs = await loop.run_in_executor(
                executor,
                parse_and_chunk_sync,
                file_dicts,
            )

        if not docs:
            await tasks.update_task_status(
                pool, task_id, "done",
                total_chunks=0,
                files_processed=len(plan.to_process),
            )
            return

        # Step 2: Embedding（网络 IO → asyncio.gather + Semaphore）
        texts = [d["text"] for d in docs]
        metadatas = [d["metadata"] for d in docs]

        embeddings_raw = await embedder.embed_documents(texts, api_key)

        # 过滤失败的 embedding
        valid_records = []
        for text, meta, emb in zip(texts, metadatas, embeddings_raw):
            if isinstance(emb, Exception):
                logger.warning("Skipping doc due to embedding error: %s", emb)
                continue
            valid_records.append((text, meta, emb))

        if not valid_records:
            raise RuntimeError("All embeddings failed")

        texts_ok = [r[0] for r in valid_records]
        metas_ok = [r[1] for r in valid_records]
        embs_ok = [r[2] for r in valid_records]

        # Step 3: 批量写入（数据库 IO → asyncpg copy_records_to_table）
        # 先删除旧数据
        files_to_delete = list(set(d["metadata"].get("file_name") for d in docs))
        await writer.delete_old_documents(pool, files_to_delete)

        # 构建记录并写入
        records = writer.build_records(texts_ok, metas_ok, embs_ok)
        await writer.insert_vectors(pool, records)

        # 更新 ingested_files
        file_hashes = [(f.name, f.md5) for f in plan.to_process]
        await writer.update_ingested_files(pool, file_hashes)

        # 更新任务状态为 Done
        await tasks.update_task_status(
            pool, task_id, "done",
            total_chunks=len(records),
            files_processed=len(plan.to_process),
        )

        logger.info(
            "Ingestion complete: task=%s, chunks=%d, files=%d",
            task_id, len(records), len(plan.to_process),
        )

    except Exception as e:
        logger.error("Ingestion failed: task=%s, error=%s", task_id, e, exc_info=True)
        await tasks.update_task_status(
            pool, task_id, "failed",
            error_message=str(e),
        )
