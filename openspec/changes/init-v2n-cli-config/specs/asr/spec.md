# Spec Delta

## Purpose

定义 ASR 阶段的行为：以固定模型（Fun-ASR-Nano）将发现的音频文件转写为 STT 原稿，设备与加载方式由配置驱动，原稿落盘到 `TRANSCRIPT_PATH`。

## ADDED Requirements

### Requirement: ASR 模型固定为 Fun-ASR-Nano
ASR 阶段 SHALL 使用固定的 Fun-ASR-Nano 模型（model_id 与 revision 写死在代码中，不暴露配置）；加载设备 SHALL 取自 `TORCH_BACKEND`，权重加载方式 SHALL 遵循 `DISABLE_MMAP` 的生效值。

#### Scenario: 按 TORCH_BACKEND 加载模型
- **WHEN** `TORCH_BACKEND: cuda` 且流水线开始转写
- **THEN** 模型被加载到 cuda 设备上执行推理

#### Scenario: DISABLE_MMAP 生效
- **WHEN** `TORCH_BACKEND: xpu` 且 `DISABLE_MMAP` 未显式配置
- **THEN** 模型与 processor 以禁用 mmap 的方式加载（生效值为 true）

### Requirement: 按 ASR_LANGUAGE 转写音频
ASR 阶段 SHALL 以 `ASR_LANGUAGE` 指定的语言对每个发现的音频文件执行转写，产出该音频的 STT 原稿文本。

#### Scenario: 转写单个音频文件
- **WHEN** 音频文件被纳入待处理列表且 `ASR_LANGUAGE: zh`
- **THEN** ASR 阶段产出该音频的中文 STT 原稿文本

### Requirement: STT 原稿落盘到 TRANSCRIPT_PATH
每个音频文件的 STT 原稿 SHALL 写入 `TRANSCRIPT_PATH` 指定的目录（目录不存在时创建），文件名与对应音频文件的主名保持一致。

#### Scenario: 原稿文件命名
- **WHEN** 音频文件 `meeting.mp3` 转写完成
- **THEN** `TRANSCRIPT_PATH` 下出现以 `meeting` 为主名的原稿文件，内容为转写文本
