# RecNote 侧需要同步修改的契约条目

这份文件是给 RecNote 仓库看的。Voice2Note 侧的实现方案已经定稿在 [add-rec-daemon](openspec/changes/add-rec-daemon/design.md)，协作备忘见 [V2N-RecDaemon.md](V2N-RecDaemon.md)。

定稿过程中有几处约定被改掉了，RecNote 现有文档里写的还是旧版本。**下列每一条都需要 RecNote 侧同步修改**，否则两端会在联调时对不上。每条都给出「现在写的」与「改成什么」，以及要改的文件。

## 一、必须修改的条目

### 1. 文件路径的基准改成「相对云盘根」

- **现在**：`design.md` 第 3 节写「每块 `path`，路径相对于任务根」，结果索引写「原稿路径相对于 `.transcripts/`」。同一套协议里有三个不同的路径基准。
- **改成**：所有路径一律相对**云盘根**。音频块是 `RecNoteRemote/<session_id>/audio/000001.wav`，原稿是 `RecNoteRemote/<session_id>/.transcripts/000001.md`。不得以 `/` 开头，不得含协议前缀、反斜杠或上级目录。
- **要改**：`openspec/changes/add-remote-compute-mode/design.md` 第 3 节、`specs/remote-audio-relay/spec.md`、`specs/remote-transcript-results/spec.md`、根目录 `Vec2Note_todo.md` 第 5 节。

### 2. 块字段删掉 `bytes`

- **现在**：块条目是 `sequence, path, start_sample, sample_count, bytes, sha256`。
- **改成**：`sequence, path, start_sample, sample_count, sha256`。完整性只看 `sha256`（覆盖整个 WAV 文件字节，含文件头）。体积上限改由电脑在流式读取时硬截断实现，不需要预先知道大小。
- **要改**：`design.md` 第 3 节、`specs/remote-audio-relay/spec.md`、`Vec2Note_todo.md` 第 5 节。
- **注意**：`sample_count` 保留且必须有，它是采样帧数，手机侧用它算段落时间范围，电脑侧用它做最终清单的连续性校验。

### 3. 状态名 `asr_completed` 改成 `completed`

- **现在**：`design.md` 第 5 节的状态流转写 `waiting_source → receiving → draining → asr_completed`。
- **改成**：`waiting_source → receiving → draining → completed`，另有 `failed` 与 `cancelled`。这六个值是接口返回 `state` 字段的取值。
- **要改**：`design.md` 第 5 节、`Vec2Note_todo.md` 第 5 节。若手机代码里已有状态字符串常量，一并改。

### 4. 探测接口的字段名 `cloud_path` 改成 `path`

- **现在**：`POST /api/v1/probe` 包体是 `token, cloud_path, sha256`。
- **改成**：`token, path, sha256`。`cloud_path` 在开始请求里指任务目录，在探测里指一个文件，同名不同义会造成歧义。探测路径固定形如 `RecNoteRemote/_probe/<uuid>.bin`，位于云盘根下恰好三段。
- **要改**：`design.md` 第 4 节的接口表、`specs/remote-compute-mode/spec.md` 的「远程连接配置与测试」、`Vec2Note_todo.md` 第 4 节。

### 5. 探测要验证两个方向，手机需要回读回执

- **现在**：探测只验证「手机写入的文件电脑能读到」。
- **改成**：电脑在读完探测文件后会写一份回执 `<探测路径>.receipt.json` 到同一目录，内容含协议版本、探测文件校验值、识别是否就绪与时间。**手机必须再读回这份回执并校验 `receipt.sha256`**，通过才算双端目录可达。
- **原因**：只验读方向时，云盘目录只读挂载也能探测全绿，用户录完一小时才会发现电脑写不回原稿。
- **要改**：`specs/remote-compute-mode/spec.md` 的「远程连接配置与测试」及其场景。

### 6. 探测响应体有了明确字段

- **现在**：探测响应未定义。
- **改成**：返回 `protocol_version, ok, asr_ready, model_loaded, read_ok, write_ok, receipt{path, sha256}, error`。HTTP 状态码只表达请求本身是否合法（鉴权、协议版本、路径格式、体积），就绪情况一律看响应体里的布尔值，不要因为 `ok: false` 就去做退避重试。
- `model_loaded` 为 `false` 表示电脑还没加载识别模型，首块识别会明显变慢，建议在界面上提示。
- **要改**：`specs/remote-compute-mode/spec.md`、`design.md` 第 4 节。

### 7. 取消的语义从「停止任务」变成「停止并清理电脑本地」

- **现在**：`design.md` 第 4 节写「停止电脑任务；可删除记录的本地清理可尽力通知，不要求删除云盘」。
- **改成**：取消会停止任务、**删除电脑本地工作目录**、保留云盘资料。对已完成的任务执行取消同样清理本地并转为 `cancelled`。所以手机删除记录时发出的取消现在有实际清理效果，值得保证送达。
- **要改**：`design.md` 第 4 节、`specs/remote-session-control/spec.md` 的「结束通知包体只有任务 token」相关条目。

