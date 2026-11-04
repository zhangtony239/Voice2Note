# V2N-RecDaemon：Voice2Note 与 RecNote 远程识别协作契约

本文档记录 `v2n rec-daemon` 常驻进程与手机端 RecNote 的协作契约（协议版本 `protocol_version: 1`），与 [`openspec/changes/add-rec-daemon/design.md`](openspec/changes/add-rec-daemon/design.md) 完全对齐。需要 RecNote 仓库同步调整的条目见 [`RecNote-Sync.md`](RecNote-Sync.md)。

## 1. 职责边界

- **手机端（RecNote）**：采集录音并按约 30 秒切分为标准 PCM WAV 音频块，上传到双方共用的云盘任务目录并维护滚动清单 `manifest.json` 与停止后的最终清单 `final.json`；通过 HTTP 控制接口向电脑发起连通探测、建立任务、通知结束、查询进度、触发重试或取消；从云盘取回原稿后，在手机本地使用手机自身配置的大模型供应商整理笔记。
- **电脑端（`v2n rec-daemon`）**：只负责语音识别（ASR）并将每个音频块的完整原稿写回云盘任务目录的 `.transcripts/` 下，维护结果索引 `index.json` 与完成标记 `complete.json`。
- **明确不做**：电脑端不包含任何 LLM 配置字段、不导入大模型模块、不调用任何大模型接口、不提供笔记整理接口、不生成笔记文件、不写 `NOTE_PATH`、不处理照片，也不向手机返回单句粒度的时间戳或流式片段。

## 2. 命令行入口与服务配置

```text
v2n rec-daemon [--config PATH] [--host HOST] [--port PORT] [--log-level LEVEL]
v2n rec-daemon init-config [--config PATH]
v2n rec-daemon token
```

- 服务配置文件固定命名为 `rec-daemon.yaml`（与批处理命令 `v2n START-END` 使用的 `config.yaml` 完全独立，互不读取）。
- `v2n rec-daemon init-config` 从包内模板 `assets/rec-daemon-template.yaml` 原样复制生成配置文件并打印绝对路径。
- `v2n rec-daemon token` 生成新的连接 token、对应的 `sha256:<64位小写十六进制>` 摘要与 `SESSION_TOKEN_SECRET`，只打印到标准输出，不修改任何文件。
- `rec-daemon.yaml` 共 22 个字段（不含任何 LLM 字段），存储目录路径（`SESSION_STATE_PATH`、`SESSION_WORK_PATH`）为相对路径时按**配置文件所在目录**解析。

## 3. 两级 Token 鉴权

1. **连接 token（`connection_token`）**
   - 用于 `GET /api/v1/health`、`POST /api/v1/probe`、`POST /api/v1/sessions/start`。
   - 服务端配置仅保存 `SERVER_AUTH_TOKEN_HASH`（`sha256:<hex>`），使用常数时间比较校验。
2. **任务 token（`session_token`）**
   - 用于 `POST /api/v1/sessions/end`、`POST /api/v1/sessions/retry`、`POST /api/v1/sessions/cancel`、`GET /api/v1/sessions/<session_id>`。
   - 服务端按 `base64url(HMAC-SHA256(SESSION_TOKEN_SECRET, session_id))` 派生（无填充 43 字符），进程重启后重算结果一致，更换连接 token 不影响进行中的任务。
   - 连接 token 与任务 token 严格隔离、互不通用；任何 token 与云盘凭据均不写入云盘文件或日志。

## 4. 任务状态与统一错误格式

### 4.1 六个固定状态

| 状态值 | 含义 | 是否终态 |
| --- | --- | --- |
| `waiting_source` | 任务已建立，尚未处理完任何音频块 | 否 |
| `receiving` | 至少已处理过一块，尚未收到结束通知 | 否 |
| `draining` | 已收到结束通知，等待最终清单及全集音频块处理完毕 | 否 |
| `completed` | 最终清单全部音频块处理完成，原稿回读校验通过并已发布结果索引与完成标记 | 是 |
| `failed` | 会话级不可继续（如 `final.json` 内容自相矛盾），可通过 `retry` 恢复 | 否 |
| `cancelled` | 已取消并清理电脑本地工作目录 | 是 |

