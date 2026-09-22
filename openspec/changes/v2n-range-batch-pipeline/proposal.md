# Proposal

## Why

当前 `v2n [PROMPT]` 对发现的每个音频文件独立执行「ASR → LLM」，逐文件生成一篇笔记：同一场会议拆成多个录音（如 `会议录音 141.aac`、`会议录音 142.aac`）时会产生多篇割裂的笔记，LLM 也无法跨文件组织内容。需要按编号范围批量选取文件，等全部 ASR 完成后把所有 STT 原稿一次性交给 LLM，生成一篇完整笔记。

## What Changes

- **BREAKING**: CLI 入口从 `v2n [PROMPT]` 改为 `v2n START-END "TITLE"`；裸 `v2n`（无参数）与可选 `PROMPT` 用法移除，缺少 `START-END` 或 `TITLE` 时报用法错误并以非零退出码退出。
- 新增范围选取：`START-END` 按文件名中的数字段整体匹配——从每个音频文件名中提取首个数字段（整数或小数，如 `141`、`1.2`），按数值（int/float）而非字符串比较，选取编号落在 `[START, END]` 闭区间内的所有文件（如 `141-142`、`1.2-2.4`）。
- 流水线编排改为两阶段：先对范围内全部文件逐一 ASR 并各自保存 STT 原稿 md；全部转写完成后，将这一批原稿合并为一份文档一次性发送给 LLM。
- 笔记文件名仅由 `TITLE` 确定（清理文件系统非法字符并截断到 `MAX_TITLE_LENGTH`）；移除「取笔记正文首行」的回退命名。
- `TITLE` 作为独立 text 内容块随合并原稿发送给 LLM（延续现有「file 块 + text 块不拼接」的消息结构）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `cli`: 移除可选 `PROMPT` 与裸 `v2n` 用法，改为必填 `START-END` 与 `"TITLE"` 两个位置参数；新增按数字段范围选取音频文件、两阶段批处理编排（全部 ASR 完成后一次性调用 LLM）的需求。
- `llm`: 由「单份原稿一次调用」改为「多份原稿合并后一次调用」；笔记文件名改为仅由 CLI 的 `TITLE` 确定，移除正文首行回退。

> 注：主 specs（`openspec/specs/`）当前为空，前序变更 `init-v2n-cli-config` 尚未归档/同步。本变更的 delta 以该变更定义的 `cli`、`llm` 能力需求为基线改写；归档顺序应先 `init-v2n-cli-config`、后本变更，同名需求以本变更为准。

## Impact

- `src/voice2note/cli.py`：参数解析（`START-END`、`TITLE` 两个必填位置参数）与 `_cmd_run` 编排重写（两阶段批处理）。
- `src/voice2note/llm.py`：`generate_note` 改为接受多份原稿；`note_filename` 移除 `prompt` 回退逻辑，仅由 `TITLE` 命名。
- `src/voice2note/config.py`（或 cli 内）：新增从文件名提取数字段与范围过滤的辅助逻辑。
- 无新增第三方依赖；`asr.py`、`config.py` 的配置字段不变。