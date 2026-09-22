# Spec Delta

## Purpose

定义 LLM 阶段的行为：以 OpenAI 兼容 API 将 STT 原稿整理为笔记 markdown，通过 tool calling 写入 `NOTE_PATH`，文件名取正文首行并截断到 `MAX_TITLE_LENGTH`。

## ADDED Requirements

### Requirement: 通过 OpenAI 兼容 API 调用 LLM
LLM 阶段 SHALL 使用 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 建立 OpenAI 兼容 API 连接，并以 `SYSTEM_PROMPT` 作为系统提示词发起请求。

STT 原稿 SHALL 以 file 内容块（openai 库 v1 兼容的 `{"type": "file", "file": {"filename", "file_data"}}`，base64 data URL）作为用户消息发送，不与文本提示拼接；CLI 提供 `PROMPT` 时，PROMPT SHALL 作为同一用户消息中的独立 text 内容块发送。

#### Scenario: 请求携带配置与原稿文件
- **WHEN** LLM 阶段处理一份 STT 原稿
- **THEN** 请求使用配置中的 base_url、api_key 与 model，system 消息为 `SYSTEM_PROMPT`，user 消息包含原稿的 file 内容块

#### Scenario: PROMPT 作为独立文本块
- **WHEN** 用户以 `v2n "2.1 title"` 运行
- **THEN** user 消息中除原稿 file 内容块外，还包含 `2.1 title` 的独立 text 内容块，二者不拼接

### Requirement: 以 tool calling 方式写入笔记
LLM 阶段 SHALL 通过 tool calling 让模型提交笔记内容，由 v2n 执行实际的文件写入；笔记为 markdown 格式。

#### Scenario: 模型提交笔记内容
- **WHEN** 模型通过工具调用提交笔记 markdown 内容
- **THEN** v2n 将该内容写入 `NOTE_PATH` 下的 `.md` 文件（目录不存在时创建）

### Requirement: 笔记文件名由 PROMPT 或正文首行确定
CLI 提供 `PROMPT` 时，笔记文件名 SHALL 由 PROMPT 确定（清理文件系统非法字符并截断到 `MAX_TITLE_LENGTH` 个字符）；未提供 PROMPT 时，文件名 SHALL 取自笔记正文的首行（去除 markdown 标题记号等前导记号），同样清理非法字符并截断。

#### Scenario: PROMPT 决定文件名
- **WHEN** 用户以 `v2n "2.1 title"` 运行且模型提交笔记内容
- **THEN** 生成的文件名由 `2.1 title` 清理截断而来，与正文首行无关

#### Scenario: 无 PROMPT 时取正文首行
- **WHEN** 用户未提供 PROMPT 且笔记正文首行为 `# 会议纪要：产品周会`
- **THEN** 生成的文件名以 `会议纪要：产品周会` 为基础（不含 `#` 记号）

#### Scenario: 首行作为文件名
- **WHEN** 笔记正文首行为 `# 会议纪要：产品周会`
- **THEN** 生成的文件名以 `会议纪要：产品周会` 为基础（不含 `#` 记号）

#### Scenario: 超长截断
- **WHEN** 首行去除记号后长度超过 `MAX_TITLE_LENGTH`
- **THEN** 文件名取前 `MAX_TITLE_LENGTH` 个字符
