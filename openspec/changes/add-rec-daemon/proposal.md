# Proposal

## Why

RecNote 在手机上长时间录音时持续运行识别模型，发热明显。现在要把识别搬到电脑上：手机只采集并保存约 30 秒的音频块，经云盘中转，由 Voice2Note 识别并把原稿写回云盘，手机取回原稿后仍由手机自己的大模型配置生成笔记。

Voice2Note 目前只有一个批处理命令 `v2n START-END [TITLE]`，跑完 ASR 接着跑 LLM，没有常驻进程，也没有对外接口。要承接手机的任务，需要一个能长期运行、能按手机通知建立任务、能从云盘取音频并回写原稿的进程。这个进程就是 `v2n rec-daemon`。

## What Changes

- 新增 `v2n rec-daemon` 常驻进程入口，以及 `v2n rec-daemon init-config`、`v2n rec-daemon token` 两个辅助子命令。现有 `v2n START-END [TITLE]` 与 `v2n config` 的行为一行不改。
- 新增服务专用配置 `rec-daemon.yaml` 与独立的加载／校验路径。该配置**不含任何 LLM 字段**，进程不导入 `voice2note.llm`，不生成笔记，不写 `NOTE_PATH`。
- 新增 HTTP 控制接口（协议 v1，`/api/v1` 下七个接口）：健康检查、云盘连通探测、任务开始、任务结束、任务状态查询、任务重试、任务取消。鉴权分两级：连接 token 用于探测与建任务，任务 token 用于结束、查询、重试、取消。
- 新增云盘数据面：通过电脑本机 OpenList 轮询手机的滚动音频清单，下载并校验音频块，把每个音频块的完整原稿写回任务目录下的 `.transcripts/`，最后发布结果索引与整任务完成标记。
- 新增任务生命周期管理：每份录音一个独立任务、独立任务 token、独立本地工作目录；结束通知先到、尾块迟到、电脑重启都能继续；取消时清理电脑本地工作目录但保留云盘资料。
- 复用现有 [`asr.py`](../../../src/voice2note/asr.py) 的 `transcribe()`（含内部按句切分），只放宽其类型标注，识别算法不改。模型在进程内单例，识别调用串行执行。
- 新增依赖 `fastapi`、`uvicorn`、`httpx`，以及开发依赖 `pytest`。
- 新增测试基础设施：本地模拟网盘服务（能按需制造 404、半写、延迟可见）、合成音频生成、协议样例文件。

## Capabilities

### New Capabilities

- `rec-daemon`: `v2n rec-daemon` 进程的控制面行为——入口与子命令、`rec-daemon.yaml` 的字段与校验、两级 token 鉴权、七个 HTTP 接口的请求与响应、状态取值、错误码与错误响应格式、健康检查与云盘探测。
- `remote-session`: 一份录音任务的生命周期——会话状态机、幂等开始、结束／重试／取消的语义、任务 token 的派生与还原、状态落盘与重启恢复、已取消任务的占位记录、并发上限与识别串行。
- `cloud-relay`: 云盘数据面——任务目录结构、滚动清单与最终清单的读取规则、音频块的下载与六步校验、原稿回写、结果索引与整任务完成标记的发布顺序、路径与体积边界。

### Modified Capabilities

无。现有 `asr`、`cli`、`config`、`llm` 四个 capability 的主 spec 尚未归档同步（`openspec/specs/` 为空），本次全部以新增 capability 描述新行为，不对未同步的主 spec 写修改。

## Impact

- 代码：新增 `src/voice2note/rec_daemon/`（进程入口、配置、鉴权、HTTP 路由、任务调度、云盘客户端、协议校验）；`src/voice2note/cli.py` 增加一个子命令分发分支；`src/voice2note/asr.py` 只改类型标注；新增 `src/voice2note/assets/rec-daemon-template.yaml`；`src/voice2note/__init__.py` 补 `__version__`。
- 依赖：`pyproject.toml` 新增 `fastapi`、`uvicorn`、`httpx`，开发依赖新增 `pytest`。
- 配置：新增 `rec-daemon.yaml` 与其模板。现有 `config.yaml` 与 `config-template.yaml` 完全不动。
- 接口：对外新增协议 v1 的七个 HTTP 接口。音频与原稿都走云盘，公开接口不传文件。
- 文档：改写 `V2N-RecDaemon.md` 使其与 RecNote 侧最新提案一致；新增 `RecNote-Sync.md` 说明 RecNote 侧需要同步修改的契约条目；`README.md` 增加 daemon 用法、NSSM 部署与手动清理说明。
- 不在本仓库实现：LLM 整理、笔记生成、照片处理、手机端任何代码。这些都在 RecNote 侧，且 RecNote 侧不需要向本进程发送整理请求。
