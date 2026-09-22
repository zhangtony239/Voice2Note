"""LLM 阶段：OpenAI 兼容 API + tool calling 写入笔记。

文件写入以 tool calling 方式实现：模型通过 `write_note` 工具提交笔记
markdown 内容，由 v2n 执行实际写盘；文件名由 CLI 的 TITLE 确定（TITLE
为空时取笔记正文首行），并截断到 `MAX_TITLE_LENGTH`。

批次内每份 STT 原稿以 openai 库 v1 兼容的 file 内容块逐份发送
（base64 data URL，filename 为真实原稿文件名），不与 user prompt（TITLE）
文本拼接；一次请求产出一篇笔记。
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)

from voice2note.config import Config

WRITE_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_note",
        "description": "将整理好的笔记 markdown 写入文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "完整的笔记 markdown 正文",
                },
            },
            "required": ["content"],
        },
    },
}

#: tool calling 轮次上限，防止死循环
_MAX_TOOL_ROUNDS = 5

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
_HEADING_PREFIX = re.compile(r"^#+\s*")


def _sanitize_title(title: str, config: Config) -> str:
    """清理标题：去非法文件名字符、截断到 MAX_TITLE_LENGTH。"""
    return _ILLEGAL_FILENAME_CHARS.sub("", title)[: config.max_title_length].strip()


def note_filename(title: str, config: Config, content: str) -> str:
    """生成笔记文件名：TITLE 非空时由其确定，否则回退取正文首行（去标题记号）。"""
    if title.strip():
        cleaned = _sanitize_title(title, config)
    else:
        first_line = next(
            (line.strip() for line in content.splitlines() if line.strip()), ""
        )
        cleaned = _sanitize_title(_HEADING_PREFIX.sub("", first_line), config)
    return f"{cleaned or 'untitled'}.md"


def write_note(content: str, config: Config, title: str = "") -> Path:
    """将笔记 markdown 写入 `NOTE_PATH`（目录不存在时创建）。"""
    path = config.note_path / note_filename(title, config, content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def generate_note(
    transcripts: Sequence[tuple[str, str]],
    config: Config,
    client=None,
    user_prompt: str = "",
) -> Path:
    """调用 LLM 将 STT 原稿整理为笔记，经 tool calling 写入文件。

    ``transcripts`` 为批次内全部 STT 原稿的 ``(文件名, 文本)`` 列表：每份
    原稿作为独立 file 内容块发送（filename 为真实原稿文件名），一次请求
    产出一篇笔记；``user_prompt`` 为 CLI 的 TITLE：随原稿发送给 LLM，并直接
    决定笔记文件名。``client`` 仅供测试注入假客户端；生产路径使用 openai SDK。
    """
    if client is None:
        from openai import OpenAI

        client = OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)

    user_parts: list[dict] = [
        {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": "data:text/markdown;base64,"
                + base64.b64encode(text.encode("utf-8")).decode("ascii"),
            },
        }
        for filename, text in transcripts
    ]
    if user_prompt:
        user_parts.append({"type": "text", "text": user_prompt})

    messages: list = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": user_parts},
    ]
    note_path: Path | None = None
    for _ in range(_MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=cast("Iterable[ChatCompletionMessageParam]", messages),
            tools=cast("Iterable[ChatCompletionToolUnionParam]", [WRITE_NOTE_TOOL]),
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls
        if not tool_calls:
            if note_path is not None:
                return note_path
            raise RuntimeError(
                "LLM 未通过 tool calling 提交笔记内容，请检查 SYSTEM_PROMPT 与模型能力。"
            )

        messages.append(message)
        for call in tool_calls:
            function = getattr(call, "function", None)
            if function is None or function.name != "write_note":
                result = f"未知工具: {getattr(function, 'name', 'unknown')}"
            else:
                args = json.loads(function.arguments or "{}")
                content = args.get("content")
                if not isinstance(content, str) or not content.strip():
                    result = "错误: content 不能为空"
                else:
                    note_path = write_note(content, config, user_prompt or "")
                    result = f"已写入: {note_path}"
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    raise RuntimeError(f"LLM 超过 {_MAX_TOOL_ROUNDS} 轮仍未完成笔记写入。")
