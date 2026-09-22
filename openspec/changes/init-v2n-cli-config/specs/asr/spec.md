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

### Requirement: 密集语音单元切分转写
模型按短话语训练，实测解码单元混入大段静音会触发重复幻觉（>45s 开始幻觉，>122s 超位置嵌入上限报错）。因此 SHALL 以自适应能量阈值检测语音区（容忍 <0.4s 短停顿），语音区边缘仅保留 0.15s 呼吸空间，使解码单元只含密集语音；句间停顿 ≤0.5s 的相邻语音区可合并（合并后 ≤30s）；<1s 的微单元并入相邻单元；单元仍超 30s 时按音量感知（目标位置之前最安静的窗口）保底硬切。

#### Scenario: 长音频逐句转写
- **WHEN** 音频时长超过 30s（如 45 分钟录音）
- **THEN** 音频被切分为多个 ≤30s 的密集语音单元分别转写，拼接为一份 STT 原稿，不发生张量尺寸错误或重复幻觉

#### Scenario: 静音不进入解码单元
- **WHEN** 说话人长时间停顿（翻页、演示操作）
- **THEN** 停顿音频不并入任何解码单元，不诱发重复幻觉

#### Scenario: 持续发言无停顿
- **WHEN** 单条语句超过 30s 且无静音句界
- **THEN** 在目标位置之前选取最安静的窗口做保底硬切

### Requirement: STT 原稿落盘到 TRANSCRIPT_PATH
每个音频文件的 STT 原稿 SHALL 写入 `TRANSCRIPT_PATH` 指定的目录（目录不存在时创建），文件名与对应音频文件的主名保持一致。

#### Scenario: 原稿文件命名
- **WHEN** 音频文件 `meeting.mp3` 转写完成
- **THEN** `TRANSCRIPT_PATH` 下出现以 `meeting` 为主名的原稿文件，内容为转写文本
