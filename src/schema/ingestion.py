"""入库任务 API 请求/响应模型"""

from pydantic import BaseModel, Field, field_validator


class IngestRequest(BaseModel):
    """触发知识库入库请求"""
    directory_path: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="知识库目录路径",
        examples=["data/skirt", "data/shirt"],
    )

    @field_validator("directory_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        if ".." in v:
            raise ValueError("路径不能包含 '..'")
        if v.startswith("/"):
            raise ValueError("路径必须是相对路径")
        if v.startswith("./"):
            v = v[2:]
        return v


class TaskStatusResponse(BaseModel):
    """异步任务状态响应"""
    task_id: str
    status: str
    total_chunks: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    error_message: str | None = None


class CancelTaskResponse(BaseModel):
    """取消任务响应"""
    task_id: str
    status: str
    message: str
