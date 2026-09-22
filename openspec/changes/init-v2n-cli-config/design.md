# Design

## Context

项目现状：`minimal.py` 是已跑通的 ASR MVP（Fun-ASR-Nano + transformers，`.to("xpu")`，`disable_mmap=True`）；`pyproject.toml` 已有 `voice2note` script 名与 uv 构建后端，依赖含 torch/torchaudio/transformers/librosa；`src/voice2note/__init__.py` 为空壳。见 proposal.md 的 Why。

约束：
- 开发与安装走 uv（`uv tool install`）。
- `config.yaml` 位于相对路径（当前工作目录），是唯一配置来源；模板是唯一默认值源。
- Python 代码不得为必填字段提供备用默认值——缺失即报错。

## Goals / Non-Goals

**Goals:**
- `v2n` console script + `v2n config` 子命令（定位/创建 config.yaml）。
- 内置 config 模板（默认值 + 注释），随包分发。
- 配置加载模块：读取、校验必填字段、推导 `DISABLE_MMAP`、暴露音频文件发现。
- 为后续 ASR/LLM 阶段固化配置契约（本期不实现流水线本身）。

**Non-Goals:**
- 不实现 ASR / LLM 流水线阶段（后续 change）。
- 不实现 `v2n run` 等其他子命令。
- 不改动 `minimal.py`。
- 不做全局配置目录（如 `~/.config`）——配置永远跟随当前工作目录。

## Decisions

### D1: CLI 框架 —— 标准库 `argparse`，不引入 click/typer
只有一个子命令，argparse 足够；避免多余依赖。备选：click（若后续子命令增多可再迁移，成本可控）。

### D2: 模板分发 —— 包内资源文件 + `importlib.resources`
模板作为 `src/voice2note/assets/config-template.yaml` 随包分发，用 `importlib.resources.files()` 读取。备选：Python 字符串常量——被否决，因为 YAML 文件便于直接编辑和 diff，且注释天然保留（PyYAML dump 会丢注释，所以创建文件时是**原样复制**模板文本，而非 load→dump）。

### D3: YAML 解析 —— 新增 `pyyaml` 依赖
标准库无 YAML 支持。备选 ruamel.yaml（保留注释）被否决：只在读取时需要解析，写入走原样复制，PyYAML 足够。

### D4: 配置加载与校验 —— 显式必填清单 + fail-fast
- `load_config()` 读取 `./config.yaml`；文件不存在 → 报错并提示运行 `v2n config`。
- 必填字段清单：`VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`。任一缺失/为空 → `ConfigError`，信息列出全部缺失字段（一次性报全，避免逐个试错）。
- `NOTE_PATH`、`MAX_TITLE_LENGTH` 在模板中有默认值，但代码中**不设**备用默认值：模板创建后它们必然存在；若用户删掉则同样报错（保持"模板是唯一默认值源"的单一性）。
- `TORCH_BACKEND` 取值白名单 `{xpu, cuda}`；`VOICE_FILE_KEYWORD` 用 `re.compile` 校验合法性。
- `DISABLE_MMAP`：可选；未配置时按 `TORCH_BACKEND` 推导（xpu→True，cuda→False）；显式配置优先。
- 校验错误统一抛 `ConfigError`，CLI 层捕获后以非零退出码 + stderr 输出。

### D5: 音频文件发现 —— 独立纯函数
`discover_voice_files(config) -> list[Path]`：遍历 `VOICE_PATH`（递归），文件名 `re.search` 匹配 `VOICE_FILE_KEYWORD`。本期只提供该函数与配置契约，不接入任何命令。备选：`glob` 模式被否决——用户明确要求正则。

