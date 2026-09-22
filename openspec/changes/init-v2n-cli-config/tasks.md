# Tasks

## 1. 包与依赖准备

- [x] 1.1 在 `pyproject.toml` 中新增 `pyyaml` 依赖并运行 `uv sync`，验证 `uv lock` 更新且安装成功
- [x] 1.2 将 `[project.scripts]` 的入口从 `voice2note = "voice2note:main"` 改为 `v2n = "voice2note.cli:main"`，验证 `pyproject.toml` 语法正确（`uv sync` 通过）

## 2. 配置模板

- [x] 2.1 创建 `src/voice2note/assets/config-template.yaml`，内容按 design.md D7 草案：含 `VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`DISABLE_MMAP`（注释态）、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`、`NOTE_PATH: outputs/`、`MAX_TITLE_LENGTH: 20`，每个字段带解释注释；验证文件存在且为合法 YAML
- [x] 2.2 确认 uv_build 会将 assets 目录随包安装：`uv build` 产物 wheel 中包含 `voice2note/assets/config-template.yaml`（editable 安装经 `.pth` 直接引用 `src/`，不复制进 site-packages）

## 3. 配置加载模块

- [ ] 3.1 实现 `src/voice2note/config.py` 中的 `ConfigError` 与 `load_config()`：读取 `./config.yaml`，文件不存在时报错并提示运行 `v2n config`；YAML 语法错误时报错并包含原始错误信息；验证：临时目录无 config.yaml 时调用抛 `ConfigError`
- [ ] 3.2 实现必填字段校验（`VOICE_PATH`、`VOICE_FILE_KEYWORD`、`TORCH_BACKEND`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`，以及模板默认字段 `NOTE_PATH`、`MAX_TITLE_LENGTH`）：缺失/为空时一次性列出全部缺失字段并抛 `ConfigError`，代码中无任何备用默认值；验证：删除任一字段后加载报错且信息含字段名
- [ ] 3.3 实现 `TORCH_BACKEND` 白名单校验（仅 `xpu`/`cuda`）与 `VOICE_FILE_KEYWORD` 正则合法性校验（`re.compile` 失败即报错）；验证：非法取值各自触发 `ConfigError`
- [ ] 3.4 实现 `DISABLE_MMAP` 推导：未配置时 xpu→`True`、cuda→`False`；显式配置优先；验证：三种场景（xpu 未配置 / cuda 未配置 / cuda 显式 true）结果正确
- [ ] 3.5 实现 `discover_voice_files(config)`：遍历 `VOICE_PATH`，文件名 `re.search` 匹配 `VOICE_FILE_KEYWORD` 者入选；验证：临时目录放入匹配/不匹配文件各一，仅匹配者返回

## 4. CLI 入口

- [ ] 4.1 实现 `src/voice2note/cli.py`：argparse 定义 `v2n config` 子命令；`config` 子命令检查 `./config.yaml`，不存在则用 `importlib.resources` 读取模板并**原样复制**（保留注释）创建，然后打印绝对路径；已存在时直接打印绝对路径且不改动文件；验证：空目录运行 `v2n config` 创建文件并输出路径，再次运行输出同一路径且文件内容不变
- [ ] 4.2 CLI 层捕获 `ConfigError`/`OSError`，向 stderr 输出错误并以非零退出码退出；验证：构造异常场景时退出码非 0 且 stderr 有信息

## 5. 集成验证

- [ ] 5.1 `uv tool install --editable .`（或 `uv sync` 后 `uv run v2n`）验证 `v2n --help` 与 `v2n config` 在全新目录下端到端可用；验证：终端输出 config.yaml 绝对路径
- [ ] 5.2 用 `ruff check` 与 `ruff format --check` 检查新增代码，修复所有告警
