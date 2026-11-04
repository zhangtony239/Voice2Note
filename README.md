# Voice2Note (v2n)

从原始录音到笔记 markdown 的工作流：ASR 转写（Fun-ASR-Nano）→ LLM 整理（OpenAI 兼容 API + tool calling）；同时提供面向手机端 RecNote 的远程识别常驻服务 `v2n rec-daemon`。

## 安装

```bash
uv tool install .
```

## 批处理用法（`v2n START-END`）

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

### 批处理配置

`config.yaml`（当前工作目录）为批处理唯一配置来源，字段说明见 `v2n config` 生成的模板注释。

---

## 远程识别常驻服务（`v2n rec-daemon`）

`v2n rec-daemon` 面向手机端 RecNote 提供远程语音识别能力：手机录制并上传约 30 秒 PCM WAV 音频块到共享云盘，电脑端 `v2n rec-daemon` 通过本机 OpenList WebDAV 拉取音频块、运行 Fun-ASR-Nano 识别并将原稿写回云盘。电脑端只负责语音识别，不保存任何大模型配置、不发起大模型请求、不生成笔记。

### 快速启动

```bash
# 1. 从模板生成 rec-daemon.yaml 并打印其绝对路径
v2n rec-daemon init-config --config rec-daemon.yaml

# 2. 生成连接 token、其 SHA-256 摘要与任务密钥（只打印，不修改文件）
v2n rec-daemon token

# 3. 将输出的 SERVER_AUTH_TOKEN_HASH、SESSION_TOKEN_SECRET 及 OpenList 凭据填入 rec-daemon.yaml 后，在前台启动服务
v2n rec-daemon --config rec-daemon.yaml
```

支持通过命令行参数覆盖监听地址、端口或日志级别：

```bash
v2n rec-daemon --config rec-daemon.yaml --host 127.0.0.1 --port 8765 --log-level INFO
```

### 服务配置说明（`rec-daemon.yaml`）

`rec-daemon.yaml` 与批处理使用的 `config.yaml` 完全独立，共 22 个字段（不含任何 LLM 字段；除 `DISABLE_MMAP` 可按计算后端自动推导外，其余 21 个字段均为必填）：

| 分组 | 字段名 | 说明 |
| --- | --- | --- |
| 识别 | `TORCH_BACKEND` | 计算后端，仅允许 `xpu` 或 `cuda` |
| 识别 | `ASR_LANGUAGE` | 语音识别语言，如 `zh` / `en` |
| 识别 | `DISABLE_MMAP` | 是否禁用 mmap 加载权重（可选；`xpu` 默认 `true`，`cuda` 默认 `false`） |
| 监听与鉴权 | `SERVER_HOST` | HTTP 控制面监听地址（默认 `127.0.0.1`） |
| 监听与鉴权 | `SERVER_PORT` | HTTP 控制面监听端口（`1`–`65535`，默认 `8765`） |
| 监听与鉴权 | `SERVER_AUTH_TOKEN_HASH` | 连接 token 的摘要，格式固定为 `sha256:<64位小写十六进制>` |
| 监听与鉴权 | `SESSION_TOKEN_SECRET` | 任务 token 派生密钥（HMAC-SHA256，长度至少 32 字符） |
| 云盘 | `OPENLIST_WEBDAV_URL` | 本机 OpenList WebDAV 根地址（如 `http://127.0.0.1:5244/dav`） |
| 云盘 | `OPENLIST_USERNAME` | OpenList WebDAV 用户名 |
| 云盘 | `OPENLIST_PASSWORD` | OpenList WebDAV 密码 |
| 云盘 | `OPENLIST_TIMEOUT_SECONDS` | WebDAV 单次请求超时秒数（正整数） |
| 云盘 | `OPENLIST_VERIFY_TLS` | 是否校验证书（`true` / `false`） |
| 云盘 | `CLOUD_ROOT_PREFIX` | 云盘任务根目录前缀（相对云盘根，默认 `RecNoteRemote`） |
| 存储 | `SESSION_STATE_PATH` | 任务状态文件目录（相对路径按配置文件所在目录解析） |
| 存储 | `SESSION_WORK_PATH` | 任务本地工作目录（相对路径按配置文件所在目录解析） |
| 调度 | `POLL_INTERVAL_SECONDS` | 初始轮询间隔秒数（正整数） |
| 调度 | `POLL_MAX_INTERVAL_SECONDS` | 指数退避最大轮询间隔秒数（正整数，且 `>= POLL_INTERVAL_SECONDS`） |
| 调度 | `ASR_MAX_CONCURRENCY` | 识别并发上限（必须固定为 `1`） |
| 调度 | `MAX_ACTIVE_SESSIONS` | 同时活动的最大任务数（正整数） |
| 保护与诊断 | `MAX_AUDIO_CHUNK_BYTES` | 单个音频块最大字节数（正整数，默认 `16777216` 即 16 MiB） |
| 保护与诊断 | `SESSION_IDLE_WARN_HOURS` | 任务空闲告警小时数（正整数，仅记录日志不改变状态） |
| 保护与诊断 | `LOG_LEVEL` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

