from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    key: str = Field(description="代理键名", examples=["research-assistant"])
    description: str = Field(description="代理的描述", examples=["用于生成研究论文的研究助手"])


class ServiceMetadata(BaseModel):
    agents: list[AgentInfo]
    models: list[str] = Field(examples=[["qwen-turbo", "deepseek-chat"]])
    default_agent: str = Field(examples=["research-assistant"])
    default_model: str = Field(examples=["deepseek-chat"])


class UserInput(BaseModel):
    message: str = Field(description="用户输入消息", examples=["裙装原型的制版方法?"])
    model: str | None = Field(default=None, examples=["deepseek-chat"])
    thread_id: str | None = Field(default=None, examples=["847c6285-8fc9-4560-a83f-4e6285809254"])
    agent_config: dict[str, Any] = Field(default={})
    # ✅ user_id 删除，从 JWT 的 sub 字段注入，不信任前端传值


class StreamInput(UserInput):
    stream_tokens: bool = Field(default=True, description="是否流式传输 LLM token")


class ToolCall(BaseModel):           # ✅ TypedDict → BaseModel，统一验证
    name: str
    args: dict[str, Any] = Field(default={})
    id: str | None = None
    type: Literal["tool_call"] = "tool_call"


class ChatMessage(BaseModel):
    type: Literal["human", "ai", "tool", "custom"]
    content: str
    tool_calls: list[ToolCall] = Field(default=[])
    tool_call_id: str | None = None
    run_id: str | None = None
    response_metadata: dict[str, Any] = Field(default={})
    custom_data: dict[str, Any] = Field(default={})

    def pretty_repr(self) -> str:
        base_title = self.type.title() + " Message"
        padded = " " + base_title + " "
        sep_len = (80 - len(padded)) // 2
        sep = "=" * sep_len
        second_sep = sep + "=" if len(padded) % 2 else sep
        return f"{sep}{padded}{second_sep}\n\n{self.content}"

    def pretty_print(self) -> None:
        print(self.pretty_repr())  # noqa: T201


class Feedback(BaseModel):
    run_id: str = Field(examples=["847c6285-8fc9-4560-a83f-4e6285809254"])
    key: str = Field(examples=["human-feedback-stars"])
    score: float = Field(ge=0.0, le=1.0, examples=[0.8])   # ✅ 加范围校验
    kwargs: dict[str, Any] = Field(default={})


class FeedbackResponse(BaseModel):
    status: Literal["success"] = "success"


class ChatHistoryInput(BaseModel):
    thread_id: str = Field(examples=["847c6285-8fc9-4560-a83f-4e6285809254"])


class ChatHistory(BaseModel):
    messages: list[ChatMessage]