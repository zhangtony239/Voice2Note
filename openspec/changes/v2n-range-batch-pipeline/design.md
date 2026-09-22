# Design

## Context

当前 [`src/voice2note/cli.py`](src/voice2note/cli.py) 的 `_cmd_run` 对 `discover_voice_files` 发现的每个音频文件独立执行「ASR → LLM」，`PROMPT` 为可选位置参数。`generate_note`（[`src/voice2note/llm.py`](src/voice2note/llm.py)）每次接收单份原稿，文件名由 PROMPT 或正文首行回退确定。动机见 proposal.md。

约束：
- ASR 模型加载重（torch/transformers），cli 已采用延迟导入，需保持。
- LLM 消息结构沿用「file 内容块 + 独立 text 块不拼接」的既有约定（openai v1 兼容）。
- 无测试框架与 CI；验证以 `ruff` + 手动运行为主。
- 前序变更 `init-v2n-cli-config` 尚未归档，本变更的 spec delta 以其为基线（见 proposal.md Capabilities 注）。

## Goals / Non-Goals

**Goals:**
- CLI 参数改为必填 `START-END` 与 `TITLE`，范围按文件名数字段数值匹配。
- 两阶段编排：全部 ASR 完成后，合并原稿一次性调用 LLM 产出一篇笔记。
- 保持既有进度显示（tqdm）与错误退出语义。

**Non-Goals:**
- 不改动 ASR 转写算法、配置字段与 `v2n config` 子命令。
- 不支持跨 `VOICE_PATH` 多目录、并行转写、断点续转。
- 不处理文件名含多个数字段的歧义消解（只取首个数字段）。

## Decisions

### D1. 范围解析与数字段提取放在 config.py，与 discover_voice_files 同层

- `START-END` 用 `re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", arg)` 解析为两个 `float`；解析失败或 START > END 抛 `ConfigError`（复用现有 `_dispatch` 的捕获路径，stderr + 退出码 1）。
- 新增 `select_voice_files(config, start: float, end: float) -> list[Path]`：在 `discover_voice_files` 结果上，用 `re.search(r"\d+(?:\.\d+)?", path.stem)` 提取首个数字段，按 `float` 数值落在 `[start, end]` 闭区间内过滤，并按数值升序返回；无数字段的文件不入选。
- 备选：放 cli.py —— 但范围选取属「发现/过滤」语义，与 `discover_voice_files` 同层更内聚且便于单测；放 config.py 不引入新模块。
- 数值而非字符串比较是硬需求（`9` 不落在 `10-20`；`2.10` 即 `2.1`）。

### D2. 原稿逐份以独立 file 内容块发送，llm.py 接口改为多原稿

- `_cmd_run` 收集 `(原稿文件名, 原稿文本)`，全部转写完成后一次性调用 `generate_note`；每份原稿作为**独立的 file 内容块**发送，`filename` 使用真实原稿文件名（`<音频主名>.md`），不拼接、不合并；TITLE 仍作为独立 text 块发送，全部内容块同处一次请求的同一 user 消息。
- `generate_note` 签名改为 `generate_note(transcripts: Sequence[tuple[str, str]], config, client, user_prompt)`：由「单文档进」变为「多原稿进」，file 块构造随之上移到参数层；tool calling 写盘逻辑不变。

### D3. 文件名由 TITLE 确定，TITLE 为空时回退正文首行

- `note_filename(title, config, content)`：TITLE 非空时由其确定文件名；为空时回退取正文首个非空行（去 markdown 标题记号）；两者清理后均为空则回退 `untitled`。`write_note(content, config, title)` 同步调整。
- 清理规则不变：去文件系统非法字符、截断 `MAX_TITLE_LENGTH`。
- `generate_note` 的 `user_prompt`（TITLE）保持可选：非空时作为独立 text 块发送，为空时不发送该块。

### D4. ASR 失败即中止，不调用 LLM

- `_cmd_run` 中 ASR 循环外层 `try/except Exception`：向 stderr 输出 `错误: ...` 并返回 1；已保存的原稿保留（天然幂等，重跑时同路径覆盖）。LLM 调用置于循环之后，天然满足「任一失败不调用」。
- 备选：跳过失败文件继续 —— 批次笔记缺一段原稿会静默产出残缺笔记，比显式失败更糟。

### D5. 用法错误走 argparse 原生机制

- `START-END`、`TITLE` 定义为两个必填位置参数（`nargs` 移除 `?`），缺参时 argparse 自动输出用法并以退出码 2 结束；`v2n config` 的手动分发逻辑保留不变。

## Risks / Trade-offs

- [合并文档超长超出 LLM 上下文] → 本期不做分片；范围由用户显式给定，超长属用户可感知、可缩小范围的自愈问题。design 不引入自动分批（会破坏「一篇笔记」语义）。
- [文件名数字段提取取首个，`会议录音 141.aac` 类命名若前缀含数字（如 `2024会议 141.aac`）会误取 `2024`] → 记录为已知限制；用户可通过 `VOICE_FILE_KEYWORD` 正则约束文件集，必要时后续再引入「取最后一个数字段」等策略。
- [llm delta 的 MODIFIED/REMOVED 操作在主 specs 为空时归档会被拒] → 依赖 `init-v2n-cli-config` 先归档（proposal 已注明顺序）；归档本变更前主 specs 将已存在对应能力。
- [裸 `v2n` 移除为破坏性变更] → 单用户工具，无外部调用方；README 尚为空壳，随实现补一行用法说明即可。

## Migration Plan

单机 CLI 工具，无数据迁移：实现合并入主干后，用户以新用法 `v2n START-END "TITLE"` 调用即可；旧用法（裸 `v2n` / 可选 PROMPT）直接报用法错误，无回滚需求（回滚即还原代码）。

## Open Questions

（无）