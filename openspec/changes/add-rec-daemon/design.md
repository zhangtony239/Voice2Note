# Design

## Context

动机见 [proposal.md](proposal.md)。当前仓库状态决定了这次改动的形状：

- [`asr.py`](../../../src/voice2note/asr.py) 的 `transcribe()` 只用到 `config.torch_backend`、`config.disable_mmap`、`config.asr_language` 三个字段，内部按句切分与模型懒加载都已具备，模型是进程级单例。这是可以直接复用的部分，不需要改算法。
- [`config.py`](../../../src/voice2note/config.py) 的必填字段里包含 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SYSTEM_PROMPT`，且只从当前工作目录读 `config.yaml`。这套加载路径无法满足「没有任何 LLM 字段也能启动」，必须另写一套。
- [`cli.py`](../../../src/voice2note/cli.py) 的 `main()` 用手工分支处理 `config` 子命令，避开必填的 `START-END` 位置参数。新增子命令可以照同样的办法接，不影响现有用法。
- [`pyproject.toml`](../../../pyproject.toml) 没有任何 Web 服务、HTTP 客户端或测试依赖。
- 手机侧契约来自 RecNote 仓库分支 `origin/codex/remote-compute-mode` 的 `f612b77` 与 `e00d3c4`（`openspec/changes/add-remote-compute-mode/` 与根目录 `Vec2Note_todo.md`）。该侧明确规定电脑只做到识别为止，不保存 LLM 配置、不提供整理接口、不生成笔记。
- 仓库里现有的 `V2N-RecDaemon.md` 是上述文档的早期快照，缺少「识别完成不调用大模型」「电脑不提供整理接口」等约定，本次一并改写。

## Goals / Non-Goals

**Goals:**

- 一个能在 Windows 上由 NSSM 拉起的常驻进程，崩溃后重启能接上原任务。
- 对外只暴露小型控制接口，音频与原稿全部走云盘。
- 云盘上的四份 JSON 字段与取值固定，双端可以各自实现、互相校验。
- 全部协议行为能在没有显卡、没有真实网盘的情况下被自动化测试覆盖。

**Non-Goals:**

- 不做笔记生成、不做大模型调用、不做照片处理。
- 不做自动清理：磁盘回收依赖手机发出的取消通知。
- 不做多进程、多实例、多卡并行识别。
- 不提供逐句时间戳或逐句字幕接口。

## Decisions

### 1. 入口叫 `v2n rec-daemon`，配置叫 `rec-daemon.yaml`

```text
v2n rec-daemon [--config PATH] [--host HOST] [--port PORT] [--log-level LEVEL]
v2n rec-daemon init-config [--config PATH]
v2n rec-daemon token
```

配置默认文件名用 `rec-daemon.yaml` 而不是 `config.yaml`，因为批处理用的 `config.yaml` 必须包含 LLM 字段，两者语义互斥；同名文件放在同一目录必然互相污染。`--config` 接受任意绝对路径，`init-config` 从模板原样复制以保留注释，`token` 只打印不写文件。

[`main()`](../../../src/voice2note/cli.py:114) 增加一条 `if args_list[0] == "rec-daemon":` 分支。`rec-daemon` 不匹配 [`parse_range`](../../../src/voice2note/config.py:184) 的 `\d+-\d+` 模式，所以 `v2n START-END [TITLE]` 的行为一行不改。

配置里的 `SERVER_HOST` / `SERVER_PORT` / `SERVER_AUTH_TOKEN_HASH` 保留 `SERVER_` 前缀，因为它们描述的是进程内部的 HTTP 监听，与会话状态用的 `SESSION_` 前缀分工清楚。

### 2. 进程形态：FastAPI + uvicorn + httpx，单 worker

选 FastAPI 的理由是它把这份契约里最容易写错的三件事变成框架默认行为：字段类型校验、统一的错误响应外壳、请求体上限中间件。用标准库自己写七个接口的校验与错误映射，正是最容易和契约悄悄走偏的地方。

三条实现要求必须写进代码，否则契约会静默失效：

- **覆盖 FastAPI 默认的校验失败响应**。默认返回 `{"detail": [...]}`，与约定的 `{code, message, retryable}` 不符，必须注册全局异常处理器改掉。
- **`workers=1`**。多进程会各自加载一份模型、各自持有一份状态、各自轮询同一批任务，直接互相踩踏。
- **`--proxy-headers --forwarded-allow-ips=127.0.0.1`**。进程在 cloudflared 后面，探测接口的限流必须按真实来源地址计，否则所有请求看起来都来自隧道进程。

同时关闭 uvicorn 自带的访问日志，改用本设计第 12 节的自有日志。

WebDAV 客户端用 `httpx`，`follow_redirects=False`，超时取配置值。所有请求由任务线程发起，`httpx.Client` 实例共享（线程安全）。

### 3. 两级 token：连接 token 存摘要，任务 token 用密钥派生

连接 token 由 `v2n rec-daemon token` 生成，配置里只存 `sha256:<64 位小写十六进制>`，比较用常数时间比较。用纯 SHA-256 而不是慢哈希，因为连接 token 是 256 位随机值，不存在字典攻击面，慢哈希只会让每个请求都白烧 CPU。

任务 token 不落盘，而是按需重算：

```text
session_token = base64url( HMAC-SHA256(SESSION_TOKEN_SECRET, session_id) )   # 43 字符
```

这样重启后重算结果必然一致，幂等开始天然成立；任务 token 与连接 token 完全解耦，用户更换连接 token 不会毁掉正在录制的任务；也不需要加密存储或密钥文件。

「结束、重试、取消只带任务 token，服务端如何定位任务」——进程启动扫描状态目录时，对每个任务标识重算一遍 token 建内存映射。这一步本来就要做（恢复任务必须扫目录），额外成本为零。

`SESSION_TOKEN_SECRET` 长度不足 32 字符时拒绝启动，否则用户填一个短串就等于任务 token 可猜。

### 4. 任务状态与错误响应

状态取值固定六个：`waiting_source`、`receiving`、`draining`、`completed`、`failed`、`cancelled`。

| 状态 | 进入条件 | 终态 |
| --- | --- | --- |
| `waiting_source` | 任务已建立，尚未处理任何块 | 否 |
| `receiving` | 至少处理过一块，结束通知未到 | 否 |
| `draining` | 已收到结束通知，最终清单全集尚未全部完成 | 否 |
| `completed` | 最终清单全部块完成，结果索引与完成标记已发布 | 是 |
| `failed` | 会话级不可继续（最终清单内容自相矛盾等） | 否，可由重试恢复 |
| `cancelled` | 收到取消 | 是 |

块级失败不进 `failed`：某块坏了，任务仍是 `receiving` 或 `draining`，只是失败块计数大于零，原因写在结果索引的块条目里。

所有错误使用同一结构，HTTP 响应外层包 `error` 键，云盘文件里直接内联该对象：

```json
{ "code": "audio_sha256_mismatch", "message": "chunk 3 sha256 mismatch", "retryable": true }
```

接口层错误码：

| HTTP | `code` | `retryable` | 触发条件 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | false | JSON 畸形、必填字段缺失、类型不符 |
| 400 | `invalid_cloud_path` | false | 云盘路径不满足根相对规则，或与任务标识不符 |
| 400 | `invalid_path` | false | 清单或索引中的文件路径不满足规则 |
| 401 | `unauthorized` | false | 缺少 token 或 token 不匹配 |
| 403 | `forbidden` | false | token 有效但与地址中的任务不匹配 |
| 404 | `unknown_session` | false | token 对应的任务不存在 |
| 404 | `not_found` | false | 未知路由 |
| 409 | `session_id_conflict` | false | 同一任务标识配不同云盘路径 |
| 409 | `session_terminal` | false | 对已完成或已取消的任务执行重试或开始 |
| 413 | `payload_too_large` | false | 控制请求体超过 64 KiB |
| 422 | `unsupported_protocol_version` | false | 协议版本不是 1 |
| 429 | `too_many_requests` | true | 探测限流或活动任务已达上限 |
| 500 | `internal_error` | true | 未预期异常 |
| 503 | `asr_not_ready` | true | 识别不可用 |
| 503 | `storage_not_ready` | true | 云盘不可达 |

云盘侧错误码（块级或会话级）：

| `code` | `retryable` | 含义 |
| --- | --- | --- |
| `audio_missing` | true | 清单声明了但读不到，可能尚未可见 |
| `audio_sha256_mismatch` | true | 读完了但校验不符，可能是最终一致性 |
| `audio_format_invalid` | false | 采样率、声道、位深或样本数不符 |
| `audio_too_large` | false | 超过单块体积上限 |
| `audio_decode_failed` | false | 解码失败 |
| `asr_failed` | true | 识别抛异常 |
| `transcript_upload_failed` | true | 原稿写回云盘失败 |
| `manifest_invalid` | true | 最终清单内容自相矛盾 |
| `probe_missing` | true | 探测文件读不到 |
| `probe_sha256_mismatch` | true | 探测文件校验不符 |
| `probe_too_large` | false | 探测文件超过上限 |

约束：错误消息不得包含 token、云盘账号密码或配置文件正文，可以包含任务标识、块编号与相对路径。服务端永不返回重定向响应，所有响应为 `application/json; charset=utf-8`，不设 Cookie。请求体中的未知字段静默忽略，让手机侧将来加字段安全。

### 5. 七个接口

| 接口 | 鉴权载体 | 说明 |
| --- | --- | --- |
| `GET /api/v1/health` | `Authorization: Bearer` 连接 token | 版本、就绪、模型是否已加载、能力列表 |
| `POST /api/v1/probe` | 包体 `token` | 云盘往返探测，不建任务 |
| `POST /api/v1/sessions/start` | 包体 `token` | 建立任务，返回任务 token |
| `POST /api/v1/sessions/end` | 包体仅 `token`（任务 token） | 进入收尾，恒返回成功 |
| `POST /api/v1/sessions/retry` | 包体仅 `token` | 只补足未完成块 |
| `POST /api/v1/sessions/cancel` | 包体仅 `token` | 停止并清理本地，恒返回成功 |
| `GET /api/v1/sessions/<session_id>` | `Authorization: Bearer` 任务 token | 状态与块计数 |

POST 接口不接受 `Authorization` 头，GET 接口不接受查询串里的 token。只保留一种方式，少一个测试面与泄漏面。

健康检查响应：

```json
{
  "protocol_version": 1,
  "service": "voice2note",
  "version": "0.1.0",
  "asr_ready": true,
  "model_loaded": false,
  "storage_ready": true,
  "capabilities": ["probe", "session-control", "asr"]
}
```

`asr_ready` 只校验配置合法、计算设备可用、状态目录可写，不触发权重加载；`model_loaded` 单独暴露模型是否已加载，让手机能提示「首块识别会较慢」而不是让用户以为卡死。

开始请求与响应：

```json
{
  "protocol_version": 1,
  "token": "<connection_token>",
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "cloud_path": "RecNoteRemote/3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "title": "示例课程",
  "source_type": "microphone"
}
```

```json
{
  "protocol_version": 1,
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "session_token": "<43 字符>",
  "state": "waiting_source"
}
```

`source_type` 严格取值 `microphone` 或 `import`。`title` 上限 200 字符，只记录用于诊断，不参与识别。

查询响应：

```json
{
  "protocol_version": 1,
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "cloud_path": "RecNoteRemote/3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "state": "draining",
  "end_received": true,
  "chunks_total": 12,
  "chunks_completed": 5,
  "chunks_failed": 1,
  "created_at": "2026-10-13T09:12:03Z",
  "updated_at": "2026-10-13T09:20:41Z",
  "error": null
}
```

不返回任何原稿路径或原稿内容，原稿由手机从云盘读取。

### 6. 云盘探测的完整往返

原契约只验「手机写、电脑读」，留下一个真实漏洞：云盘目录只读挂载时探测全绿，用户录完一小时才发现电脑写不回原稿。因此探测做成四步：

1. 读探测文件，校验体积与内容校验值；
2. 创建探测目录（已存在视为成功）；
3. 写回回执，内容含协议版本、探测文件校验值、识别是否就绪与时间；
4. 回读回执并自校验，通过才认为写入正常。

手机在收到成功后还必须读回该回执并校验，通过才算双端目录可达。缺这一环，写入结论只是电脑的一面之词——两端挂载名不同是常态。

请求体：

```json
{
  "protocol_version": 1,
  "token": "<connection_token>",
  "path": "RecNoteRemote/_probe/6f1d0c9a-8b2e-4d3f-9a71-2c5b8e4d1f60.bin",
  "sha256": "<手机写入文件的整文件校验值>"
}
```

字段名用 `path` 而不是 `cloud_path`，因为 `cloud_path` 在开始请求里指任务目录，同名不同义正是要消灭的歧义。探测路径必须位于 `<CLOUD_ROOT_PREFIX>/_probe/` 下、恰好三段、无上级目录。

响应：

```json
{
  "protocol_version": 1,
  "ok": true,
  "asr_ready": true,
  "model_loaded": false,
  "read_ok": true,
  "write_ok": true,
  "receipt": {
    "path": "RecNoteRemote/_probe/6f1d0c9a-....bin.receipt.json",
    "sha256": "<回读校验通过的那个校验值>"
  },
  "error": null
}
```

HTTP 状态码只表达请求本身是否合法，就绪情况一律走响应体。否则手机会把「目录不可达」误判成「请求失败」去做无意义的退避。同一来源每分钟最多 10 次探测，超出返回 429。

### 7. 云盘目录结构与四份 JSON

```text
<cloud_path>/
  audio/000001.wav
  manifest.json                  # 手机：滚动的已上传音频清单
  final.json                     # 手机：停止后不可变的全部音频清单
  .transcripts/
    000001.md                    # 电脑：一个大切片的完整原稿
    index.json                   # 电脑：块结果与递增版本
    complete.json                # 电脑：整次识别完成
