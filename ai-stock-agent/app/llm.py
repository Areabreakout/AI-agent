# -*- coding: utf-8 -*-
"""LLM 客户端：调用用户配置的 OpenAI 兼容 API（DeepSeek / GLM / 通义 / OpenAI 等）
不训练任何模型，所有智能均来自用户添加的 API。"""
import json
from typing import Generator

from openai import OpenAI

from . import settings


def _get_client() -> OpenAI:
    cfg = settings.load()["llm"]
    if not cfg.get("api_key"):
        raise RuntimeError("尚未配置 LLM API，请先在【设置】中添加 API Key")
    return OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120)


def chat(messages: list, tools: list = None, tool_choice: str = "auto") -> dict:
    """单次对话调用（含工具定义），返回完整响应"""
    cfg = settings.load()["llm"]
    client = _get_client()
    kwargs = dict(
        model=cfg["model"],
        messages=messages,
        temperature=cfg.get("temperature", 0.3),
    )
    if tools:
        kwargs.update(tools=tools, tool_choice=tool_choice)
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message


def chat_stream(messages: list, on_tool_call=None) -> Generator[str, None, None]:
    """流式对话：逐段产出文本增量。
    Agent 循环中若模型发起工具调用，则通过 on_tool_call 执行工具、
    把结果回填后继续流式，直至产出最终文本。"""
    cfg = settings.load()["llm"]
    client = _get_client()
    tools = None
    if on_tool_call is not None:
        tools = on_tool_call()

    msgs = list(messages)
    max_tool_rounds = 8  # 防止工具调用死循环
    for _ in range(max_tool_rounds):
        kwargs = dict(model=cfg["model"], messages=msgs,
                      temperature=cfg.get("temperature", 0.3), stream=True)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        stream = client.chat.completions.create(**kwargs)

        collected = []
        tool_calls = []  # [{index, id, name, arguments}]
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if delta.content:
                collected.append(delta.content)
                yield delta.content
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    while len(tool_calls) <= tc.index:
                        tool_calls.append({"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        tool_calls[tc.index]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls[tc.index]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls[tc.index]["arguments"] += tc.function.arguments

        text = "".join(collected)
        if not tool_calls:
            return  # 正常结束

        # 模型请求调用工具：先回填助手消息，再执行工具并追加结果
        msgs.append({"role": "assistant", "content": text or None,
                     "tool_calls": [{"id": tc["id"], "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": tc["arguments"]}}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            result = on_tool_call(call=tc)
            yield f"\n```tool\n{result}\n```\n"  # 前端渲染为可折叠的工具调用过程
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": result})
    yield "\n（已达到最大工具调用轮数，停止继续调用）"
