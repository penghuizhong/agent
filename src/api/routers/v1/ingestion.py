"""入库任务 API 路由"""

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from api.deps import CurrentUser
from core.postgres import get_async_pool
from ingestion import tasks
from ingestion.md5_checker import build_incremental_plan
from ingestion.pipeline import run_ingestion
from schema.ingestion import (
    CancelTaskResponse,
    IngestRequest,
    TaskStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["入库任务"])


@router.post(
    "/ingest",
    response_model=TaskStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发知识库入库",
)
async def trigger_ingestion(
    body: IngestRequest,
    _user: CurrentUser,
    background_tasks: BackgroundTasks,
    pool=Depends(get_async_pool),
) -> TaskStatusResponse:
    """
    触发知识库入库。

    1. 创建任务记录
    2. 执行 MD5 增量比对（主进程）
    3. BackgroundTasks 启动流水线
    """
    task_id = uuid4()

    # 创建任务记录
    await tasks.create_task(pool, task_id, body.directory_path)

    # MD5 增量比对
    plan = await build_incremental_plan(pool, body.directory_path)

    # 更新跳过的文件数
    await tasks.update_task_status(
        pool, task_id, "pending",
        files_skipped=len(plan.to_skip),
    )

    if not plan.to_process:
        await tasks.update_task_status(
            pool, task_id, "done",
            files_skipped=len(plan.to_skip),
        )
        return TaskStatusResponse(
            task_id=str(task_id),
            status="done",
            files_skipped=len(plan.to_skip),
        )

    # 异步启动流水线
    background_tasks.add_task(run_ingestion, pool, task_id, plan)

    return TaskStatusResponse(
        task_id=str(task_id),
        status="pending",
        files_skipped=len(plan.to_skip),
    )


@router.get(
    "/ingest/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询入库任务状态",
)
async def get_ingest_status(
    task_id: str,
    _user: CurrentUser,
    pool=Depends(get_async_pool),
) -> TaskStatusResponse:
    """查询入库任务状态"""
    try:
        tid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid task ID")

    row = await tasks.get_task_status(pool, tid)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskStatusResponse(
        task_id=str(row["id"]),
        status=row["status"],
        total_chunks=row["total_chunks"],
        files_processed=row["files_processed"],
        files_skipped=row["files_skipped"],
        error_message=row["error_message"],
    )


@router.post(
    "/ingest/{task_id}/cancel",
    response_model=CancelTaskResponse,
    summary="取消入库任务",
)
async def cancel_ingest(
    task_id: str,
    _user: CurrentUser,
    pool=Depends(get_async_pool),
) -> CancelTaskResponse:
    """取消入库任务（仅允许取消 pending/processing）"""
    try:
        tid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid task ID")

    success = await tasks.cancel_task(pool, tid)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Task cannot be cancelled (already done/failed/cancelled)",
        )

    return CancelTaskResponse(
        task_id=task_id,
        status="cancelled",
        message="Task cancelled successfully",
    )
