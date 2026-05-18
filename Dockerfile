# ==============================================================================
# 方圆智版 AI - Agent 服务 Dockerfile (极简优化)
# ==============================================================================
# 构建：docker build -t fyzj-agent .
# 运行：docker run --rm -p 8001:8001 fyzj-agent
# ==============================================================================

FROM python:3.12-slim AS base

# ── 1. 环境变量 ───────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app/src:/app" \
    UV_PROJECT_ENVIRONMENT="/usr/local/" \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

# ── 2. 创建非 root 用户 ──────────────────────────────────────────────────
RUN adduser --disabled-password --gecos '' appuser

# ── 3. 安装 uv ────────────────────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ── 4. 依赖层（利用 Docker 缓存） ─────────────────────────────────────────
WORKDIR /app
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ── 5. 应用代码层 ─────────────────────────────────────────────────────────
COPY src/ ./src/

# ── 6. 安全设置 ───────────────────────────────────────────────────────────
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app

USER appuser

# ── 7. 健康检查 ───────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/v1/agent/health')" || exit 1

EXPOSE 8001

# ── 8. 启动命令 ───────────────────────────────────────────────────────────
CMD ["python", "src/main.py"]
