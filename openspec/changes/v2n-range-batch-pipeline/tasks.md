# Tasks

## 1. 范围解析与文件选取（config.py）

- [ ] 1.1 在 `src/voice2note/config.py` 新增 `parse_range(arg: str) -> tuple[float, float]`：以 `re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", arg)` 解析 `START-END` 为两个 float，解析失败或 START > END 时抛 `ConfigError`；验证：临时脚本对 `141-142`、`1.2-2.4`、`abc-142`、`142-141` 的解析结果符合 design D1
- [ ] 1.2 在 `src/voice2note/config.py` 新增 `select_voice_files(config, start: float, end: float) -> list[Path]`：基于 `discover_voice_files` 结果提取文件名首个数字段（`re.search(r"\d+(?:\.\d+)?", path.stem)`），按 float 数值过滤 `[start, end]` 闭区间并升序返回，无数字段的文件不入选；验证：构造临时目录（`会议录音 141.aac`、`会议录音 142.aac`、`会议录音 143.aac`、`会议录音 9.aac`、`会议录音 21.aac`、无编号文件）断言 `141-142` 只选 141/142、`10-20` 为空

## 2. LLM 模块调整（llm.py）

- [ ] 2.1 修改 `src/voice2note/llm.py`：`note_filename(content, config, prompt)` 改为 `note_filename(title, config)`（移除正文首行回退，保留非法字符清理与 `MAX_TITLE_LENGTH` 截断、空值回退 `untitled`），`write_note(content, config, title)` 同步调整；验证：`ruff check` 通过，临时脚本确认 `note_filename("a/b:c", config)` 与超长截断行为正确
- [ ] 2.2 更新 `generate_note` 文档字符串与 file 内容块 `filename` 语义（`transcript` 为合并文档、`user_prompt` 为 TITLE），消息结构（file 块 + 独立 text 块）保持不变；验证：`ruff check` 通过，代码审读确认无行为性改动

## 3. CLI 编排重写（cli.py）

- [ ] 3.1 修改 `src/voice2note/cli.py` 的 `build_parser`：`START-END` 与 `TITLE` 改为两个必填位置参数（移除 `nargs="?"` 的 `prompt`），更新模块与 help 文案；验证：`v2n`（无参）与 `v2n 141-142` 输出 argparse 用法错误且退出码非零，`v2n config` 子命令不受影响
- [ ] 3.2 重写 `_cmd_run`：`parse_range` → `select_voice_files`（为空时提示并返回 0）→ 循环 `transcribe` + `save_transcript` 收集 `(文件名, 原稿文本)`（ASR 循环外层 `try/except Exception`，stderr 输出并返回 1）→ 全部完成后按 design D2 拼接合并文档（每份前加 `## <文件名去扩展名>`）→ 单次调用 `generate_note(合并文档, config, user_prompt=TITLE)`；验证：`ruff check` 通过
- [ ] 3.3 保持 tqdm 进度显示（外层按选取文件列表，语句级进度工厂不变），转写与笔记路径输出沿用现有 `tqdm.write` 风格；验证：代码审读确认两阶段输出顺序为「原稿×N → 笔记×1」

## 4. 文档与收尾

- [ ] 4.1 在 `README.md` 补充新用法说明（`v2n START-END "TITLE"` 示例与两阶段行为一句话说明）；验证：README 内容可读且与 spec 一致
- [ ] 4.2 运行 `uv run ruff check` 与 `uv run v2n --help` 确认整体无回归；验证：两条命令均成功且 help 文案展示两个必填位置参数
- [ ] 4.3 手动端到端验证（需真实配置与音频）：在含 `会议录音 141.aac`、`会议录音 142.aac` 的目录执行 `v2n 141-142 "测试纪要"`，确认先产出两份原稿 md、后产出一篇 `测试纪要.md`，且 LLM 仅收到一次请求；验证：`TRANSCRIPT_PATH` 下两份原稿、`NOTE_PATH` 下一篇笔记