单块下载或识别失败不会将任务状态置为 `failed`，任务保持 `receiving` 或 `draining`，仅递增 `chunks_failed` 并在 `index.json` 的对应块记录错误原因。任务不设收尾超时，不因等待时间过长而自动失败。

### 4.2 错误结构与错误码

HTTP 错误响应统一为 `{"error": {"code": "...", "message": "...", "retryable": true|false}}`，云盘 JSON 内联同一 `{code, message, retryable}` 结构。

- **接口层错误码**：`invalid_request` (400)、`invalid_cloud_path` (400)、`invalid_path` (400)、`unauthorized` (401)、`forbidden` (403)、`unknown_session` (404)、`not_found` (404)、`session_id_conflict` (409)、`session_terminal` (409)、`payload_too_large` (413)、`unsupported_protocol_version` (422)、`too_many_requests` (429)、`internal_error` (500)、`asr_not_ready` (503)、`storage_not_ready` (503)。
- **云盘侧错误码**：`audio_missing`、`audio_sha256_mismatch`、`audio_format_invalid`、`audio_too_large`、`audio_decode_failed`、`asr_failed`、`transcript_upload_failed`、`manifest_invalid`、`probe_missing`、`probe_sha256_mismatch`、`probe_too_large`。

所有接口响应均为 `application/json; charset=utf-8`，永不返回 3xx 重定向；请求体中的未知字段静默忽略；控制请求体上限 64 KiB。

## 5. 七个 HTTP 控制接口

- `GET /api/v1/health`（`Authorization: Bearer <connection_token>`）
  返回 `{protocol_version, service, version, asr_ready, model_loaded, storage_ready, capabilities}`。
- `POST /api/v1/probe`（包体 `token` 为连接 token）
  请求 `{protocol_version: 1, token, path, sha256}`，其中 `path` 固定形如 `<CLOUD_ROOT_PREFIX>/_probe/<uuid>.bin`（相对云盘根恰好三段）。
  电脑执行四步闭环：① 读取探测文件并校验体积（≤ 64 KiB）与 `sha256`；② 确保 `_probe` 目录存在；③ 写入回执 `<path>.receipt.json`；④ 回读回执并校验 `sha256`。
  返回 `{protocol_version, ok, asr_ready, model_loaded, read_ok, write_ok, receipt: {path, sha256}, error}`。手机收到响应后还须从云盘读回 `receipt.path` 并核对 `receipt.sha256`。同一来源 IP 每分钟限流 10 次。
- `POST /api/v1/sessions/start`（包体 `token` 为连接 token）
  请求 `{protocol_version: 1, token, session_id, cloud_path, title, source_type}`。`session_id` 为带连字符的 UUID（归一化为小写），`cloud_path` 必须等于 `<CLOUD_ROOT_PREFIX>/<session_id>`，`source_type` 仅允许 `microphone` 或 `import`。
  按 `session_id` 幂等：同路径重复请求返回原 `session_token` 与当前 `state`；同 `session_id` 不同 `cloud_path` 返回 `409 session_id_conflict`；已 `cancelled` 的任务返回 `409 session_terminal`。
- `POST /api/v1/sessions/end`（包体仅 `{protocol_version: 1, token: <session_token>}`）
  幂等进入 `draining`（若已完成或已取消则保持原终态），恒返回 200。
- `POST /api/v1/sessions/retry`（包体仅 `{protocol_version: 1, token: <session_token>}`）
  保留已完成块、已上传原稿与 `index.json` 版本号，仅将失败块重置为待处理并重读清单；对 `completed` 或 `cancelled` 任务返回 `409 session_terminal`。
