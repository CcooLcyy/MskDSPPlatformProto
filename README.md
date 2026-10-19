# MskDSPPlatformProto

MskDSP 平台的 Protocol Buffers 接口契约仓库，供平台服务、上位机、CI 和发布客户端按需引用并生成服务端或客户端代码。

## 职责边界

- 本仓库维护平台对外的接口定义及使用说明。
- 平台的软件发布管理接口与版本查询接口分别定义，公共消息结构按需复用。
- 上位机与下位机之间的设备接口继续由 `MskDSPProto` 维护。
- 本仓库不包含平台业务实现、发布客户端或生成的代码。

## 软件仓库接口

契约位于 `platform/v1/software.proto`，命名空间为 `mskdsp.platform.software.v1`。

- `SoftwarePublishService` 面向 CI 和发布客户端：创建上传任务、按偏移分块上传、查询进度、校验入库；只有完整发布权限才能发布和撤回。
- `SoftwareCatalogService` 面向上位机：查询已发布或曾发布版本，并通过 `DownloadPackage` 服务端流下载安装包；既有 HTTP 安装包和 `latest.json` 清单保持兼容。

发布 RPC 必须携带 gRPC metadata `authorization: Bearer <发布令牌>`。令牌由平台管理员独立配置，不能写入协议仓库或日志；未配置时发布服务拒绝请求。
客户端每块最多发送 1 MiB，遇到断线先 `GetUpload` 查询落盘偏移再续传。流结束后必须显式 `CompleteUpload` 校验，再调用 `PublishRelease` 更新指定通道。重复创建、完成和发布的幂等语义及错误码见 RPC 中文注释。

平台自身 CI 包的产品为 `mskdsp-platform`、架构为 `linux-amd64`，新增兼容字段 `build_time=14`、`channel=15`，仅接受 `main` 和 `ci`。CI 专用令牌只允许创建、上传、查询和完成这些包，不能发布、撤回或操作上下位机包。
平台自身包在 `CompleteUpload` 后成为私有候选，GitLab 不调用 `PublishRelease`；平台管理员检查本地软件仓库后显式决定升级。公共目录与下载不公开这些候选包。

各项目应将仓库固定到明确提交，并用对应语言的 protobuf/gRPC 工具生成代码。服务端向后兼容增加字段时分配新字段号，禁止重用删除字段号或改变现有字段含义；破坏性协议使用新的版本命名空间。

## 公共安装包流式下载

`DownloadPackageRequest` 为 `package_id:string=1`、`offset:uint64=2`；响应 `DownloadPackageChunk` 为 `offset:uint64=1`、`data:bytes=2`、`total_size:uint64=3`、`sha256:string=4`、`eof:bool=5`。此新增 RPC 不改变既有消息字段。

不携带发布令牌即可读取已发布或曾发布的上下位机包；未发布和平台自身私有候选返回 `NOT_FOUND`。服务端每块最多 256 KiB，偏移连续，各块完整文件大小/摘要保持一致，只有末块 EOF；请求偏移等于大小时返回空 EOF，超过大小返回 `OUT_OF_RANGE`。损坏包返回 `DATA_LOSS`，读取或背压等待超时返回 `DEADLINE_EXCEEDED`；客户端取消即结束读取。

客户端按已落盘偏移续传，并核对块身份、偏移、EOF 和完整文件大小/SHA256。上位机安装继续验证 Tauri 签名，下位机继续验证安装包；不能把流关闭或目录元数据视为安装成功。
