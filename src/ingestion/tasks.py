"""入库任务状态管理 — 直接操作 Postgres"""

import logging
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "processing", "done", "failed", "cancelled"}

# 允许动态更新的字段白名单（防止 SQL 注入）
ALLOWED_UPDATE_FIELDS = {
    "files_skipped",
    "total_chunks",
    "files_processed",
    "error_message",
}


async def create_task(
    pool: asyncpg.Pool,
    task_id: UUID,
    directory_path: str,
) -> None:
    """创建入库任务记录"""
    await pool.execute(
        """
        INSERT INTO ingestion_tasks (id, directory_path, status)
        VALUES ($1, $2, 'pending')
        """,
        task_id,
        directory_path,
    )


async def update_task_status(
    pool: asyncpg.Pool,
    task_id: UUID,
    status: str,
    **kwargs,
) -> None:
    """更新任务状态（支持附加字段）"""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    # 验证字段白名单
    for key in kwargs:
        if key not in ALLOWED_UPDATE_FIELDS:
            raise ValueError(f"Invalid update field: {key}")

    fields = ["status = $3"]
    values: list = [task_id, status]
    param_idx = 4

    for key, value in kwargs.items():
        fields.append(f"{key} = ${param_idx}")
        values.append(value)
        param_idx += 1

    query = f"""
        UPDATE ingestion_tasks
        SET {', '.join(fields)}
        WHERE id = $1
    """
    await pool.execute(query, *values)


async def get_task_status(
    pool: asyncpg.Pool,
    task_id: UUID,
) -> dict | None:
    """查询任务状态"""
    return await pool.fetchrow(
        "SELECT * FROM ingestion_tasks WHERE id = $1",
        task_id,
    )


async def cancel_task(
    pool: asyncpg.Pool,
    task_id: UUID,
) -> bool:
    """
    取消任务。仅允许取消 pending/processing 状态。
    返回 True 表示成功取消。
    """
    result = await pool.execute(
        """
        UPDATE ingestion_tasks
        SET status = 'cancelled'
        WHERE id = $1 AND status IN ('pending', 'processing')
        """,
        task_id,
    )
    return result == "UPDATE 1"
