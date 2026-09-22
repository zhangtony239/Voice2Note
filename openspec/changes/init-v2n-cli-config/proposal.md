# Proposal

## Why

Voice2Note（v2n）是一个"原始录音 → 笔记 markdown"的工作流（音频 → ASR → STT 原稿 → LLM → 笔记），目前只有一个跑通 ASR 的 MVP 脚本 `minimal.py`，没有任何 CLI 入口和配置体系。要把它变成可用的 uv tool，第一步是建立 CLI 骨架和以 `config.yaml` 为唯一默认值源的配置系统，后续 ASR/LLM 流水线阶段都依赖这套配置。

## What Changes

- 新增 `v2n` CLI 入口（uv tool 方式安装，`uv tool install`），替代当前 `pyproject.toml` 中的 `voice2note` script 名。
- 本期只实现一个指令：`v2n config` —— 输出 `config.yaml` 的绝对路径；若文件不存在，则从内置模板创建后再输出路径。
- 新增内置 config 模板：通过默认值 + 注释的方式成为 v2n 所有配置的唯一默认值源，包含：
  - `VOICE_PATH`、`VOICE_FILE_KEYWORD`（正则）：音频文件发现规则；
  - `TORCH_BACKEND`（可选 `xpu` / `cuda`）；
  - `DISABLE_MMAP`：默认注释掉；xpu 时默认开启（UMA 显存充足），cuda 默认关闭，解释写在模板注释中；
  - `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`：LLM 阶段配置；
  - `NOTE_PATH`（默认 `outputs/`）、`MAX_TITLE_LENGTH`（默认 20）：笔记输出规则。
- 新增配置加载与校验：Python 代码不得为必填字段提供备用默认值，缺失/非法时直接报错。
- 预留（仅定义配置契约，不在本期实现）：ASR 阶段按 `TORCH_BACKEND`/`DISABLE_MMAP` 加载模型；LLM 阶段以 tool calling 方式写文件，输出到 `NOTE_PATH`，文件名取正文首行、截断到 `MAX_TITLE_LENGTH`。

## Capabilities

### New Capabilities

- `cli`: `v2n` 命令行入口与子命令行为（本期仅 `v2n config`），uv tool 打包方式。
- `config`: `config.yaml` 模板（唯一默认值源）、配置加载与校验（必填字段缺失即报错）、基于 `VOICE_PATH` + `VOICE_FILE_KEYWORD` 正则的音频文件发现规则。

### Modified Capabilities

（无 —— 项目尚无任何已有 spec。）

## Impact

- `pyproject.toml`：`[project.scripts]` 由 `voice2note` 改为 `v2n`（**BREAKING** 对已安装的旧入口名）。
- 新增 `src/voice2note/` 下的 config 模板资源、配置加载模块与 CLI 模块。
- 依赖：新增 `pyyaml`（解析/生成 config.yaml）；`torch`、`transformers` 已存在（本期不改动 `minimal.py` 的 ASR 逻辑，仅作为后续阶段参考）。
- 不影响：`minimal.py` 保持原样作为 ASR MVP 参考。
