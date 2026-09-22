# Proposal

## Why

Voice2Note（v2n）是一个"原始录音 → 笔记 markdown"的工作流（音频 → ASR → STT 原稿 → LLM → 笔记），目前只有一个跑通 ASR 的 MVP 脚本 `minimal.py`，没有任何 CLI 入口和配置体系。要把它变成可用的 uv tool，需要建立 CLI 骨架、以 `config.yaml` 为唯一默认值源的配置系统，并打通 ASR 与 LLM 两个流水线阶段。

## What Changes

- 新增 `v2n` CLI 入口（uv tool 方式安装，`uv tool install`），替代当前 `pyproject.toml` 中的 `voice2note` script 名。
- `v2n config` 指令：输出 `config.yaml` 的绝对路径；若文件不存在，则从内置模板创建后再输出路径。
- 新增内置 config 模板：通过默认值 + 注释的方式成为 v2n 所有配置的唯一默认值源，包含：
  - `VOICE_PATH`、`VOICE_FILE_KEYWORD`（正则）：音频文件发现规则；
  - `TORCH_BACKEND`（可选 `xpu` / `cuda`）、`DISABLE_MMAP`（默认注释掉；xpu 时默认开启——UMA 显存充足，cuda 默认关闭，解释写在模板注释中）、`ASR_LANGUAGE`：ASR 阶段配置；
  - `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`：LLM 阶段配置；
  - `NOTE_PATH`（默认 `outputs/`）、`TRANSCRIPT_PATH`（默认 `outputs/transcripts/`）、`MAX_TITLE_LENGTH`（默认 20）：输出规则。
- 新增配置加载与校验：Python 代码不得为必填字段提供备用默认值，缺失/非法时直接报错。
- ASR 阶段：模型固定为 Fun-ASR-Nano（model_id/revision 写死在代码），按 `TORCH_BACKEND`/`DISABLE_MMAP` 加载，按 `ASR_LANGUAGE` 转写发现的音频文件，STT 原稿保存到 `TRANSCRIPT_PATH`。
- LLM 阶段：OpenAI 兼容 API，以 tool calling 方式写入笔记 markdown，输出到 `NOTE_PATH`，文件名取正文首行、截断到 `MAX_TITLE_LENGTH`。
- 裸 `v2n` 指令：串起完整流水线，对发现的每个音频文件产出一份 STT 原稿和一篇笔记。

## Capabilities

### New Capabilities

- `cli`: `v2n` 命令行入口与子命令行为（裸 `v2n` 跑流水线、`v2n config` 管理配置），uv tool 打包方式。
- `config`: `config.yaml` 模板（唯一默认值源）、配置加载与校验（必填字段缺失即报错）、基于 `VOICE_PATH` + `VOICE_FILE_KEYWORD` 正则的音频文件发现规则。
- `asr`: ASR 阶段行为——固定模型加载（设备/`DISABLE_MMAP` 由配置驱动）、音频转写、STT 原稿落盘到 `TRANSCRIPT_PATH`。
- `llm`: LLM 阶段行为——OpenAI 兼容 API 调用、tool calling 写入笔记、`NOTE_PATH`/`MAX_TITLE_LENGTH` 输出规则。

### Modified Capabilities

（无 —— 项目尚无任何已有 spec。）

## Impact

- `pyproject.toml`：`[project.scripts]` 由 `voice2note` 改为 `v2n`（**BREAKING** 对已安装的旧入口名）；新增 `openai` 依赖。
- 新增 `src/voice2note/` 下的 config 模板资源、配置加载模块、CLI 模块、ASR 模块、LLM 模块。
- 依赖：新增 `pyyaml`（解析 config.yaml）、`openai`（OpenAI 兼容 API 客户端）；`torch`、`transformers` 已存在。
- 不影响：`minimal.py` 保持原样作为 ASR MVP 参考。
