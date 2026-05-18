import logging

from core import settings
from core.llm import get_model
from core.postgres import get_async_pool

logger = logging.getLogger("uvicorn")

TITLE_PROMPT = """根据用户的输入，提取核心意图作为会话标题。
要求：只输出标题文本，不要任何标点符号，字数限制在 4-8 个字内。
用户输入：{message}"""

# ✅ 可配置：从 settings 读取 core 服务的表名前缀（默认 'core_'）
# 这样如果 core 服务改变了表前缀，只需要改 .env，不用改代码
CHAT_SESSIONS_TABLE = f"{settings.CORE_TABLE_PREFIX}{settings.CHAT_SESSION_TABLE_NAME}"


async def generate_title_background(thread_id: str, first_message: str) -> None:
    """后台异步生成标题，不阻塞主消息流"""
    try:
        llm = get_model(settings.DEFAULT_MODEL)
        prompt = TITLE_PROMPT.format(message=first_message[:100])
        response = await llm.ainvoke(prompt)
        title = str(response.content).strip()[:20]

        if not title:
            return

        # 条件更新：仅当 title 仍为默认值时才更新，防止覆盖用户手动重命名
        pool = await get_async_pool()
        async with pool.acquire() as conn:
            # ✅ 使用变量拼接 SQL，不再硬编码
            sql = f"""
                UPDATE {CHAT_SESSIONS_TABLE} 
                SET title = $1 
                WHERE thread_id = $2::uuid 
                AND (title = '新对话' OR title IS NULL)
            """
            result = await conn.execute(sql, title, thread_id)
            logger.info("标题生成 for %s: %s (%s rows updated)", thread_id, title, result)
    except Exception as e:
        logger.error("标题生成失败 for %s: %s", thread_id, e)
