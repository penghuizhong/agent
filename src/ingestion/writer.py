"""asyncpg 批量写入 pgvector"""

import json
import logging
from uuid import uuid4

import asyncpg

from ingestion.constants import VECTOR_TABLE_NAME

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


async def delete_old_documents(
    pool: asyncpg.Pool,
    file_names: list[str],
) -> int:
    """删除指定文件名的旧向量数据"""
    if not file_names:
        return 0

    deleted = await pool.execute(
        f"""
        DELETE FROM {VECTOR_TABLE_NAME}
        WHERE metadata_->>'file_name' = ANY($1)
        """,
        file_names,
    )
    # 解析 DELETE N
    count = int(deleted.split()[-1]) if deleted.split()[-1].isdigit() else 0
    logger.info("Deleted %d old vectors for %d files", count, len(file_names))
    return count


async def insert_vectors(
    pool: asyncpg.Pool,
    records: list[tuple],
) -> int:
    """
    批量插入向量数据。

    records: list of (id, text, metadata_json, embedding_vector)
    使用 copy_records_to_table 实现高性能批量写入。
    """
    if not records:
        return 0

    columns = ["id", "text", "metadata_", "embedding"]

    # 分批写入
    total = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        await pool.copy_records_to_table(
            VECTOR_TABLE_NAME,
            columns=columns,
            records=batch,
        )
        total += len(batch)

    logger.info("Inserted %d vectors into %s", total, VECTOR_TABLE_NAME)
    return total


def build_records(
    texts: list[str],
    metadatas: list[dict],
    embeddings: list[list[float]],
) -> list[tuple]:
    """构建 copy_records_to_table 所需的记录列表"""
    records = []
    for text, meta, embedding in zip(texts, metadatas, embeddings):
        records.append((
            str(uuid4()),
            text,
            json.dumps(meta, ensure_ascii=False),
            embedding,
        ))
    return records


async def update_ingested_files(
    pool: asyncpg.Pool,
    file_hashes: list[tuple[str, str]],
) -> None:
    """
    更新 ingested_files 表。

    file_hashes: list of (file_name, file_hash)
    使用 UPSERT 语义（ON CONFLICT DO UPDATE）。
    """
    if not file_hashes:
        return

    await pool.executemany(
        """
        INSERT INTO ingested_files (file_name, table_name, file_hash)
        VALUES ($1, $2, $3)
        ON CONFLICT (file_name, table_name)
        DO UPDATE SET file_hash = EXCLUDED.file_hash, ingested_at = NOW()
        """,
        [(name, VECTOR_TABLE_NAME, h) for name, h in file_hashes],
    )
    logger.info("Updated ingested_files for %d files", len(file_hashes))
