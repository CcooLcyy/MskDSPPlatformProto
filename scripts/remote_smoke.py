"""使用独立 Python gRPC 客户端验证本机真实平台进程，禁止连接部署环境。

依赖 grpcio、protobuf、protoc；运行时只向临时目录写入数据和测试凭据。
示例：python scripts/remote_smoke.py --platform-binary ../MskDSPPlatform/target/debug/mskdsp-platform.exe
"""

import argparse
import concurrent.futures
import datetime
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "mskdsp.platform.remote.v1"
AGENT = "mskdsp.platform.v1"


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Outbound:
    def __init__(self):
        self.queue = queue.Queue(maxsize=8)
        self.stopped = threading.Event()

    def send(self, value):
        self.queue.put(value, timeout=5)

    def close(self):
        self.send(None)

    def cancel(self):
        self.stopped.set()

    def __iter__(self):
        return self

    def __next__(self):
        while not self.stopped.is_set():
            try:
                value = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if value is None:
                raise StopIteration
            return value
        raise StopIteration


class Smoke:
    def __init__(self, binary, temporary, tls=False):
        self.binary = binary
        self.temporary = Path(temporary)
        self.pool = descriptor_pool.DescriptorPool()
        self.cookiejar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookiejar))
        self.http_port = unused_port()
        self.grpc_port = unused_port()
        while self.grpc_port == self.http_port:
            self.grpc_port = unused_port()
        self.base = f"http://127.0.0.1:{self.http_port}"
        self.process = None
        self.tls = tls
        self.control = None
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.stop = threading.Event()
        self.observed = queue.Queue(maxsize=32)
        self.commands = []
        self.outbound = []

    def sender(self):
        outgoing = Outbound()
        self.outbound.append(outgoing)
        return outgoing

    def msg(self, name, **fields):
        return message_factory.GetMessageClass(self.pool.FindMessageTypeByName(name))(**fields)

    def frame(self, **fields):
        return self.msg(f"{REMOTE}.TunnelFrame", **fields)

    def rpc(self, service, method, response, streaming=False):
        factory = self.channel.stream_stream if streaming else self.channel.unary_unary
        cls = message_factory.GetMessageClass(self.pool.FindMessageTypeByName(response))
        return factory(
            f"/{service}/{method}",
            request_serializer=lambda value: value.SerializeToString(),
            response_deserializer=cls.FromString,
        )

    def api(self, path, body=None, method=None, action="device"):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json", "X-MskDSP-Action": action,
                     "Origin": self.base},
        )
        with self.http.open(request, timeout=10) as response:
            data = response.read()
            return json.loads(data) if data else None

    def start(self):
        descriptor = self.temporary / "contract.pb"
        subprocess.run(
            [shutil.which("protoc") or "protoc", "--proto_path=.",
             "--proto_path=../MskDSPPlatform/proto",
             f"--descriptor_set_out={descriptor}",
             "platform/v1/remote.proto", "platform/v1/agent.proto"],
            cwd=ROOT, check=True, capture_output=True,
        )
        for file in descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes()).file:
            self.pool.Add(file)
        password = secrets.token_urlsafe(24)
        admin_hash = subprocess.run(
            [str(self.binary), "--hash-admin-password"], input=password.encode(),
            capture_output=True, check=True,
        ).stdout.decode().strip()
        config = self.temporary / "platform.toml"
        config_text = (
            f'[server]\nhttp_addr="127.0.0.1:{self.http_port}"\n'
            f'grpc_addr="127.0.0.1:{self.grpc_port}"\n'
            f'[auth]\npassword_hash="{admin_hash}"\n'
        )
        roots = None
        if self.tls:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (
                x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]), critical=False).sign(key, hashes.SHA256())
            )
            roots = certificate.public_bytes(serialization.Encoding.PEM)
            cert_file, key_file = self.temporary / "server.pem", self.temporary / "server-key.pem"
            cert_file.write_bytes(roots)
            key_file.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            key_file.chmod(0o600)
            config_text += (
                '[tls]\nenabled=true\n'
                f'server_certificate={json.dumps(cert_file.as_posix())}\n'
                f'server_private_key={json.dumps(key_file.as_posix())}\n'
            )
        config.write_text(config_text, encoding="utf-8")
        environment = os.environ.copy()
        environment["MSKDSP_PLATFORM_CONFIG"] = str(config)
        environment["MSKDSP_PLATFORM_DATA_DIR"] = str(self.temporary / "data")
        self.log = (self.temporary / "platform.log").open("wb")
        self.process = subprocess.Popen(
            [str(self.binary)], env=environment, cwd=self.temporary,
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                self.api("/healthz")
                break
            except Exception:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("本地平台进程未就绪") from None
                self.stop.wait(0.1)
        self.api("/api/v1/auth/login", {"username": "root", "password": password}, action="auth")
        user_password = secrets.token_urlsafe(24)
        account = self.api("/api/v1/accounts", {
            "username": "remote-smoke", "display_name": "本地跨语言测试账号",
            "password": user_password,
        }, action="account")
        device = self.api("/api/v1/devices", {"device_id": "smoke-device", "display_name": "本地模拟设备"})
        self.device_id = device["device_id"]
        self.device_auth = (("authorization", f"Bearer {device['token']}"),)
        address = f"127.0.0.1:{self.grpc_port}"
        self.channel = (grpc.secure_channel(address, grpc.ssl_channel_credentials(root_certificates=roots))
                        if self.tls else grpc.insecure_channel(address))
        grpc.channel_ready_future(self.channel).result(timeout=10)
        login = self.rpc(f"{AGENT}.AgentService", "Login", f"{AGENT}.LoginResponse")(
            self.msg(f"{AGENT}.LoginRequest", username="remote-smoke", password=user_password), timeout=10,
        )
        self.user_auth = (("authorization", f"Bearer {login.access_token}"),)
        self.list_devices = self.rpc(f"{REMOTE}.RemoteAccessService", "ListDevices", f"{REMOTE}.ListDevicesResponse")
        assert not self.list_devices(self.msg(f"{REMOTE}.ListDevicesRequest"), metadata=self.user_auth, timeout=5).devices
        self.account_id = account["id"]
        self.grants = [{"account_id": self.account_id, "allow_grpc": True, "allow_ssh": True}]
        self.api(f"/api/v1/devices/{self.device_id}/grants", self.grants, method="PUT")
        visible = self.list_devices(self.msg(f"{REMOTE}.ListDevicesRequest"), metadata=self.user_auth, timeout=5).devices
        assert len(visible) == 1 and not visible[0].online
        self.open_rpc = self.rpc(f"{REMOTE}.RemoteAccessService", "OpenTunnel", f"{REMOTE}.TunnelFrame", True)
        self.attach_rpc = self.rpc(f"{REMOTE}.DeviceAccessService", "AttachTunnel", f"{REMOTE}.TunnelFrame", True)
        self.control_out = self.sender()
        self.control_out.send(self.msg(f"{REMOTE}.DeviceMessage", hello={
            "device_id": self.device_id, "agent_version": "python-smoke", "services": ["ModuleManager", "ssh"],
        }))
        self.control = self.rpc(f"{REMOTE}.DeviceAccessService", "ConnectDevice", f"{REMOTE}.PlatformMessage", True)(
            self.control_out, metadata=self.device_auth, timeout=90,
        )
        assert next(self.control).WhichOneof("payload") == "welcome"
        self.control_worker = self.workers.submit(self.control_loop)
        print(f"通过：真实平台进程、Web设备授权、Python用户登录及设备上线（TLS={self.tls}）")

    def control_loop(self):
        try:
            for message in self.control:
                if message.WhichOneof("payload") == "open_tunnel":
                    self.commands.append(message.open_tunnel)
                    self.observed.put(message.open_tunnel, timeout=5)
        except grpc.RpcError:
            if not self.stop.is_set():
                raise

    def open(self, service="ssh"):
        outgoing = self.sender()
        outgoing.send(self.frame(open={"device_id": self.device_id, "service": service}))
        return outgoing, self.open_rpc(outgoing, metadata=self.user_auth, timeout=30)

    def echo(self, command):
        outgoing = self.sender()
        outgoing.send(self.frame(attach={"tunnel_id": command.tunnel_id, "attach_token": command.attach_token}))
        call = self.attach_rpc(outgoing, metadata=self.device_auth, timeout=30)
        assert next(call).WhichOneof("payload") == "ready"
        for frame in call:
            kind = frame.WhichOneof("payload")
            if kind == "data":
                outgoing.send(self.frame(data=frame.data))
            elif kind == "half_close":
                # 明确验证客户端写EOF后，反方向最后一段数据不能丢失。
                outgoing.send(self.frame(data=b"tail-after-half-close"))
                outgoing.send(self.frame(half_close={}))
                outgoing.close()
            elif kind == "close":
                return

    def check_echo(self):
        outgoing, call = self.open()
        command = self.observed.get(timeout=5)
        worker = self.workers.submit(self.echo, command)
        assert next(call).WhichOneof("payload") == "ready"
        payload = bytes(range(256)) * 512
        for offset in range(0, len(payload), 65536):
            outgoing.send(self.frame(data=payload[offset:offset + 65536]))
        outgoing.send(self.frame(half_close={}))
        received = bytearray()
        saw_half = False
        for frame in call:
            kind = frame.WhichOneof("payload")
            if kind == "data":
                received.extend(frame.data)
            elif kind == "half_close":
                saw_half = True
                break
            elif kind == "close":
                break
        assert received == payload + b"tail-after-half-close", "双向数据或半关闭尾部损坏"
        assert saw_half, "反向半关闭未传递"
        worker.result(timeout=5)
        call.cancel()
        replay = self.sender()
        replay.send(self.frame(attach={"tunnel_id": command.tunnel_id, "attach_token": command.attach_token}))
        replay_call = self.attach_rpc(replay, metadata=self.device_auth, timeout=5)
        try:
            next(replay_call)
            raise AssertionError("一次性票据被重复使用")
        except grpc.RpcError as error:
            assert error.code() in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.NOT_FOUND)
        finally:
            replay_call.cancel()
        print("通过：128 KiB非文本双向数据、半关闭尾部、一次性票据不可重放")

    def check_rejection_and_revoke(self):
        outgoing, call = self.open("127.0.0.1:22")
        try:
            next(call)
            raise AssertionError("平台允许客户端指定任意网络地址")
        except grpc.RpcError as error:
            assert error.code() in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND,
                                    grpc.StatusCode.PERMISSION_DENIED)
        finally:
            call.cancel()
        outgoing, call = self.open()
        command = self.observed.get(timeout=5)
        worker = self.workers.submit(self.echo, command)
        assert next(call).WhichOneof("payload") == "ready"
        self.api(f"/api/v1/devices/{self.device_id}/grants", [], method="PUT")
        started = time.monotonic()
        try:
            while True:
                frame = next(call)
                if frame.WhichOneof("payload") == "close":
                    break
        except (grpc.RpcError, StopIteration):
            pass
        finally:
            call.cancel()
        assert time.monotonic() - started < 5, "权限撤销没有及时关闭已有隧道"
        try:
            worker.result(timeout=5)
        except grpc.RpcError:
            pass
        assert not self.list_devices(self.msg(f"{REMOTE}.ListDevicesRequest"), metadata=self.user_auth, timeout=5).devices
        print("通过：任意目标拒绝、运行中权限撤销及设备列表隔离")

    def finish(self):
        self.stop.set()
        for outgoing in self.outbound:
            outgoing.cancel()
        if self.control:
            self.control.cancel()
        if hasattr(self, "channel"):
            self.channel.close()
        self.workers.shutdown(wait=True, cancel_futures=True)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if hasattr(self, "log"):
            self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-binary", type=Path, required=True)
    parser.add_argument("--tls", action="store_true", help="生成临时本机证书并验证真实 TLS（需要 cryptography）")
    args = parser.parse_args()
    binary = args.platform_binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="mskdsp-local-smoke-") as temporary:
        smoke = Smoke(binary, temporary, tls=args.tls)
        try:
            smoke.start()
            smoke.check_echo()
            smoke.check_rejection_and_revoke()
        finally:
            smoke.finish()
    print("本地跨语言平台隧道验证通过；此结果不包含下位机C++/ARM64或现场SSH验证")


if __name__ == "__main__":
    main()
