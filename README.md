# Voice2Note (v2n)

从原始录音到笔记 markdown 的工作流：ASR 转写（Fun-ASR-Nano）→ LLM 整理（OpenAI 兼容 API + tool calling）。

## 安装

```bash
uv tool install .
```

## 用法

```bash
# 定位（必要时从模板创建）config.yaml
v2n config

# 运行流水线：按编号范围批处理
v2n START-END ["TITLE"]
```

- `START-END`：音频文件编号范围（如 `141-142`、`1.2-2.4`）。按 `VOICE_PATH` 下匹配 `VOICE_FILE_KEYWORD` 的文件名中**首个数字段的数值**选取闭区间内的文件（数值比较，非字符串），按编号升序处理。
- `TITLE`（可选）：笔记标题，决定笔记文件名，非空时随原稿发送给 LLM；省略时文件名取笔记正文首行。

示例：

```bash
v2n 141-142 "会议纪要"   # 处理 会议录音 141.aac 与 会议录音 142.aac，产出一篇 会议纪要.md
v2n 1.2-2.4             # 处理 会议1.2 录音a.wav 至 会议2.4 录音b.wav，文件名取正文首行
```

流水线为两阶段批处理：先对范围内全部文件逐一 ASR 并在 `TRANSCRIPT_PATH` 下各保存一份 STT 原稿 md；全部转写完成后，将各份原稿一次性发送给 LLM（每份原稿为独立 file 内容块），在 `NOTE_PATH` 下生成一篇笔记。任一文件转写失败即中止，不调用 LLM。

## 配置

`config.yaml`（当前工作目录）为唯一配置来源，字段说明见 `v2n config` 生成的模板注释。
