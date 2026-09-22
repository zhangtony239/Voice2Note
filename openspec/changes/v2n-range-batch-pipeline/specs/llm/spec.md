# Spec Delta

## MODIFIED Requirements

### Requirement: 通过 OpenAI 兼容 API 调用 LLM
LLM 阶段 SHALL 使用 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 建立 OpenAI 兼容 API 连接，并以 `SYSTEM_PROMPT` 作为系统提示词发起请求。

合并后的 STT 原稿 SHALL 以 file 内容块（openai 库 v1 兼容的 `{"type": "file", "file": {"filename", "file_data"}}`，base64 data URL）作为用户消息发送，不与文本提示拼接；CLI 提供 `TITLE` 时，TITLE SHALL 作为同一用户消息中的独立 text 内容块发送。一次笔记生成 SHALL 只发起一轮请求，请求内容涵盖批次内全部原稿。

#### Scenario: 请求携带配置与合并原稿文件
- **WHEN** LLM 阶段处理一个包含多份 STT 原稿的批次
- **THEN** 请求使用配置中的 base_url、api_key 与 model，system 消息为 `SYSTEM_PROMPT`，user 消息包含合并后原稿的 file 内容块

#### Scenario: TITLE 作为独立文本块
- **WHEN** 用户以 `v2n 141-142 "会议纪要"` 运行
- **THEN** user 消息中除合并原稿 file 内容块外，还包含 `会议纪要` 的独立 text 内容块，二者不拼接

## REMOVED Requirements

### Requirement: 笔记文件名由 PROMPT 或正文首行确定
**Reason**: CLI 改为必填 `TITLE` 位置参数，文件名来源唯一化；「未提供 PROMPT 时取正文首行」的回退命名随裸 `v2n` 用法一并移除。
**Migration**: 笔记文件名一律由 `TITLE` 清理非法字符并截断到 `MAX_TITLE_LENGTH` 后确定。

## ADDED Requirements

### Requirement: 笔记文件名由 TITLE 确定
笔记文件名 SHALL 由 CLI 的 `TITLE` 确定：清理文件系统非法字符并截断到 `MAX_TITLE_LENGTH` 个字符，与笔记正文首行无关。

#### Scenario: TITLE 决定文件名
- **WHEN** 用户以 `v2n 141-142 "产品周会纪要"` 运行且模型提交笔记内容
- **THEN** 生成的文件名为 `产品周会纪要.md`，与正文首行无关

#### Scenario: 非法字符清理与超长截断
- **WHEN** TITLE 含文件系统非法字符（如 `a/b:c`）或长度超过 `MAX_TITLE_LENGTH`
- **THEN** 文件名移除非法字符，且不超过 `MAX_TITLE_LENGTH` 个字符