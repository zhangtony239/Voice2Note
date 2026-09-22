# Tasks

## 1. 范围解析与文件选取（config.py）

- [x] 1.1 在 `src/voice2note/config.py` 新增 `parse_range(arg: str) -> tuple[float, float]`：以 `re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", arg)` 解析 `START-END` 为两个 float，解析失败或 START > END 时抛 `ConfigError`；验证：临时脚本对 `141-142`、`1.2-2.4`、`abc-142`、`142-141` 的解析结果符合 design D1
- [x] 1.2 在 `src/voice2note/config.py` 新增 `select_voice_files(config, start: float, end: float) -> list[Path]`：基于 `discover_voice_files` 结果提取文件名首个数字段（`re.search(r"\d+(?:\.\d+)?", path.stem)`），按 float 数值过滤 `[start, end]` 闭区间并升序返回，无数字段的文件不入选；验证：构造临时目录（`会议录音 141.aac`、`会议录音 142.aac`、`会议录音 143.aac`、`会议录音 9.aac`、`会议录音 21.aac`、无编号文件）断言 `141-142` 只选 141/142、`10-20` 为空

## 2. LLM 模块调整（llm.py）

- [x] 2.1 修改 `src/voice2note/llm.py`：`note_filename(content, config, prompt)` 改为 `note_filename(title, config, content)`（TITLE 非空由其确定，为空回退正文首行去标题记号，均空回退 `untitled`；保留非法字符清理与 `MAX_TITLE_LENGTH` 截断），`write_note(content, config, title="")` 同步调整；验证：`ruff check` 通过，临时脚本确认 `note_filename("a/b:c", config, content)`、TITLE 为空取首行与超长截断行为正确
- [x] 2.2 修改 `generate_note`：签名改为 `generate_note(transcripts: Sequence[tuple[str, str]], config, client, user_prompt)`，每份原稿以独立 file 内容块发送（`filename` 为真实原稿文件名），`user_prompt`（TITLE）仍为独立 text 块，一次请求产出一篇笔记；验证：`ruff check` 通过，假客户端脚本确认多 file 块消息结构与 TITLE 为空时无 text 块

## 3. CLI 编排重写（cli.py）

- [x] 3.1 修改 `src/voice2note/cli.py` 的 `build_parser`：`START-END` 为必填位置参数、`TITLE` 为可选位置参数（`nargs="?"` 默认空），更新模块与 help 文案；验证：`v2n`（无参）输出 argparse 用法错误且退出码非零，`v2n 141-142` 可进入流水线，`v2n config` 子命令不受影响
- [x] 3.2 重写 `_cmd_run`：`parse_range` → `select_voice_files`（为空时提示并返回 0）→ 循环 `transcribe` + `save_transcript` 收集 `(原稿文件名, 原稿文本)`（ASR 循环外层 `try/except Exception`，stderr 输出并返回 1）→ 全部完成后单次调用 `generate_note(原稿列表, config, user_prompt=TITLE)`（每份原稿独立 file 块，见 design D2）；验证：`ruff check` 通过，`abc-142`/`142-141` 报 ConfigError 退出码 1
- [x] 3.3 保持 tqdm 进度显示（外层按选取文件列表，语句级进度工厂不变），转写与笔记路径输出沿用现有 `tqdm.write` 风格；验证：代码审读确认两阶段输出顺序为「原稿×N → 笔记×1」

## 4. 文档与收尾

- [x] 4.1 在 `README.md` 补充新用法说明（`v2n START-END "TITLE"` 示例与两阶段行为一句话说明）；验证：README 内容可读且与 spec 一致
- [x] 4.2 运行 `uv run ruff check` 与 `uv run v2n --help` 确认整体无回归；验证：两条命令均成功且 help 文案展示 START-END 必填、TITLE 可选
- [ ] 4.3 手动端到端验证（需真实配置与音频）：在含 `会议录音 141.aac`、`会议录音 142.aac` 的目录执行 `v2n 141-142 "测试纪要"`，确认先产出两份原稿 md、后产出一篇 `测试纪要.md`，且 LLM 仅收到一次请求；验证：`TRANSCRIPT_PATH` 下两份原稿、`NOTE_PATH` 下一篇笔记