```

所有文件路径一律相对于云盘根，不使用相对于任务目录或相对于 `.transcripts/` 的第二种基准。原草案同时存在三个基准，是纯粹的隐患。校验只需四条规则：以 `cloud_path + "/"` 开头；不以 `/` 开头、不含 scheme、不含反斜杠、不含上级目录或空段；文件名按块编号固定生成；只用「自己的云盘根 + 该相对路径」拼接，永不接受 URL。

音频清单（`manifest.json`，`final.json` 为同结构加 `total_samples` 与 `chunk_count`）：

```json
{
  "protocol_version": 1,
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "revision": 3,
  "sample_rate": 16000,
  "channels": 1,
  "encoding": "pcm_s16le",
  "chunks": [
    {
      "sequence": 1,
      "path": "RecNoteRemote/3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13/audio/000001.wav",
      "start_sample": 0,
      "sample_count": 480000,
      "sha256": "9f2c...e1"
    }
  ]
}
```

块字段只有 `sequence`、`path`、`start_sample`、`sample_count`、`sha256` 五项。原先还有一个 `bytes` 字段，它与校验值在完整性上完全重复，唯一剩余价值是下载前预检，而这个价值可以用流式读取时硬截断实现，因此删除。`sample_count` 是采样帧数，不可省略：它是段落时间线的来源，也是最终清单连续性校验的链条，还是区分短块与坏文件的唯一依据。

结果索引（`index.json`）：

```json
{
  "protocol_version": 1,
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "revision": 7,
  "chunks": [
    {
      "sequence": 1,
      "audio_sha256": "9f2c...e1",
      "status": "completed",
      "path": "RecNoteRemote/3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13/.transcripts/000001.md",
      "sha256": "ab12...7d",
      "error": null
    }
  ]
}
```

`status` 只有 `completed` 与 `failed` 两个值，索引只收录已经尝试完毕的块；尚未处理的块不出现，手机用最终清单的块集合减去索引中的块集合得到「等待中」。这样「等待」与「失败」永远不会混。

完成标记（`complete.json`）：

```json
{
  "protocol_version": 1,
  "session_id": "3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13",
  "final_manifest_sha256": "5c7a...90",
  "chunks": [
    {
      "sequence": 1,
      "audio_sha256": "9f2c...e1",
      "path": "RecNoteRemote/3f2b8c1e-9a4d-4f7b-8e21-5c6d7a9b0e13/.transcripts/000001.md",
      "sha256": "ab12...7d"
    }
  ]
}
```

`final_manifest_sha256` 是对电脑读到的最终清单原始字节做校验，不是重新序列化后再算。所有 `sha256` 为小写十六进制 64 字符，所有计数与版本为整数，文件为 UTF-8 无 BOM。

`cloud_path` 必须等于 `RecNoteRemote/<session_id>`，任务标识为带连字符的 UUID（大小写不敏感，服务端归一化为小写）。这条同时解决了路径穿越与「索引指向别的任务」两类问题。

### 8. 音频块的六步校验

```text
读音频块（流式，边写临时文件边算校验值）
  → 超过单块体积上限        → audio_too_large（不可重试），中止并删除临时文件
  → 读不到                  → audio_missing（可重试），删除临时文件
  → 校验值不符              → audio_sha256_mismatch（可重试），删除临时文件
  → 原子替换为正式文件
  → 解析音频头：16 kHz、单声道、16 位、帧数与 sample_count 一致、数据长度一致
  → 任一不符                → audio_format_invalid（不可重试），删除文件
  → sample_count 为 0       → audio_format_invalid（最终清单不得含空块）