### Windows NSSM 常驻服务部署说明

在 Windows 上推荐使用 NSSM 将 `v2n rec-daemon` 注册为后台系统服务：

1. **可执行文件与参数**
   - **Path（可执行文件）**：指向虚拟环境或全局安装的 `v2n.exe`（例如 `C:\repos\Voice2Note\.venv\Scripts\v2n.exe`）。
   - **Arguments（参数）**：`rec-daemon --config C:\work\rec-daemon.yaml`（建议传入配置文件的绝对路径）。
   - **Startup directory（工作目录）**：`C:\work`（存储路径 `SESSION_STATE_PATH` 与 `SESSION_WORK_PATH` 按配置文件所在目录解析，不依赖当前工作目录）。
2. **单实例约束**
   - 仅注册并运行**单个** `v2n rec-daemon` 服务实例，内部固定单进程单 worker（`workers=1`、`ASR_MAX_CONCURRENCY=1`），避免多实例争抢显存或重复轮询同一任务状态目录。
3. **日志重定向（I/O 与 Rotation）**
   - `v2n rec-daemon` 将结构化脱敏日志统一输出到标准错误（`stderr`）。
   - 在 NSSM 的 **I/O** 选项卡中将 `Output (stdout)` 与 `Error (stderr)` 重定向到日志文件（如 `C:\work\logs\rec-daemon.log`），并在 **File rotation** 选项卡启用按体积自动切分。
4. **崩溃重启与任务恢复（Exit actions）**
   - 在 NSSM 的 **Exit actions** 选项卡设置重启策略（Restart application，延迟 5000 ms）。
   - 服务重启后会自动扫描 `SESSION_STATE_PATH` 下的任务状态文件，重算并恢复原有的 `session_token`、保留已完成块与结果索引版本号，继续处理未完成或收尾中的录音任务。

```powershell
nssm install V2NRecDaemon "C:\repos\Voice2Note\.venv\Scripts\v2n.exe" "rec-daemon --config C:\work\rec-daemon.yaml"
nssm set V2NRecDaemon AppDirectory "C:\work"
nssm set V2NRecDaemon AppStdout "C:\work\logs\rec-daemon.log"
nssm set V2NRecDaemon AppStderr "C:\work\logs\rec-daemon.log"
nssm set V2NRecDaemon AppRotateFiles 1
nssm set V2NRecDaemon AppExit Default Restart
nssm start V2NRecDaemon
```

### 手动清理说明

- **正常清理**：手机端删除录音记录时会调用 `POST /api/v1/sessions/cancel`，电脑端自动删除对应任务的本地工作目录（`<SESSION_WORK_PATH>/<session_id>/`）并保留最小取消占位记录，不删除云盘文件。
- **遗弃任务与手动回收**：若手机端重装应用、丢失本地数据或长时间未发送取消通知，电脑端不会自动过期删除任务目录（每小时录音约占 115 MB）。管理员可在确认无进行中任务后停止服务，手动清空 `<SESSION_WORK_PATH>` 下的旧任务子目录，以及云盘 `<CLOUD_ROOT_PREFIX>/_probe/` 下累积的探测小文件。
