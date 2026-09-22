# Tasks

## 1. 包与依赖准备

- [x] 1.1 在 `pyproject.toml` 中新增 `pyyaml` 依赖并运行 `uv sync`，验证 `uv lock` 更新且安装成功
- [x] 1.2 将 `[project.scripts]` 的入口从 `voice2note = "voice2note:main"` 改为 `v2n = "voice2note.cli:main"`，验证 `pyproject.toml` 语法正确（`uv sync` 通过）

## 2. 配置模板

- [x] 2.1 创建 `src/voice2note/assets/config-template.yaml`，内容按 design.md D7 草案：含 `VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`DISABLE_MMAP`（注释态）、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`、`NOTE_PATH: outputs/`、`MAX_TITLE_LENGTH: 20`，每个字段带解释注释；验证文件存在且为合法 YAML
- [x] 2.2 确认 uv_build 会将 assets 目录随包安装：`uv build` 产物 wheel 中包含 `voice2note/assets/config-template.yaml`（editable 安装经 `.pth` 直接引用 `src/`，不复制进 site-packages）

## 3. 配置加载模块

- [x] 3.1 实现 `src/voice2note/config.py` 中的 `ConfigError` 与 `load_config()`：读取 `./config.yaml`，文件不存在时报错并提示运行 `v2n config`；YAML 语法错误时报错并包含原始错误信息；验证：临时目录无 config.yaml 时调用抛 `ConfigError`
- [x] 3.2 实现必填字段校验（`VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`，以及模板默认字段 `NOTE_PATH`、`MAX_TITLE_LENGTH`）：缺失/为空时一次性列出全部缺失字段并抛 `ConfigError`，代码中无任何备用默认值；验证：删除任一字段后加载报错且信息含字段名
- [x] 3.3 实现 `TORCH_BACKEND` 白名单校验（仅 `xpu`/`cuda`）与 `VOICE_FILE_KEYWORD` 正则合法性校验（`re.compile` 失败即报错）；验证：非法取值各自触发 `ConfigError`
- [x] 3.4 实现 `DISABLE_MMAP` 推导：未配置时 xpu→`True`、cuda→`False`；显式配置优先；验证：三种场景（xpu 未配置 / cuda 未配置 / cuda 显式 true）结果正确
- [x] 3.5 实现 `discover_voice_files(config)`：遍历 `VOICE_PATH`，文件名 `re.search` 匹配 `VOICE_FILE_KEYWORD` 者入选；验证：临时目录放入匹配/不匹配文件各一，仅匹配者返回

## 4. CLI 入口

- [x] 4.1 实现 `src/voice2note/cli.py`：argparse 定义 `v2n config` 子命令；`config` 子命令检查 `./config.yaml`，不存在则用 `importlib.resources` 读取模板并**原样复制**（保留注释）创建，然后打印绝对路径；已存在时直接打印绝对路径且不改动文件；验证：空目录运行 `v2n config` 创建文件并输出路径，再次运行输出同一路径且文件内容不变
- [x] 4.2 CLI 层捕获 `ConfigError`/`OSError`，向 stderr 输出错误并以非零退出码退出；验证：构造异常场景时退出码非 0 且 stderr 有信息

## 5. 集成验证

- [x] 5.1 `uv sync` 后验证 `v2n --help` 与 `v2n config` 在全新目录下端到端可用；验证：终端输出 config.yaml 绝对路径（临时沙箱目录已清理）
- [x] 5.2 用 `ruff check` 与 `ruff format --check` 检查新增代码，修复所有告警

## 6. 范围扩展：流水线配置与模板

- [x] 6.1 模板新增 `ASR_LANGUAGE: zh` 与 `TRANSCRIPT_PATH: .transcripts/`（含解释注释）；验证：`v2n config` 重建的文件包含这两个字段
- [x] 6.2 `config.py` 的 `REQUIRED_FIELDS` 与 `Config` 增加 `asr_language`、`transcript_path`；验证：删除字段后加载报错且信息含字段名
- [x] 6.3 `pyproject.toml` 新增 `openai`、`tqdm` 依赖并 `uv sync`；验证：解析与安装成功

## 7. ASR 阶段

- [x] 7.1 实现 `src/voice2note/asr.py`：固定 Fun-ASR-Nano（model_id/revision 写死），按 `TORCH_BACKEND` 加载（bfloat16、`disable_mmap` 透传），懒加载单例；验证：`v2n config` 路径不触发 torch 导入
- [x] 7.2 实现 `transcribe(audio_path, config)`：`apply_transcription_request(audio=本地路径, language=ASR_LANGUAGE)` + `generate(max_new_tokens=512, do_sample=False)`，输入 `.to(TORCH_BACKEND)` 消除设备不匹配警告；验证：HF 示例音频转写出正确文本
- [x] 7.3 原稿落盘：写入 `TRANSCRIPT_PATH/(stem).md`（目录不存在时创建）；验证：转写后 `.transcripts/en.md` 生成且内容一致

## 8. LLM 阶段

- [x] 8.1 实现 `src/voice2note/llm.py`：openai SDK（base_url/api_key/model），system=`SYSTEM_PROMPT`、user=STT 原稿；验证：模块可导入、参数组装正确（假 client 单测通过）
- [x] 8.2 实现 `write_note` tool calling 循环：模型提交 content → v2n 写文件 → 回传 tool 结果，直到模型给出最终答复或轮次上限；验证：假 client 场景下文件被写入
- [x] 8.3 实现文件名规则：首行去 `#` 记号与空白、清理非法字符 `\ / : * ? " < > |`、截断 `MAX_TITLE_LENGTH`、扩展名 `.md`，写入 `NOTE_PATH`；验证：`# 会议纪要：产品周会` → `会议纪要：产品周会.md`，超长截断生效

## 9. 流水线编排与集成验证

- [x] 9.1 `cli.py` 支持裸 `v2n`：无子命令时执行 `load_config → discover_voice_files → 逐文件 transcribe+落盘 → generate_note`（进度走 tqdm）；无匹配文件时提示并正常退出；配置错误 fail-fast；验证：`v2n --help` 仍可用、无音频目录运行提示正常
- [ ] 9.2 端到端验证：真实音频 + 真实 LLM 配置跑通 `v2n`，产出原稿与笔记；验证：`TRANSCRIPT_PATH` 与 `NOTE_PATH` 下各有产物
- [x] 9.4 `v2n [PROMPT]`：可选位置参数以独立 text 内容块随原稿 file 内容块发送（不拼接）并直接决定文件名（清理+截断）；无 PROMPT 回退正文首行命名；验证：假 client 两场景文件名正确
- [x] 9.5 AAC/M4A 支持：librosa 无法解码的格式经 PyAV（FFmpeg）解码为 16kHz float32 数组再传入 processor（Windows 无 torchcodec 轮子）；验证：av 生成 aac → 解码 → 转写全链路通过
- [x] 9.3 `ruff check` 与 `ruff format --check` 全部通过
