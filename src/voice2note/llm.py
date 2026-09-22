"""LLM 阶段：OpenAI 兼容 API + tool calling 写入笔记。

文件写入以 tool calling 方式实现：模型通过 `write_note` 工具提交笔记
markdown 内容，由 v2n 执行实际写盘；文件名取正文首行并截断到
`MAX_TITLE_LENGTH`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from voice2note.config import Config

WRITE_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_note",
        "description": "将整理好的笔记 markdown 写入文件。content 为完整笔记正文，首行将作为文件名。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "完整的笔记 markdown 正文，首行作为标题/文件名",
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


def note_filename(content: str, config: Config) -> str:
    """由笔记正文首行生成文件名：去标题记号、清理非法字符、截断。"""
    first_line = next(
        (line.strip() for line in content.splitlines() if line.strip()), ""
    )
    title = _HEADING_PREFIX.sub("", first_line).strip()
    title = _ILLEGAL_FILENAME_CHARS.sub("", title)
    title = title[: config.max_title_length].strip()
    return f"{title or 'untitled'}.md"


def write_note(content: str, config: Config) -> Path:
    """将笔记 markdown 写入 `NOTE_PATH`（目录不存在时创建）。"""
    path = config.note_path / note_filename(content, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def generate_note(transcript: str, config: Config, client=None) -> Path:
    """调用 LLM 将 STT 原稿整理为笔记，经 tool calling 写入文件。

    ``client`` 仅供测试注入假客户端；生产路径使用 openai SDK。
    """
    if client is None:
        from openai import OpenAI

        client = OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)

    messages: list = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": transcript},
    ]
    note_path: Path | None = None
    for _ in range(_MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=config.llm_model,
            messages=messages,
            tools=[WRITE_NOTE_TOOL],
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
            if call.function.name != "write_note":
                result = f"未知工具: {call.function.name}"
            else:
                args = json.loads(call.function.arguments or "{}")
                content = args.get("content")
                if not isinstance(content, str) or not content.strip():
                    result = "错误: content 不能为空"
                else:
                    note_path = write_note(content, config)
                    result = f"已写入: {note_path}"
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    raise RuntimeError(f"LLM 超过 {_MAX_TOOL_ROUNDS} 轮仍未完成笔记写入。")