### 8. 对已取消的任务再次开始，返回冲突

- **现在**：未定义。
- **改成**：返回 `409 session_terminal`。手机侧不要把这种冲突当成异常弹窗，它表示「这个任务已经取消了，要用新的 `session_id`」。
- **要改**：`specs/remote-session-control/spec.md` 的「开始通知立即建立轮询任务」。

### 9. 请求体里的未知字段会被静默忽略

- **现在**：未定义。
- **改成**：电脑侧忽略不认识的字段，只校验必填字段的存在与类型。手机侧将来加字段是安全的，但 v1 不要依赖任何未在本文档列出的字段。
- **要改**：`specs/remote-session-control/spec.md` 的「控制请求不泄漏身份」。

### 10. 原稿统一以单个换行结尾，空块不是 0 字节文件

- **现在**：写「静音块原稿正文可为空，但仍须生成可核验文件」。
- **改成**：所有原稿内容等于识别结果加一个换行；空块原稿是 1 字节的换行文件。手机侧去掉首尾空白后判空，逻辑不变。这样避免 0 字节对象在部分网盘后端被拒绝或读回 404。
- **要改**：`design.md` 第 3 节、`specs/remote-transcript-results/spec.md`、`Vec2Note_todo.md` 第 5 节。

### 11. 电脑的配置文件叫 `rec-daemon.yaml`，不是 `config.yaml`

- **现在**：`Vec2Note_todo.md` 第 3 节写「服务 `config.yaml`」。
- **改成**：服务配置文件名是 `rec-daemon.yaml`，与批处理用的 `config.yaml` 分开。内容仍然不含任何模型字段。
- **要改**：`Vec2Note_todo.md` 第 3 节、`design.md` 第 7 节。

### 12. 结束通知恒返回成功

- **现在**：未定义。
- **改成**：只要任务 token 合法，结束通知一律返回成功，重复提交也返回成功。手机侧不需要为结束通知处理 409 或其他冲突分支。
- **要改**：`specs/remote-session-control/spec.md` 的「结束通知包体只有任务 token」。

## 二、新增内容

### 13. 新增错误码 `audio_too_large`

音频块体积超过电脑配置的上限时返回该错误码，标记为不可重试。不要与「格式不符」混为一谈，两者的修复方式不同。

### 14. 复制一份协议样例文件

Voice2Note 侧会在 `tests/fixtures/protocol_v1/` 放四份 JSON 样例（滚动清单、最终清单、结果索引、完成标记）与一份字段说明。**请复制一份到 RecNote 的单元测试里做字段断言**——这是联调前唯一能发现字段拼写分歧的手段，语言无关，不需要跑 Python。

### 15. 完整的错误码清单

接口层：`invalid_request`、`invalid_cloud_path`、`invalid_path`、`unauthorized`、`forbidden`、`unknown_session`、`not_found`、`session_id_conflict`、`session_terminal`、`payload_too_large`、`unsupported_protocol_version`、`too_many_requests`、`internal_error`、`asr_not_ready`、`storage_not_ready`。

云盘侧：`audio_missing`、`audio_sha256_mismatch`、`audio_format_invalid`、`audio_too_large`、`audio_decode_failed`、`asr_failed`、`transcript_upload_failed`、`manifest_invalid`、`probe_missing`、`probe_sha256_mismatch`、`probe_too_large`。

重试策略按 `retryable` 字段走，不要按 HTTP 状态码自己猜。

## 三、不需要改的内容

以下约定 RecNote 侧现有写法与定稿一致，保持不动：

- 电脑只做识别，不保存模型配置、不提供整理接口、不生成笔记。
- 音频只走云盘，公开接口不传音频本体。
- 每份录音独立任务 token，结束包体只有 `token` 一个字段。
- 手机只提供约 30 秒大切片，按句切分在电脑内部完成，手机不接收逐句字幕。
- 滚动清单与最终清单的字段集合、`revision` 单调递增、`sample_rate=16000`／`channels=1`／`encoding=pcm_s16le`。
- 进入详情或手动刷新时才查询结果，后台不建立原稿长轮询。
- 原稿到齐后由用户手动点击整理，手机直连自己保存的供应商。

## 四、联调前的最小检查清单

1. 手机写一个探测文件，电脑能读到并校验通过。
2. 电脑写回回执，手机能读到并校验通过。
3. 手机上传一块 30 秒音频并更新滚动清单，电脑能下载并通过格式与校验值检查。
4. 电脑写出 `000001.md` 后再更新 `index.json`，手机按索引取回原稿，段落时间来自本地音频块样本范围。
5. 手机停止并发出结束通知，电脑等最终清单齐全后依次写出最终索引与 `complete.json`。
6. 手机校验 `complete.json` 与全部原稿后显示转写完成，此时电脑全程没有发出任何模型请求。
7. 手机删除记录并发出取消，电脑本地工作目录被清理，云盘副本仍在。
