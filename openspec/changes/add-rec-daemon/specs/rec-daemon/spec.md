# Spec Delta

## Purpose

定义 `v2n rec-daemon` 这个常驻进程对外的控制面行为：进程入口与子命令、服务配置的来源与校验、两级 token 鉴权、七个 HTTP 接口的请求与响应、任务状态取值、错误响应格式，以及健康检查与云盘探测。这些行为是手机端 RecNote 依赖的稳定契约，识别算法与云盘数据格式分别属于其他 capability。

## ADDED Requirements

### Requirement: 常驻进程入口与辅助子命令

系统 SHALL 提供 `v2n rec-daemon` 作为常驻进程入口，并额外提供 `v2n rec-daemon init-config` 与 `v2n rec-daemon token` 两个辅助子命令。`v2n rec-daemon` MUST 在前台持续运行直到收到终止信号，MUST NOT 自行派生守护进程或写系统服务。现有 `v2n START-END [TITLE]` 与 `v2n config` 的行为 MUST 保持不变。

#### Scenario: 启动常驻进程
- **WHEN** 用户执行 `v2n rec-daemon --config C:\work\rec-daemon.yaml`
- **THEN** 进程加载该配置、恢复已有任务、在配置的地址与端口上提供接口，并持续运行

#### Scenario: 旧批处理用法不受影响
- **WHEN** 用户执行 `v2n 141-142 "示例课程"`
- **THEN** 行为与本次改动前完全一致，仍依次执行识别与笔记生成

#### Scenario: 生成配置与凭据
- **WHEN** 用户依次执行 `v2n rec-daemon init-config` 与 `v2n rec-daemon token`
- **THEN** 前者从模板创建配置文件并打印其绝对路径，后者只打印新连接 token 及其摘要和会话密钥，不修改任何文件

### Requirement: 服务配置与批处理配置分离

服务配置 SHALL 使用独立文件名 `rec-daemon.yaml` 与独立的加载校验路径，MUST NOT 读取或要求 `config.yaml` 中的任何字段。该配置 MUST NOT 包含任何 LLM 相关字段，进程 MUST NOT 导入 LLM 模块、不生成笔记、不写笔记输出目录。配置缺失必填字段时 MUST 一次性列出全部缺失字段名。

#### Scenario: 同一目录两个配置共存
- **WHEN** 同一目录下同时存在批处理用的 `config.yaml` 与服务用的 `rec-daemon.yaml`
- **THEN** `v2n START-END` 只读前者，`v2n rec-daemon` 只读后者，互不影响

#### Scenario: 缺少全部 LLM 字段仍可启动
- **WHEN** `rec-daemon.yaml` 只含识别、监听、鉴权、云盘与状态目录字段
- **THEN** 进程正常启动、接收任务、完成识别并回写原稿，全程不发出任何 LLM 请求

#### Scenario: 必填字段缺失
- **WHEN** 配置删除了多个必填字段
- **THEN** 启动失败并一次性列出全部缺失字段名，不启动接口

### Requirement: 配置错误消息不含字段值

配置加载失败时的错误消息 MUST 只报告字段名与失败原因，MUST NOT 输出字段值、凭据正文或配置文件全文。所有日志级别下 MUST NOT 出现连接 token、任务 token、云盘账号或云盘密码。

#### Scenario: 云盘密码非法
- **WHEN** `OPENLIST_PASSWORD` 取值非法导致校验失败
- **THEN** 错误消息指出该字段名与原因，不含密码内容

#### Scenario: 全级别日志脱敏
- **WHEN** 使用合成凭据把日志级别依次设为 DEBUG、INFO、WARNING、ERROR 并跑完一次任务
- **THEN** 任何级别的日志中都不出现 token 与云盘凭据

### Requirement: 两级 token 鉴权

连接 token SHALL 用于健康检查、云盘探测和建立任务，服务端只保存其不可逆摘要。任务 token SHALL 由服务端为每份录音独立派生，用于结束、查询、重试与取消。仅有连接 token MUST NOT 能结束任何任务；任务 token MUST NOT 能操作其他任务。两类 token MUST NOT 写入云盘文件或日志。

