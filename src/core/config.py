import logging
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import find_dotenv
from pydantic import (
    SecretStr,
    computed_field,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("uvicorn")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # ==========================================
    # 1. 基础网络配置
    # ==========================================
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    MODE: str = "production"
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30

    # ==========================================
    # 2. 核心大模型配置 (Agent 专属)
    # ==========================================
    DEEPSEEK_API_KEY: SecretStr | None = None
    DASHSCOPE_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    OLLAMA_API_KEY: SecretStr | None = None
    MLX_API_KEY: SecretStr | None = None
    ENABLE_SAFEGUARD: bool = True
    LANGFUSE_TRACING: bool = False
    
    # 动态加载的配置项
    DEFAULT_MODEL: str = "deepseek-chat"
    AVAILABLE_MODELS: list[str] = []
    PROVIDER_CONFIG: dict[str, dict] = {}

    # ==========================================
    # 3. 基础设施：PostgreSQL (LangGraph Checkpointer)
    # ==========================================
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_APPLICATION_NAME: str = "agent_service"
    
    # Core 服务表名前缀（Agent 服务需要跨服务访问 core 数据库）
    CORE_TABLE_PREFIX: str = "core_"
    CHAT_SESSION_TABLE_NAME: str = "chat_sessions"
    
    # Agent 服务表名前缀（LangGraph checkpointer 使用）
    AGENT_TABLE_PREFIX: str = "agent_server_"
    
    
    # psycopg_pool 原生连接池优化 (非 SQLAlchemy)
    POSTGRES_POOL_OPEN_TIMEOUT: int = 30
    POSTGRES_POOL_CLOSE_TIMEOUT: int = 10
    POSTGRES_POOL_MAX_IDLE_TIME: int = 300
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = 5
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = 30

    # ==========================================
    # 5. 安全防护与鉴权 (BFF 对称验签)
    # ==========================================
    AUTH_SECRET: SecretStr | None = None

    # fastapi的docs接口开关（生产环境建议关闭）
    SHOW_DOCS: bool = True
    API_PREFIX_AGENT: str = "/v1/agent"    


    CORS_ORIGINS: str = "http://localhost:3000,https://chat.fyzj.online"
    
    @property
    def cors_list(self) -> list[str]:
        return [i.strip() for i in self.CORS_ORIGINS.split(",") if i.strip()]
    
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_ANONYMOUS: str = "5/minute"
    RATE_LIMIT_AUTHENTICATED: str = "50/minute"
    
    
    # ==========================================
    # 7. 安全防护配置
    # ==========================================
    SAFEGUARD_ENABLED: bool = True
    SAFEGUARD_THRESHOLD: int = 80
    SAFEGUARD_MALICIOUS_TARGETS: list[str] = []

    # ==========================================
    # 8. Agent 注册表配置
    # ==========================================
    AGENT_DEFAULT: str = "rag-assistant"
    AGENT_REGISTRY: dict = {}

    # ==========================================
    # 9. Embedding 配置
    # ==========================================
    EMBEDDING_DIM: int = 1024

    # ==========================================
    # 10. 知识库分类路由规则（由 config.yaml 动态加载）
    # ==========================================
    CATEGORY_RULES: dict = {}


    def model_post_init(self, __context: Any) -> None:
        """初始化后自动读取项目根目录下的 config.yaml (用于管理 LLM 提供商)"""
        current_file = Path(__file__).resolve()

        possible_roots = [
            current_file.parent.parent.parent.parent,  # /fyzj/
            current_file.parent.parent.parent,         # /fyzj/agent_api/
            current_file.parent.parent,                # /fyzj/agent_api/src/
        ]

        yaml_path = None
        for root in possible_roots:
            candidate = root / "config.yaml"
            if candidate.exists():
                yaml_path = candidate
                break

        if yaml_path is None:
            yaml_path = current_file.parent.parent.parent / "config.yaml"

        if yaml_path.exists():
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    self.PROVIDER_CONFIG = data.get("providers", {})

                    models = []
                    for p_val in self.PROVIDER_CONFIG.values():
                        models.extend(p_val.get("models", []))
                    self.AVAILABLE_MODELS = models

                    self.DEFAULT_MODEL = data.get("default_model", self.DEFAULT_MODEL)
                    # 2. 🌟 挂载知识库分类路由规则
                    self.CATEGORY_RULES = data.get("category_routing", {})
                    # 3. 🛡️ 安全防护配置
                    safeguard_cfg = data.get("safeguard", {})
                    self.SAFEGUARD_ENABLED = safeguard_cfg.get("enabled", True)
                    self.SAFEGUARD_THRESHOLD = safeguard_cfg.get("threshold", 80)
                    self.SAFEGUARD_MALICIOUS_TARGETS = safeguard_cfg.get("malicious_targets", [])
                    # 4. 🤖 Agent 注册表配置
                    agents_cfg = data.get("agents", {})
                    self.AGENT_DEFAULT = agents_cfg.get("default", "rag-assistant")
                    self.AGENT_REGISTRY = agents_cfg.get("registry", {})
                logger.info(f"✅ 成功从 {yaml_path} 加载大模型配置")
            except Exception as e:
                logger.error(f"❌ 加载 config.yaml 失败: {e}")
        else:
            logger.warning(f"⚠️ 未找到 {yaml_path}, 将使用系统默认模型")

    @computed_field
    @property
    def BASE_URL(self) -> str:
        return f"http://{self.HOST}:{self.PORT}"

    def _get_provider_prefix(self, provider_key: str) -> str:
        """获取 LiteLLM 支持的提供商前缀"""
        # dashscope 使用 openai 兼容接口，新版 litellm 不再支持 dashscope 前缀
        provider_map = {
            "deepseek": "deepseek",
            "openai": "openai",
            "ollama": "ollama",
            "mlx": "openai",
            "dashscope": "openai",
        }
        return provider_map.get(provider_key, "openai")

    def build_litellm_model_list(self) -> list[dict[str, Any]]:
        """将 PROVIDER_CONFIG 转换为 LiteLLM Router 的 model_list 格式"""
        model_list = []
        for provider_key, provider_cfg in self.PROVIDER_CONFIG.items():
            base_url = provider_cfg.get("base_url", "")
            models = provider_cfg.get("models", [])
            api_key = self._get_provider_api_key(provider_key)

            prefix = self._get_provider_prefix(provider_key)

            for model_name in models:
                entry: dict[str, Any] = {
                    "model_name": model_name,
                    "litellm_params": {
                        "model": f"{prefix}/{model_name}",
                    },
                }
                if api_key:
                    entry["litellm_params"]["api_key"] = api_key.get_secret_value()
                if base_url:
                    entry["litellm_params"]["api_base"] = base_url

                model_list.append(entry)

        return model_list

    def _get_provider_api_key(self, provider_key: str) -> SecretStr | None:
        """根据提供商名称获取对应的 API Key"""
        key_map = {
            "dashscope": self.DASHSCOPE_API_KEY,
            "deepseek": self.DEEPSEEK_API_KEY,
            "openai": self.OPENAI_API_KEY,
            "ollama": self.OLLAMA_API_KEY,
            "mlx": self.MLX_API_KEY,
        }
        return key_map.get(provider_key)

    @cached_property
    def LITELLM_MODEL_LIST(self) -> list[dict[str, Any]]:
        """LiteLLM Router 初始化所需的 model_list"""
        return self.build_litellm_model_list()

    def is_dev(self) -> bool:
        return self.MODE.lower() == "dev"
    
@lru_cache
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


settings = get_settings()