- `POST /api/v1/sessions/cancel`（包体仅 `{protocol_version: 1, token: <session_token>}`）
  幂等停止任务：先写入 `cancelled` 占位状态记录，再删除电脑本地工作目录，**保留云盘上的全部文件**；对已完成任务同样清理本地工作目录并转为 `cancelled`，重复取消恒返回 200。
- `GET /api/v1/sessions/<session_id>`（`Authorization: Bearer <session_token>`）
  返回 `{protocol_version, session_id, cloud_path, state, end_received, chunks_total, chunks_completed, chunks_failed, created_at, updated_at, error}`。若任务 token 与 URL 中的 `session_id` 不匹配，返回 `403 forbidden`。

## 6. 云盘目录结构与数据面规则

```text
<CLOUD_ROOT_PREFIX>/<session_id>/
  audio/000001.wav
  manifest.json
  final.json
  .transcripts/
    000001.md
    index.json
    complete.json
```

1. **统一路径基准**：所有 JSON 中出现的 `path` 字段一律为**相对于云盘根**的路径（如 `RecNoteRemote/<session_id>/audio/000001.wav`、`RecNoteRemote/<session_id>/.transcripts/000001.md`），不得以 `/` 开头，不得含协议前缀、反斜杠、空段或 `..`。音频块文件名固定为 `audio/{sequence:06d}.wav`，原稿文件名固定为 `.transcripts/{sequence:06d}.md`。
2. **清单格式与版本规则**：
   - `manifest.json` 含 `protocol_version, session_id, revision, sample_rate(16000), channels(1), encoding("pcm_s16le"), chunks`。
   - 每个音频块条目仅含五个字段：`sequence, path, start_sample, sample_count, sha256`。
   - `final.json` 在同结构基础上增加 `total_samples` 与 `chunk_count`。
   - 滚动清单只在 `revision` 增大时采用；版本相同但内容不同或版本更小时忽略并记录警告，不将任务置为失败。
   - 最终清单读取失败、JSON 截断或字段缺失视为暂时不可用继续等待；解析成功但块序号不连续、样本范围重叠/空洞、`total_samples` 或 `chunk_count` 不一致时置为 `failed`（`manifest_invalid`）。
3. **音频块六步校验**：
   流式下载并硬截断检查 `MAX_AUDIO_CHUNK_BYTES`（超限记 `audio_too_large`）→ 404/不可读记 `audio_missing` → 整文件 `sha256` 校验（不符记 `audio_sha256_mismatch`）→ 原子落盘 → 解析 WAV 头核对 `16000 Hz / 单声道 / 16-bit PCM / 帧数 == sample_count / sample_count > 0`（不符记 `audio_format_invalid`）→ 调用 `transcribe()` 识别（解码失败记 `audio_decode_failed`，推理异常记 `asr_failed`）。
4. **原稿格式与发布顺序**：
   - 原稿内容等于 `transcribe()` 返回文本加上单个换行符 `\n`（静音空块为 1 字节 `"\n"`，避免 0 字节文件在部分 WebDAV 后端异常）。
   - 单块完成后：先原子写本地原稿 → 上传云盘 `.transcripts/{sequence:06d}.md` → 更新并上传 `.transcripts/index.json`（`revision` 单调递增，只收录 `completed` 与 `failed` 的块）。
   - 最终清单全部块完成后：逐份从云盘回读 `.transcripts/{sequence:06d}.md` 并校验 `sha256` → 写入最终 `index.json` → 写入 `.transcripts/complete.json`（含 `final_manifest_sha256` 与每块的 `sequence, audio_sha256, path, sha256`）→ 状态转为 `completed`。
5. **体积边界**：`manifest.json` 与 `index.json` 上限 2 MiB，单份原稿上限 128 KiB，控制请求与探测文件上限 64 KiB。
