# Spec Delta

## Purpose

定义 `v2n` 命令行工具的入口与子命令行为：v2n 以 uv tool 方式安装，本期仅提供 `v2n config` 指令，用于定位（必要时创建）工作流配置文件。

## ADDED Requirements

### Requirement: v2n 以 uv tool 形式提供 CLI 入口
项目 SHALL 以 `v2n` 作为 console script 名称暴露 CLI 入口，用户可通过 `uv tool install` 安装后在任意目录直接调用 `v2n`。

#### Scenario: 安装后可调用 v2n
- **WHEN** 用户执行 `uv tool install`（本地路径或发布源）安装 voice2note 包
- **THEN** 终端中存在可执行命令 `v2n`，且 `v2n --help` 能正常输出帮助信息

### Requirement: v2n config 输出配置文件绝对路径
`v2n config` SHALL 向标准输出打印当前工作目录下 `config.yaml` 的绝对路径。

#### Scenario: config.yaml 已存在
- **WHEN** 当前工作目录下已存在 `config.yaml` 且用户执行 `v2n config`
- **THEN** 命令打印该文件的绝对路径，且不修改该文件内容

#### Scenario: config.yaml 不存在
- **WHEN** 当前工作目录下不存在 `config.yaml` 且用户执行 `v2n config`
- **THEN** 命令从内置模板创建 `config.yaml`（内容与模板一致，含默认值与注释），随后打印新创建文件的绝对路径

### Requirement: v2n config 不校验配置内容
`v2n config` SHALL 只负责定位/创建配置文件，不执行配置字段的存在性与合法性校验；校验行为属于 config 加载能力，由后续流水线阶段触发。

#### Scenario: 模板创建后未填写必填项
- **WHEN** 用户执行 `v2n config` 创建了模板文件但未填写 `LLM_API_KEY` 等必填项
- **THEN** `v2n config` 仍正常退出并打印路径，不因字段缺失而报错
