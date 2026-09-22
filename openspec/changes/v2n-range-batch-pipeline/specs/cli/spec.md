# Spec Delta

## REMOVED Requirements

### Requirement: v2n 接受可选 PROMPT 参数
**Reason**: CLI 入口改为 `v2n START-END ["TITLE"]`：PROMPT 位置参数被 TITLE 取代，承担「随原稿发送给 LLM」与「决定笔记文件名」职责；TITLE 可空，为空时回退正文首行命名。
**Migration**: 改用 `v2n START-END ["TITLE"]`；原 `v2n "2.1 title"` 的意图由 `v2n 2.1-2.1 "title"` 表达。

### Requirement: 裸 v2n 运行完整流水线
**Reason**: 批处理语义要求显式给出文件范围与笔记标题，无参数运行无法表达，且逐文件独立生成笔记的旧行为被「全部 ASR 完成后一次性生成一篇笔记」取代。
**Migration**: 使用 `v2n START-END "TITLE"` 运行完整流水线。

## ADDED Requirements

### Requirement: v2n 以 START-END 与 TITLE 运行完整流水线
`v2n` SHALL 接受位置参数 `START-END`（音频文件编号范围，必填）与 `TITLE`（笔记标题，可选，默认为空）。执行顺序 SHALL 为：加载配置 → 按 `START-END` 选取音频文件 → 对范围内每个文件执行 ASR 转写并在 `TRANSCRIPT_PATH` 下保存 STT 原稿 → 全部转写完成后将所有原稿一次性发送给 LLM → 在 `NOTE_PATH` 下生成一篇笔记 markdown。缺少 `START-END` 时 SHALL 输出用法错误并以非零退出码退出，不开始处理任何音频文件。

#### Scenario: 正常运行
- **WHEN** 当前目录存在合法 `config.yaml`，`VOICE_PATH` 下存在编号落在 `START-END` 内的音频文件，用户执行 `v2n 141-142 "会议纪要"`
- **THEN** 范围内每个音频文件在 `TRANSCRIPT_PATH` 下各产出一份 STT 原稿，全部转写完成后在 `NOTE_PATH` 下产出一篇笔记 markdown

#### Scenario: 缺少位置参数
- **WHEN** 用户执行 `v2n`（无参数）
- **THEN** v2n 输出用法错误并以非零退出码退出，不调用 ASR 与 LLM

#### Scenario: 省略 TITLE
- **WHEN** 用户执行 `v2n 141-142`（未提供 TITLE）
- **THEN** 流水线正常运行，笔记文件名取笔记正文首行（去除 markdown 标题记号）

#### Scenario: 配置非法
- **WHEN** `config.yaml` 缺失或含非法字段，用户执行 `v2n 141-142 "标题"`
- **THEN** v2n 向 stderr 输出错误信息并以非零退出码退出，不开始处理任何音频文件

### Requirement: START-END 按文件名数字段范围选取音频文件
`v2n` SHALL 在 `VOICE_PATH` 下发现匹配 `VOICE_FILE_KEYWORD` 的音频文件后，从每个文件名中提取首个数字段（可含小数点，如 `141`、`1.2`），并将该数字段按数值（整数/浮点数）而非字符串与 START、END 比较；SHALL 选取数值落在 `[START, END]` 闭区间内的所有文件，并按数值升序处理。`START-END` 无法解析为数字段，或 START 数值大于 END 时，SHALL 输出错误信息并以非零退出码退出。选取结果为空时 SHALL 输出提示信息并正常退出，不调用 ASR 与 LLM。

#### Scenario: 整数编号范围
- **WHEN** `VOICE_PATH` 下存在 `会议录音 141.aac`、`会议录音 142.aac`、`会议录音 143.aac`，用户执行 `v2n 141-142 "标题"`
- **THEN** 仅选取 `会议录音 141.aac` 与 `会议录音 142.aac`，按 141 → 142 的顺序处理

#### Scenario: 小数编号范围
- **WHEN** `VOICE_PATH` 下存在 `会议1.2 录音a.wav` 至 `会议2.4 录音b.wav` 的多个文件，用户执行 `v2n 1.2-2.4 "标题"`
- **THEN** 选取编号数值在 1.2 与 2.4 之间（含端点）的所有文件，按编号数值升序处理

#### Scenario: 按数值而非字符串比较
- **WHEN** `VOICE_PATH` 下存在 `会议录音 9.aac` 与 `会议录音 21.aac`，用户执行 `v2n 10-20 "标题"`
- **THEN** 两个文件均不入选（9 < 10，21 > 20；字符串比较会误判）

#### Scenario: 范围格式非法
- **WHEN** 用户执行 `v2n abc-142 "标题"` 或 `v2n 142-141 "标题"`
- **THEN** v2n 输出错误信息并以非零退出码退出，不调用 ASR 与 LLM

#### Scenario: 范围内无匹配文件
- **WHEN** `VOICE_PATH` 下没有编号落在 `START-END` 内的音频文件，用户执行 `v2n 300-400 "标题"`
- **THEN** v2n 输出提示信息并正常退出（退出码 0），不调用 ASR 与 LLM

### Requirement: 全部 ASR 完成后一次性调用 LLM
`v2n` SHALL 在范围内全部文件的 STT 原稿均保存完成后，才调用 LLM；LLM SHALL 只被调用一次，一次请求涵盖全部原稿（每份原稿为独立 file 内容块），并只产出一篇笔记。范围内任一文件 ASR 失败时，v2n SHALL 向 stderr 输出错误信息并以非零退出码退出，不调用 LLM。

#### Scenario: 两阶段顺序执行
- **WHEN** 范围内有两个音频文件，用户执行 `v2n 141-142 "标题"`
- **THEN** 先后完成两个文件的转写并各产出一份原稿 md，之后才发起 LLM 请求并产出一篇笔记

#### Scenario: 单次 LLM 请求包含全部原稿
- **WHEN** 范围内两个文件的转写均完成
- **THEN** LLM 恰好收到一次请求，其内容涵盖两份原稿的文本

#### Scenario: ASR 失败时不调用 LLM
- **WHEN** 范围内某个文件转写过程中抛出异常
- **THEN** v2n 向 stderr 输出错误信息并以非零退出码退出，已完成的文件原稿保留，不发起 LLM 请求