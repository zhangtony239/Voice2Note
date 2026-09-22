# Spec Delta

## Purpose

定义 v2n 的配置体系：以相对路径下的 `config.yaml` 为唯一配置文件，内置模板通过默认值与注释成为所有配置的唯一默认值源；Python 代码不得为必填字段提供备用默认值，缺失即报错；并约定基于 `VOICE_PATH` 与 `VOICE_FILE_KEYWORD` 正则的音频文件发现规则。

## ADDED Requirements

### Requirement: config.yaml 是唯一配置来源
v2n SHALL 只从当前工作目录相对路径下的 `config.yaml` 读取配置；所有可暴露的配置项都定义在该文件中，Python 代码中不得为必填字段硬编码备用默认值。

#### Scenario: 必填字段缺失时报错
- **WHEN** `config.yaml` 中缺少某个必填字段（如 `LLM_API_KEY`）且流水线阶段尝试加载配置
- **THEN** v2n 报错退出，错误信息指明缺失的字段名，而不是回退到代码内置默认值

#### Scenario: 配置文件不存在时加载配置
- **WHEN** 当前工作目录下不存在 `config.yaml` 且流水线阶段尝试加载配置
- **THEN** v2n 报错退出，错误信息提示用户先运行 `v2n config` 创建配置文件

### Requirement: 内置模板是唯一默认值源
v2n SHALL 内置一份 config 模板，模板中每个配置项通过默认值和解释性注释定义其唯一默认值；`v2n config` 创建的文件内容 SHALL 与该模板一致。

#### Scenario: 模板包含全部配置项
- **WHEN** 用户通过 `v2n config` 创建 `config.yaml` 并打开查看
- **THEN** 文件中包含以下配置项及解释注释：`VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`DISABLE_MMAP`（默认注释掉）、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`、`NOTE_PATH`、`MAX_TITLE_LENGTH`

#### Scenario: 模板默认值
- **WHEN** 用户未修改模板直接使用
- **THEN** `NOTE_PATH` 默认为 `outputs/`，`MAX_TITLE_LENGTH` 默认为 `20`，`DISABLE_MMAP` 处于注释状态

### Requirement: TORCH_BACKEND 仅接受 xpu 或 cuda
`TORCH_BACKEND` SHALL 为必填字段，取值仅允许 `xpu` 或 `cuda`；其他取值 SHALL 导致配置加载报错。

#### Scenario: 非法取值报错
- **WHEN** `config.yaml` 中 `TORCH_BACKEND` 为 `xpu` 或 `cuda` 之外的值
- **THEN** 配置加载报错，错误信息指明 `TORCH_BACKEND` 的合法取值

### Requirement: DISABLE_MMAP 按 TORCH_BACKEND 推导默认值
`DISABLE_MMAP` SHALL 为可选字段（模板中默认注释掉）：未显式配置时，`TORCH_BACKEND` 为 `xpu` 时默认开启（`true`，因 UMA 共享内存充足），为 `cuda` 时默认关闭（`false`）；显式配置时以配置值为准。

#### Scenario: xpu 下未配置 DISABLE_MMAP
- **WHEN** `TORCH_BACKEND: xpu` 且 `DISABLE_MMAP` 未在 `config.yaml` 中出现
- **THEN** 生效的 `DISABLE_MMAP` 为 `true`

#### Scenario: cuda 下未配置 DISABLE_MMAP
- **WHEN** `TORCH_BACKEND: cuda` 且 `DISABLE_MMAP` 未在 `config.yaml` 中出现
- **THEN** 生效的 `DISABLE_MMAP` 为 `false`

#### Scenario: 显式配置优先
- **WHEN** `TORCH_BACKEND: cuda` 且 `DISABLE_MMAP: true`
- **THEN** 生效的 `DISABLE_MMAP` 为 `true`

### Requirement: 音频文件发现遵循 VOICE_PATH 与 VOICE_FILE_KEYWORD
音频文件发现 SHALL 在 `VOICE_PATH` 指定的目录下进行，且仅接受文件名匹配 `VOICE_FILE_KEYWORD` 正则表达式的文件；不匹配的文件 SHALL 被忽略。

#### Scenario: 匹配正则的文件被选中
- **WHEN** `VOICE_PATH` 目录下存在文件名匹配 `VOICE_FILE_KEYWORD` 正则的音频文件
- **THEN** 该文件被纳入待处理音频列表

#### Scenario: 不匹配正则的文件被忽略
- **WHEN** `VOICE_PATH` 目录下存在文件名不匹配 `VOICE_FILE_KEYWORD` 正则的文件
- **THEN** 该文件不出现在待处理音频列表中

#### Scenario: VOICE_FILE_KEYWORD 为非法正则
- **WHEN** `VOICE_FILE_KEYWORD` 的值不是合法的正则表达式
- **THEN** 配置加载报错，错误信息指明该字段

### Requirement: LLM 阶段配置项
LLM 阶段 SHALL 从配置读取 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT` 四个必填字段；任一缺失 SHALL 导致配置加载报错。

#### Scenario: LLM 配置齐全
- **WHEN** `config.yaml` 中四个 LLM 字段均已填写
- **THEN** 配置加载成功，LLM 阶段可使用这些值建立 OpenAI 兼容 API 连接

### Requirement: 笔记输出遵循 NOTE_PATH 与 MAX_TITLE_LENGTH
笔记输出目录 SHALL 取自 `NOTE_PATH`（模板默认 `outputs/`）；笔记文件名 SHALL 取自笔记正文首行，并截断到 `MAX_TITLE_LENGTH`（模板默认 `20`）个字符。

#### Scenario: 输出到 NOTE_PATH
- **WHEN** LLM 阶段以 tool calling 方式写入笔记文件
- **THEN** 文件被写入 `NOTE_PATH` 指定的目录（目录不存在时创建）

#### Scenario: 文件名截断
- **WHEN** 笔记正文首行长度超过 `MAX_TITLE_LENGTH`
- **THEN** 文件名取首行前 `MAX_TITLE_LENGTH` 个字符
