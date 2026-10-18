# MskDSPPlatformProto

MskDSP 平台的 Protocol Buffers 接口契约仓库，供平台服务、上位机和 PubMgr 按需引用并生成服务端或客户端代码。

## 职责边界

- 本仓库维护平台对外的接口定义及使用说明。
- 平台的软件发布管理接口与版本查询接口分别定义，公共消息结构按需复用。
- 上位机与下位机之间的设备接口继续由 `MskDSPProto` 维护。
- 本仓库不包含平台业务实现、发布客户端或生成的代码。

## 软件仓库接口

契约位于 `platform/v1/software.proto`，命名空间为 `mskdsp.platform.software.v1`。

- `SoftwarePublishService` 面向 PubMgr 和 GitLab CI：创建上传任务、按偏移分块上传、查询进度、校验入库；只有完整发布权限才能发布和撤回。
- `SoftwareCatalogService` 面向上位机：查询已发布或曾发布版本；安装包和兼容 `latest.json` 清单通过平台 HTTP 下载。

发布 RPC 必须携带 gRPC metadata `authorization: Bearer <发布令牌>`。令牌由平台管理员独立配置，不能写入协议仓库或日志；未配置时发布服务拒绝请求。
客户端每块最多发送 1 MiB，遇到断线先 `GetUpload` 查询落盘偏移再续传。流结束后必须显式 `CompleteUpload` 校验，再调用 `PublishRelease` 更新指定通道。重复创建、完成和发布的幂等语义及错误码见 RPC 中文注释。

平台自身 CI 包的产品为 `mskdsp-platform`、架构为 `linux-amd64`，新增兼容字段 `build_time=14`、`channel=15`，仅接受 `main` 和 `ci`。CI 专用令牌只允许创建、上传、查询和完成这些包，不能发布、撤回或操作上下位机包。
平台自身包在 `CompleteUpload` 后成为私有候选，GitLab 不调用 `PublishRelease`；平台管理员检查本地软件仓库后显式决定升级。公共目录与下载不公开这些候选包。

各项目应将仓库固定到明确提交，并用对应语言的 protobuf/gRPC 工具生成代码。服务端向后兼容增加字段时分配新字段号，禁止重用删除字段号或改变现有字段含义；破坏性协议使用新的版本命名空间。
