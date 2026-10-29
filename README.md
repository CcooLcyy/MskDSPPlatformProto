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

## 远程设备与 SSH 隧道

契约位于 `platform/v1/remote.proto`，命名空间为 `mskdsp.platform.remote.v1`。复用平台现有 gRPC 监听地址；设备控制连接与每条数据隧道使用独立的双向流。

- `DeviceAccessService.ConnectDevice`：下位机使用独立设备 Bearer 令牌上线，首帧为 `hello`，接收平台打开隧道通知；每 30 秒发送完整服务列表心跳。
- `DeviceAccessService.AttachTunnel`：下位机先连接本机服务或固定纵密目标，再使用设备凭据和一次性票据附着数据流。票据绑定设备和当前控制连接，15 秒有效且仅消费一次。
- `RemoteAccessService.ListDevices`：上位机使用既有账号登录会话查询获授权设备，不包含设备凭据或现场地址。
- `RemoteAccessService.OpenTunnel`：上位机首帧指定设备及服务名称，平台授权并配对设备数据流；收到 `ready` 后转发 `data`，支持方向独立的 `half_close` 和完整 `close`。

服务名称为 `ModuleManager`、下位机允许且实际运行的模块名、`ssh` 或固定纵密服务 `vertical-security`；不能传任意 URL/IP/端口。每帧 1～65536 字节，每设备最多 16 条隧道，背压等待最多 30 秒。隧道断开不重放字节，设备重连不恢复旧隧道。协议逐项错误码、首帧及超时要求以 RPC 中文注释为准。

另有上位机内部纵密维护服务 `vertical-security`，仅由启用 SSH 维护能力的下位机登记，目标固定为 `11.22.33.44:8888`。平台将其与 `ssh` 一样按现有 `allow_ssh` 授权、过滤列表和动态撤权；仅 `allow_grpc` 不能使用该服务。该扩展沿用现有服务名称字段和字节流契约，不引入协议字段、任意目标参数或通用 IP 通道。纵密 TLS 和账号认证由上位机与装置处理，平台仅转发。

用户凭据与设备凭据不得混用。连接统一复用现有明文 gRPC，平台登录和设备令牌随明文连接传输，不提供 TLS 或证书配置。平台按设备及服务类别授权；通用字节隧道不解析模块 RPC，不能据此承诺读写分离。SSH 身份校验和系统登录由上位机 SSH 客户端与下位机 SSH 服务完成，平台不解密 SSH 会话、不保存 SSH 密码或私钥。

实现方案与验收见平台仓库 `docs/远程设备接入与SSH隧道.md`。各消费仓库可以固定提交引用子模块或保存规范文件镜像；镜像必须与本仓库字节一致。运行 `python -m unittest discover -s tests -v` 验证线格式兼容，运行 `python scripts/check_remote_consumers.py --workspace <三端仓库父目录>` 验证消费端协议一致性（需要 Python protobuf 包及 protoc）。

`scripts/remote_smoke.py` 使用独立 Python 客户端启动本机真实平台程序，在临时目录创建测试账号、设备和数据，验证 Web 授权、明文 gRPC 登录、设备上线、双向二进制数据、半关闭尾部、票据防重放和运行中撤权。依赖 `grpcio`、`protobuf` 和 `protoc`。脚本不接受部署地址，不操作远端：

```sh
python scripts/remote_smoke.py --platform-binary ../MskDSPPlatform/target/debug/mskdsp-platform.exe
```

这类本机跨语言验证不包含下位机 C++ 编译、ARM64 或现场 SSH 验收。Linux 下将可执行路径调整为无 `.exe` 后缀的本机程序。

## 公共安装包流式下载

`DownloadPackageRequest` 为 `package_id:string=1`、`offset:uint64=2`；响应 `DownloadPackageChunk` 为 `offset:uint64=1`、`data:bytes=2`、`total_size:uint64=3`、`sha256:string=4`、`eof:bool=5`。此新增 RPC 不改变既有消息字段。

不携带发布令牌即可读取已发布或曾发布的上下位机包；未发布和平台自身私有候选返回 `NOT_FOUND`。服务端每块最多 256 KiB，偏移连续，各块完整文件大小/摘要保持一致，只有末块 EOF；请求偏移等于大小时返回空 EOF，超过大小返回 `OUT_OF_RANGE`。损坏包返回 `DATA_LOSS`，读取或背压等待超时返回 `DEADLINE_EXCEEDED`；客户端取消即结束读取。

客户端按已落盘偏移续传，并核对块身份、偏移、EOF 和完整文件大小/SHA256。上位机安装继续验证 Tauri 签名，下位机继续验证安装包；不能把流关闭或目录元数据视为安装成功。