### D6: 包结构
```
src/voice2note/
  __init__.py        # 保持
  cli.py             # argparse 入口：v2n config
  config.py          # load_config / ConfigError / discover_voice_files / create_config
  assets/
    config-template.yaml
```
`pyproject.toml`：`[project.scripts]` 改为 `v2n = "voice2note.cli:main"`；`[tool.uv.build-backend]` 需声明 `module-name = "voice2note"` 并确保 assets 随包安装（uv_build 默认包含包目录下所有文件）。

### D7: 模板内容（草案，实现时以此为准）
```yaml
# Voice2Note 配置 —— 本文件是 v2n 所有配置的唯一默认值源。
# 必填字段删除或留空后，v2n 会在加载配置时报错（代码中无备用默认值）。

# 音频文件所在目录（相对或绝对路径均可）
VOICE_PATH: .

# 音频文件名的正则表达式（re.search 语义），例如: r"\.(mp3|wav|m4a|flac)$"
VOICE_FILE_KEYWORD: \.(mp3|wav|m4a|flac)$

# PyTorch 后端，可选: xpu | cuda
TORCH_BACKEND: xpu

# 是否禁用 mmap 加载模型权重。默认注释掉，由 TORCH_BACKEND 推导：
#   xpu  -> 默认开启（true）：UMA 共享内存充足，禁用 mmap 可避免跨设备映射问题
#   cuda -> 默认关闭（false）
# 取消注释可显式覆盖推导值。
# DISABLE_MMAP: true

# LLM 阶段（OpenAI 兼容 API）
LLM_BASE_URL: ""      # 例如: https://api.example.com/v1
LLM_API_KEY: ""       # 必填
LLM_MODEL: ""         # 必填，例如: gpt-4o-mini
SYSTEM_PROMPT: ""     # 必填，驱动 LLM 生成笔记并以 tool calling 写文件

# ASR 转写语言（必填）
ASR_LANGUAGE: zh

# STT 原稿保存目录（目录不存在时自动创建）
TRANSCRIPT_PATH: .transcripts/

# 笔记输出目录
NOTE_PATH: outputs/

# 笔记文件名最大长度（取正文首行，超长截断）
MAX_TITLE_LENGTH: 20
```
注：`VOICE_PATH`/`VOICE_FILE_KEYWORD`/`TORCH_BACKEND`/`ASR_LANGUAGE`/`NOTE_PATH`/`TRANSCRIPT_PATH`/`MAX_TITLE_LENGTH` 的模板默认值即"唯一默认值源"；`LLM_*` 与 `SYSTEM_PROMPT` 无合理默认值，模板留空、加载时报错。

### D8: 裸 `v2n` 运行流水线
argparse 的 subparsers 不设 `required`：无子命令时执行流水线（`_cmd_run`），`v2n config` 仍为子命令。备选：新增 `v2n run` 子命令被否决——用户明确指定裸 `v2n`。

### D9: ASR 阶段实现（`asr.py`）
- 固定模型：`FunAudioLLM/Fun-ASR-Nano-2512-hf` + revision `d93b302ee7fd505e1b3576120fc142fc6f7820e1`（与 `minimal.py` 一致，写死在代码）。
- 加载：`dtype=torch.bfloat16`，`.to(TORCH_BACKEND)`，processor 与 model 均透传 `disable_mmap=生效值`。
- 推理：`processor.apply_transcription_request(audio=..., language=ASR_LANGUAGE, ...)`，`model.generate(max_new_tokens=512, do_sample=False)`（512 为记录的假设：MVP 的 128 对真实录音偏短）。
- 密集语音单元切分：实测（会议录音）解码单元混入大段静音（老师翻页/演示）会触发重复幻觉（≤30s 且含长静音的块也循环；>45s 幻觉、>122s 超位置嵌入上限报错）。因此以 20ms 帧级 RMS + 自适应阈值（噪声地板分位 ×4）检测语音区（容忍 <0.4s 短停顿），语音区边缘仅保留 0.15s 呼吸空间，解码单元只含密集语音；句间停顿 ≤0.5s 的相邻语音区合并（≤30s）；<1s 微单元并入相邻单元；超 30s 单元在目标位置之前 5s 内选最安静 20ms 窗口保底硬切。音频统一加载为 16kHz float32 数组（librosa；aac/m4a 经 PyAV）。拼接时中日文不加空格、其余语言加空格。转写进度经 `progress_factory` 回调接入 tqdm（逐句更新）。验证：10 分钟录音切 65 单元（平均 8.3s），此前幻觉的两个区域（论文演示、图表展示）全部恢复真实内容，零垃圾单元。
- 模型懒加载 + 进程内单例：`v2n config` 等轻量命令不触发 torch/transformers 导入。
- 原稿落盘：`TRANSCRIPT_PATH / (音频 stem + ".md")`。

