import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse  # type: ignore[import-untyped]
from langgraph.types import Command, Interrupt
from uuid_utils import uuid7

from agents import DEFAULT_AGENT, AgentGraph, get_agent, get_all_agent_info
from api.deps import CurrentUser
from api.services.title_generator import generate_title_background
from api.utils import (
    convert_message_content_to_string,
    langchain_to_chat_message,
    remove_tool_calls,
)
from core import settings
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
)

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/v1/agent", tags=["Agent 相关接口"])


def custom_generate_unique_id(route: APIRoute) -> str:
    return route.name


# ---------------------------------------------------------------------------
# /info - 返回服务元数据（agent 列表 + 可用模型列表）
# ---------------------------------------------------------------------------
@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=get_all_agent_info(),
        models=models,
        default_agent=DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _handle_input(
    user_input: UserInput,
    agent: AgentGraph,
    current_user: dict,
) -> tuple[dict[str, Any], UUID]:
    run_id = uuid7()
    thread_id = user_input.thread_id or str(uuid4())

    user_id = current_user.get("sub") or str(uuid4())

    configurable = {"thread_id": thread_id, "user_id": user_id}
    if user_input.model is not None:
        configurable["model"] = user_input.model

    if user_input.agent_config:
        reserved_keys = {"thread_id", "user_id", "model"}
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    config = RunnableConfig(
        configurable=configurable,
        run_id=run_id,
    )

    state = await agent.aget_state(config=config)
    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks:
        input = Command(resume=user_input.message)
    else:
        input = {"messages": [HumanMessage(content=user_input.message)]}

    return {"input": input, "config": config}, run_id


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(
    user_input: UserInput,
    current_user: CurrentUser,
    agent_id: str = DEFAULT_AGENT,
) -> ChatMessage:
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, current_user)

    try:
        response_events: list[tuple[str, Any]] = await agent.ainvoke(
            **kwargs, stream_mode=["updates", "values"]
        )  # type: ignore
        response_type, response = response_events[-1]
        if response_type == "values":
            output = langchain_to_chat_message(response["messages"][-1])
        elif response_type == "updates" and "__interrupt__" in response:
            output = langchain_to_chat_message(
                AIMessage(content=response["__interrupt__"][0].value)
            )
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)

        return output
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Agent invoke error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error")


async def message_generator(
    user_input: StreamInput,
    current_user: dict,
    agent_id: str = DEFAULT_AGENT,
) -> AsyncGenerator[str, None]:
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, current_user)

    try:
        async for stream_event in agent.astream(
            **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
        ):
            if not isinstance(stream_event, tuple):
                continue

            if len(stream_event) == 3:
                _, stream_mode, event = stream_event
            else:
                stream_mode, event = stream_event

            new_messages = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(AIMessage(content=interrupt.value))
                        continue

                    if node == "block_unsafe_content":
                        unsafe_msgs = updates.get("messages", [])
                        if unsafe_msgs:
                            unsafe_content = unsafe_msgs[-1].content
                            yield f"data: {json.dumps({'type': 'token', 'content': unsafe_content})}\n\n"

                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    if "supervisor" in node or "sub-agent" in node:
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                update_messages = update_messages[-2:]
                            else:
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                new_messages = [event]

            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    current_message[key] = value
                else:
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in processed_messages:
                try:
                    chat_message = langchain_to_chat_message(message)
                    chat_message.run_id = str(run_id)
                except Exception as e:
                    logger.error("Error parsing message: %s", e, exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Unexpected error'})}\n\n"
                    continue
                if chat_message.type == "human" and chat_message.content == user_input.message:
                    continue
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                if not isinstance(msg, AIMessageChunk):
                    continue
                content = remove_tool_calls(msg.content)
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
    except Exception as e:
        logger.error("Error in message generator: %s", e, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
    finally:
        yield "data: [DONE]\n\n"


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(
    user_input: StreamInput,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    agent_id: str = DEFAULT_AGENT,
) -> StreamingResponse:
    # 检测是否是新 thread 的第一条消息
    if user_input.thread_id:
        agent: AgentGraph = get_agent(agent_id)
        config = RunnableConfig(
            configurable={"thread_id": user_input.thread_id}
        )
        try:
            state = await agent.aget_state(config=config)
            is_new_thread = not state.values.get("messages")
        except Exception:
            is_new_thread = False

        if is_new_thread:
            background_tasks.add_task(
                generate_title_background,
                user_input.thread_id,
                user_input.message,
            )

    return StreamingResponse(
        message_generator(user_input, current_user, agent_id),
        media_type="text/event-stream",
    )


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    try:
        langfuse = Langfuse()
        kwargs = feedback.kwargs or {}
        langfuse.score(
            trace_id=str(feedback.run_id),
            name=feedback.key or "user_feedback",
            value=feedback.score,
            comment=kwargs.get("comment", ""),
            **kwargs,
        )
        langfuse.flush()
        return FeedbackResponse()
    except Exception as e:
        logger.error("写入 Langfuse 评分时发生错误: %s", e, exc_info=True)
        return FeedbackResponse()


# ---------------------------------------------------------------------------
# /history - 查询指定 thread 的历史消息（直接查询 LangGraph PostgreSQL checkpointer）
# ---------------------------------------------------------------------------
@router.post("/history")
async def history(input: ChatHistoryInput) -> ChatHistory:
    agent: AgentGraph = get_agent(DEFAULT_AGENT)

    try:
        state_snapshot = await agent.aget_state(
            config=RunnableConfig(configurable={"thread_id": input.thread_id})
        )
        messages: list[AnyMessage] = state_snapshot.values["messages"]
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except Exception as e:
        logger.error("Agent history error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error")
