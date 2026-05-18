"""向量管理 API 请求/响应模型"""

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    """触发知识库入库请求"""
    directory_path: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="知识库目录路径",
        examples=["data/skirt", "data/shirt"],
    )


class TaskStatusResponse(BaseModel):
    """异步任务状态响应"""
    task_id: str
    status: str
    result: dict | None = None


class DeleteDocumentRequest(BaseModel):
    """删除文档请求"""
    table_name: str = Field(
        ...,
        min_length=6,
        max_length=100,
        pattern=r"^data_[a-zA-Z0-9_]+$",
        description="向量表名",
    )
    file_name: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="文件名",
    )


class VectorTableInfo(BaseModel):
    """向量表信息"""
    name: str
    chunk_count: int


class VectorTablesResponse(BaseModel):
    """向量表列表响应"""
    tables: list[VectorTableInfo]


class ChunkItem(BaseModel):
    """向量切片项"""
    id: str
    text: str
    token_est: int
    char_len: int
    source: str
    page: str


class ChunkStats(BaseModel):
    """切片统计信息"""
    total: int
    avg_tok: int
    max_tok: int


class ChunksResponse(BaseModel):
    """切片列表响应（含分页）"""
    chunks: list[ChunkItem]
    total_filtered: int
    stats: ChunkStats
    page: int
    per_page: int
    has_next: bool


class DeleteResponse(BaseModel):
    """删除响应"""
    deleted_chunks: int
    table_name: str
    file_name: str


# ── 查询参数约束 ──────────────────────────────────────────────────────
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class ChunkQueryParams(BaseModel):
    """切片查询参数"""
    table_name: str = Field(
        ...,
        min_length=6,
        max_length=100,
        pattern=r"^data_[a-zA-Z0-9_]+$",
    )
    search: str = Field(default="", max_length=200)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