### D10: LLM 阶段实现（`llm.py`）
- 客户端：`openai` SDK（OpenAI 兼容），`base_url=LLM_BASE_URL`、`api_key=LLM_API_KEY`、`model=LLM_MODEL`。
- 工具：`write_note(content: string)`——模型通过 tool calling 提交笔记 markdown，v2n 执行写入；循环处理 tool_calls 直到模型给出最终答复或达到轮次上限（防死循环）。
- 用户消息（openai 库 v1 兼容内容块数组）：STT 原稿以 file 内容块发送——`{"type": "file", "file": {"filename": "transcript.md", "file_data": "data:text/markdown;base64,<b64>"}}`（单请求，不依赖 files API）；CLI 提供 `PROMPT` 时追加独立 text 内容块 `{"type": "text", "text": "用户指示：{PROMPT}"}`，与原稿不拼接。
- 文件名：提供 `PROMPT` 时直接由 PROMPT 确定（清理非法字符 `\ / : * ? " < > |`、截断 `MAX_TITLE_LENGTH`，确定性命名）；未提供时回退取 `content` 首个非空行（去行首 `#` 与空白）同样清理截断；扩展名 `.md`；写入 `NOTE_PATH`（mkdir parents）。

### D11: 流水线编排（`cli.py` `_cmd_run`）
`load_config` → `discover_voice_files` → 逐个文件（tqdm 进度）：`transcribe`（内层按语句段更新 tqdm）→ 原稿落盘 → `generate_note`（tool calling 写笔记）。CLI 可选位置参数 `PROMPT` 透传给 `generate_note`。无匹配文件时输出提示并正常退出；配置错误在处理任何文件前 fail-fast。

- 解码循环抑制：密集语音单元仍可能偶发自回归循环（如板书停顿后"零"×512 直至 token 上限）。在 `generate` 中加 `repetition_penalty=1.2`（解码层面修复，非文本后处理）；A/B 验证：循环单元 dup 1.00→0.04 且恢复真实内容（"零一厘米…身高体重…三十八个"），英文基准句与正常中文单元质量不变。

## Risks / Trade-offs

- [uv_build 对非 Python 资源文件的打包] → assets 放在包目录内，uv_build 默认打包包目录全部文件；tasks 中加入"安装后验证模板可读取"的检查项。
- [script 名 `voice2note` → `v2n` 是 BREAKING] → 项目尚未发布，影响仅限本机已安装的 editable 环境；重新 `uv tool install` 即可。
- [config.yaml 跟随当前工作目录，用户在错误目录运行会"找不到配置"] → 报错信息明确提示"在包含 config.yaml 的目录下运行，或先执行 v2n config"。
- [PyYAML 解析用户手改的 YAML 可能出现语法错误] → 捕获解析异常，报错信息包含 YAML 错误详情与文件路径。

## Migration Plan

不适用（新能力，无存量数据迁移）。安装方式：`uv tool install --editable .`（开发）或打包后安装。

## Open Questions

无 —— LLM_MODEL 必填已与用户确认；其余歧义（模板默认值、正则示例）已在 D7 给出草案，实现阶段可微调注释措辞而不影响 spec。
