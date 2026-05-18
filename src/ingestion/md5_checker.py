"""MD5 增量比对 — 扫描目录，计算哈希，对比数据库"""

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from ingestion.constants import VECTOR_TABLE_NAME

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".txt", ".html"}

_md5_executor = ThreadPoolExecutor(max_workers=4)


@dataclass
class FileScanResult:
    """文件扫描结果"""
    path: Path
    name: str
    md5: str


@dataclass
class IncrementalPlan:
    """增量入库计划"""
    to_skip: list[FileScanResult] = field(default_factory=list)
    to_process: list[FileScanResult] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)  # file_names with changed hash


def compute_file_md5(file_path: Path) -> str:
    """计算文件 MD5 哈希值"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


async def compute_file_md5_async(file_path: Path) -> str:
    """异步计算文件 MD5，不阻塞事件循环"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_md5_executor, compute_file_md5, file_path)


async def build_incremental_plan(
    pool: asyncpg.Pool,
    directory: str,
) -> IncrementalPlan:
    """
    构建增量入库计划。

    1. 扫描目录下所有支持的文件
    2. 计算每个文件的 MD5
    3. 对比 ingested_files 表
    4. 返回：跳过列表、处理列表、需删除的文件名列表
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ValueError(f"Directory not found: {directory}")

    # 扫描文件
    files: list[FileScanResult] = []
    for ext in SUPPORTED_EXTENSIONS:
        for f in dir_path.rglob(f"*{ext}"):
            if f.is_file():
                md5 = await compute_file_md5_async(f)
                files.append(FileScanResult(
                    path=f,
                    name=f.name,
                    md5=md5,
                ))

    if not files:
        logger.warning("No supported files found in %s", directory)
        return IncrementalPlan()

    # 查询已入库文件
    rows = await pool.fetch(
        """
        SELECT file_name, file_hash
        FROM ingested_files
        WHERE table_name = $1
        """,
        VECTOR_TABLE_NAME,
    )
    existing: dict[str, str] = {row["file_name"]: row["file_hash"] for row in rows}

    plan = IncrementalPlan()

    for f in files:
        if f.name in existing:
            if existing[f.name] == f.md5:
                # 哈希一致 → 跳过
                plan.to_skip.append(f)
            else:
                # 哈希改变 → 需删除旧数据 + 处理新数据
                plan.to_delete.append(f.name)
                plan.to_process.append(f)
        else:
            # 新文件 → 处理
            plan.to_process.append(f)

    logger.info(
        "Incremental plan: skip=%d, process=%d, delete=%d",
        len(plan.to_skip),
        len(plan.to_process),
        len(plan.to_delete),
    )
    return plan