#### Scenario: 连接 token 无法结束任务
- **WHEN** 客户端用连接 token 调用结束接口
- **THEN** 请求被拒绝并返回鉴权错误，任务状态不变

#### Scenario: 任务 token 与任务不匹配
- **WHEN** 用任务甲的 token 查询或取消任务乙
- **THEN** 请求被拒绝，任务乙保持原状态

#### Scenario: 更换连接 token 不影响进行中任务
- **WHEN** 用户在录音进行中更换了服务端配置里的连接 token 摘要并重启进程
- **THEN** 已签发的任务 token 仍然有效，进行中的任务可以继续结束与查询

### Requirement: 健康检查与云盘探测接口

`GET /api/v1/health` SHALL 返回协议版本、进程版本、识别就绪、模型是否已加载、云盘就绪与能力列表，MUST NOT 返回配置正文或任何凭据，MUST NOT 因缺少 LLM 配置而失败。`POST /api/v1/probe` SHALL 完成「手机写入探测文件、电脑读取并校验、电脑写回回执、手机回读回执」的完整往返，MUST NOT 建立任务或发起识别。

#### Scenario: 健康检查
- **WHEN** 客户端带连接 token 请求健康检查
- **THEN** 得到协议版本、版本号、识别与云盘就绪状态，响应中不含任何凭据

#### Scenario: 探测通过
- **WHEN** 手机写入一个小型探测文件并提交其相对路径与校验值，电脑能读到且校验通过，电脑写回回执，手机能读回该回执
- **THEN** 响应报告读取与写入均正常，客户端据此判定双端目录可达

#### Scenario: 电脑无法写回
- **WHEN** 电脑能读到探测文件但写入回执失败
- **THEN** 响应明确报告写入不可用及可读原因，客户端不得显示连接正常

#### Scenario: 探测不建任务
- **WHEN** 客户端完成一次探测
- **THEN** 服务端任务数量不变，没有任何识别被启动

### Requirement: 任务控制接口的请求与响应

开始、结束、重试、取消四个接口 SHALL 分别接受协议版本、连接 token 或任务 token，并按固定字段返回结果。结束与取消 MUST 幂等且恒返回成功；重试对已结束或已取消的任务 MUST 返回冲突错误；开始对已取消的同一任务 MUST 返回冲突错误。查询接口 SHALL 返回任务状态、结束标志、总块数、已完成块数、失败块数与时间戳。

#### Scenario: 开始任务
- **WHEN** 客户端提交合法协议版本、连接 token、任务标识、云盘路径、标题与来源类型
- **THEN** 返回任务标识、任务 token 与初始状态

#### Scenario: 重复结束
- **WHEN** 客户端对同一任务重复提交结束通知
- **THEN** 每次均返回成功，任务进入收尾或保持已完成状态

#### Scenario: 对终态任务重试
- **WHEN** 客户端对已完成或已取消的任务提交重试
- **THEN** 返回冲突错误，任务状态不变

#### Scenario: 查询任务进度
- **WHEN** 客户端带任务 token 查询自己的任务
- **THEN** 得到状态、结束标志、块计数与最近更新时间，响应中不含原稿内容

### Requirement: 统一状态取值与错误响应格式

任务状态 SHALL 取自固定集合：等待来源、接收中、收尾中、已完成、失败、已取消。所有错误响应 SHALL 使用同一结构，包含错误码、可读原因与是否可重试。服务端 MUST NOT 返回重定向响应。请求体中的未知字段 MUST 被静默忽略，缺失必填字段或类型不符 MUST 返回参数错误。

#### Scenario: 鉴权失败不重试
- **WHEN** 客户端收到鉴权失败响应
- **THEN** 响应标记为不可重试，客户端停止自动提交

#### Scenario: 暂时性故障可退避
- **WHEN** 客户端收到限流或服务暂不可用响应
- **THEN** 响应标记为可重试，客户端按有限退避重试

#### Scenario: 未知字段被忽略
- **WHEN** 请求体包含服务端不认识的额外字段
- **THEN** 请求照常处理，不因该字段返回错误

#### Scenario: 接口不跳转
- **WHEN** 客户端请求任一接口
- **THEN** 服务端只返回成功或错误结果，不返回指向其他地址的重定向