```

校验失败但文件可读的块保留音频文件，供重试时免去重新下载；只有「下载后校验不符」的块在重试时删除重下。识别解码失败另记 `audio_decode_failed`。

不使用「文件大小 = 44 + 样本数 × 2」这类等式做判据：如果音频头带额外数据块，等式会成立不了，把好文件判成坏文件。一律以解析头部为准。

### 9. 原稿内容与发布顺序

原稿内容等于 `transcribe()` 的返回值，不加标题、不加前置元数据、不加时间戳，统一以单个换行结尾（空块原稿是 1 字节的换行文件）。

选择带换行而不是 0 字节文件，是因为 0 字节对象在部分 WebDAV 后端会被拒绝或读回 404，各类网盘驱动对空对象处理很不一致。校验值覆盖文件实际字节，两端都只需原样读、原样校验，不需要理解这个约定。手机侧去掉首尾空白后判空，逻辑不变。

发布顺序固定：

```text
块识别完成
  → 原子写本地原稿
  → 写回云盘原稿
  → 更新结果索引（版本号 +1）
全部块完成
  → 逐份回读原稿并校验
  → 写回最终结果索引
  → 写回完成标记
  → 状态转为 completed
```

单块写回后不回读，但发布完成标记前必须逐份回读。写回接口返回成功不代表存储最终落盘，这是「没有完成标记之前手机不能误判」的唯一可靠实现方式。

同一块被重复处理且原稿字节完全相同时，不写云盘、不递增索引版本，避免重试把版本号空转推高。

### 10. 幂等、终态与重试取消

开始请求以任务标识为幂等键，不掺标题与来源类型：

| 已有记录 | 返回 |
| --- | --- |
| 无 | 建任务、签发任务 token、`waiting_source` |
| 有，云盘路径相同 | 原任务 token 与当前状态 |
| 有，云盘路径不同 | `409 session_id_conflict` |
| 有，状态为 `cancelled` | `409 session_terminal`，不复活任务 |

结束与取消恒返回成功（只要 token 合法）：重复结束返回当前状态，对已完成或已取消的任务也返回成功。取消对已完成的任务同样清理本地工作目录并转为已取消。两者语义分歧是有意的：取消是「别再做这个了」，对终态无害；重试是「再多做一些」，对终态无意义，因此返回 409。

状态机不设时间上限，不设收尾超时。录四小时的会议是合法场景，而「手机崩溃导致任务永远停在收尾」的代价只是一个退避到 30 秒的轮询线程，比误杀真实任务小得多。配置里的空闲告警只写日志，不改状态。

清单版本处置：版本增大则采用；版本相同且内容相同则无操作；版本相同但内容不同或版本更小则忽略并记警告，一律不失败。手机侧一个版本号计算错误不该把电脑任务锁死。

最终清单要区分两种情况：读不到、解析失败、字段缺失都算暂时不可用，继续等待；解析成功但内容自相矛盾（块范围有空洞、重叠、总数与块数不符、文件名不符）才算失败，并标记为可重试，因为仍可能是存储侧短暂返回了拼接版本。已采用过一份最终清单后读到不同内容则忽略后来的。

重试只做六件事：重读清单；把失败块与校验未通过的块重置为待处理；保留已完成块、已上传原稿与索引版本；会话级失败的任务重新校验最终清单，通过则退回收尾或接收状态；返回本次重置的块编号列表。索引版本永不回退。

### 11. 状态保存、调度与识别串行

```text
<SESSION_STATE_PATH>/<session_id>.json      # 任务状态
<SESSION_WORK_PATH>/<session_id>/audio/     # 已下载音频
<SESSION_WORK_PATH>/<session_id>/transcripts/  # 本地原稿
```

状态文件记录任务身份、状态、结束标志、清单版本与校验值、索引版本、每块进度与重试次数。写入采用「同目录临时文件 → 落盘 → 原子替换」。`updated_at` 只在状态实质变化时更新，轮询本身不写盘。

每个任务一个轮询线程，间隔从 `POLL_INTERVAL_SECONDS` 起，无变化时指数退避到 `POLL_MAX_INTERVAL_SECONDS`，有变化立即复位。用每任务一线程而不是单调度循环，是因为各任务节奏天然不同（一个在收尾等尾块、一个在正常接收），单循环要为每个任务维护下次执行时间，反而更复杂。活动任务数上限由 `MAX_ACTIVE_SESSIONS` 控制，超限拒绝开始请求。

识别必须串行：模型是进程级单例，多线程同时进入推理既不是安全用法，在单卡上也只会互相争抢显存。用一把全局锁包住整个识别调用，`ASR_MAX_CONCURRENCY` 保留但启动时校验必须为 1。

复用识别函数的方式只是放宽类型标注：在 [`asr.py`](../../../src/voice2note/asr.py) 里定义只含 `torch_backend`、`disable_mmap`、`asr_language` 三个字段的设置类型，把 `transcribe()` 及内部加载、单段转写的标注从 `Config` 换成它。运行期零影响，算法零改动，`Config` 因字段名相同天然兼容。原稿写入不复用 `save_transcript()`（它是裸写，没有原子性与落盘保证），服务侧自己写原子版本。

取消时的清理顺序是**先写占位记录，再删工作目录**。反过来做，进程在中间被杀就会留下「状态还在、音频已删」的不一致，重启后会尝试重新识别一个已被取消的任务。占位记录保留任务标识，所以重复取消仍能通过鉴权并返回成功，不会把「清理成功」误报成「鉴权失败」。

### 12. 配置字段与契约常量

`rec-daemon.yaml` 共 22 个字段，全部列入必填清单（`DISABLE_MMAP` 除外，按计算后端推导），默认值只存在于 `assets/rec-daemon-template.yaml`：

| 分组 | 字段 |
| --- | --- |
| 识别 | `TORCH_BACKEND`、`ASR_LANGUAGE`、`DISABLE_MMAP` |
| 监听与鉴权 | `SERVER_HOST`、`SERVER_PORT`、`SERVER_AUTH_TOKEN_HASH`、`SESSION_TOKEN_SECRET` |
| 云盘 | `OPENLIST_WEBDAV_URL`、`OPENLIST_USERNAME`、`OPENLIST_PASSWORD`、`OPENLIST_TIMEOUT_SECONDS`、`OPENLIST_VERIFY_TLS`、`CLOUD_ROOT_PREFIX` |
| 存储 | `SESSION_STATE_PATH`、`SESSION_WORK_PATH` |
| 调度 | `POLL_INTERVAL_SECONDS`、`POLL_MAX_INTERVAL_SECONDS`、`ASR_MAX_CONCURRENCY`、`MAX_ACTIVE_SESSIONS` |
| 保护与诊断 | `MAX_AUDIO_CHUNK_BYTES`、`SESSION_IDLE_WARN_HOURS`、`LOG_LEVEL` |

存储路径字段的相对路径按**配置文件所在目录**解析，不按当前工作目录——NSSM 拉起服务时工作目录是系统目录，按工作目录解析必然读错。

契约常量**不做成配置项**，硬编码在代码里：清单与索引 2 MiB、单份原稿 128 KiB、控制请求 64 KiB、探测文件 64 KiB、采样率 16000、单声道、`pcm_s16le`、块约 30 秒。可配置就等于两端可能不一致，而契约不一致是这类项目最贵的缺陷。只有实现侧的保护值（单块体积上限、活动任务上限）才可配置。

配置校验失败时只报字段名与原因，不报字段值。现有的 `TORCH_BACKEND` 错误会打印取值，新加载器不能照抄这个模式，否则云盘密码会进日志。

日志统一输出到标准错误由 NSSM 捕获，格式为「时间 级别 [任务标识前 8 位] 消息」，记录任务建立与结束、清单版本变化、块下载与识别结果及耗时、原稿写回、索引与完成标记发布、状态转移；不记录 token、云盘凭据、原稿正文与音频内容。

### 13. 测试策略

四层，边界清楚：

| 层 | 内容 | 需要显卡 |
| --- | --- | --- |
| 单元测试 | 路径校验、音频头校验、最终清单语义校验、状态转移、token 派生与摘要、配置校验与脱敏 | 否 |
| 协议测试 | 本地模拟网盘服务 + 用固定文本替换识别结果，跑完整任务生命周期 | 否 |
| 接口测试 | 直接对应用对象发请求，不启真实端口，覆盖全部错误码 | 否 |
| 真模型冒烟 | 真模型跑一个三秒合成音频，默认跳过 | 是 |

协议测试的识别结果用固定文本替换，因为协议正确性与模型质量无关，真模型会让每个用例慢几十秒并依赖显存。

本地模拟网盘服务实现最小读写子集，故障注入是它的核心价值，必须覆盖：文件尚未可见、半写 JSON、延迟可见、内容被篡改、拒绝 0 字节写入、慢响应、重定向、写入返回成功但不落盘。

合成音频用标准库生成，可控样本数（标准块、短块、纯静音块、样本数为零的非法块）与头部字段。

`tests/fixtures/protocol_v1/` 放四份 JSON 样例与一份字段说明，作为两端共同的字段基准；RecNote 侧复制一份做同样的字段断言。这是联调前唯一能发现字段拼写分歧的手段。

完成标准：每个错误码至少一个用例触发它；幂等分支与终态分支各有用例；六步校验每一步有一个「构造该失败」的用例；用合成凭据跑全部日志级别并断言不含凭据；重启后任务 token 重算一致、索引版本不回退。

不做的：不连真实网盘、不测隧道链路、不在持续集成里跑真模型、不做压力测试。

## Risks / Trade-offs

- [识别串行，两台手机同时长录音时第二台原稿明显延后] → 明确写进文档与已知限制，不假装并行。
- [本地工作目录不自动清理，约 115 MB/小时] → 清理依赖手机删除记录时发出的取消通知；README 说明可整体清空工作目录；被遗弃的任务（手机重装、记录永不删除）会永久残留。
- [探测目录的小文件会累积] → 已知限制，手动清理。
- [首次真实录音要等模型加载] → 健康检查与探测暴露模型是否已加载，手机据此提示；进程不预热模型，避免启动即占显存。
- [最终清单内容永久矛盾时任务无法自愈] → 任务标记失败并保留已下载音频与原稿，用户需用新任务标识重录。
- [取消后反悔必须换任务标识] → 取消必须真的取消；这是有意的取舍。
- [手机侧数据丢失会导致电脑侧工作目录残留] → 唯一无法自动兜底的场景，写进 README 的手动清理说明。
- [契约改动需要双仓库同步] → 由 `RecNote-Sync.md` 逐条列出需要对方修改的内容，避免只改一端。

## Migration Plan

1. 先落地进程骨架与配置加载：`v2n rec-daemon` 能启动、健康检查与探测可用、`v2n START-END` 行为经回归确认不变。
2. 再落地任务生命周期：开始／结束／查询／重试／取消、token 派生、状态保存与重启恢复。
3. 再落地云盘数据面：清单读取、块下载与校验、原稿写回、结果索引与完成标记。
4. 最后补测试基础设施与协议样例，并把 `V2N-RecDaemon.md`、`RecNote-Sync.md`、`README.md` 写完整。
5. 回退方式：停止并禁用 NSSM 服务即可，现有批处理命令不受影响；已产生的任务目录可手动删除。
6. 上线前用本地模拟网盘服务跑通全流程，再在真实 OpenList 上做一次小体积往返验证；真实链路验收需人工执行，自动化测试不能替代。
