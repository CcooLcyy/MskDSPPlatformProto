"""固定远程客户端的线格式兼容契约，不代替三端业务及设备联调。"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mskdsp.platform.remote.v1"


class RemoteWireCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("protoc")
        if not compiler:
            raise RuntimeError("未找到 protoc，不能把未执行协议检查视为测试通过")
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = Path(temporary) / "remote.pb"
            subprocess.run(
                [compiler, "--proto_path=.", f"--descriptor_set_out={descriptor}",
                 "platform/v1/remote.proto"],
                cwd=ROOT, check=True, capture_output=True,
            )
            descriptors = descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes())
        cls.pool = descriptor_pool.DescriptorPool()
        for file in descriptors.file:
            cls.pool.Add(file)

    def message(self, name):
        return message_factory.GetMessageClass(
            self.pool.FindMessageTypeByName(f"{PACKAGE}.{name}")
        )

    # 固定客户端已知的二进制载荷标记，保证零字节/非UTF8数据不被文本转换。
    def test_binary_data_golden_wire(self):
        frame_type = self.message("TunnelFrame")
        golden = bytes.fromhex("220400ff0d0a")
        frame = frame_type.FromString(golden)
        self.assertEqual(frame.WhichOneof("payload"), "data")
        self.assertEqual(frame.data, b"\x00\xff\r\n")
        self.assertEqual(frame_type(data=b"\x00\xff\r\n").SerializeToString(), golden)

    # 半关闭与完整关闭必须能由旧客户端区分，不能复用字段号。
    def test_half_close_and_close_are_distinct(self):
        frame_type = self.message("TunnelFrame")
        half = frame_type.FromString(bytes.fromhex("2a00"))
        closed = frame_type.FromString(bytes.fromhex("3200"))
        self.assertEqual(half.WhichOneof("payload"), "half_close")
        self.assertEqual(closed.WhichOneof("payload"), "close")
        self.assertNotEqual(half.SerializeToString(), closed.SerializeToString())

    # 首帧服务选择固定为命名目标，不能退化为客户端指定现场网络地址。
    def test_open_target_golden_wire(self):
        frame_type = self.message("TunnelFrame")
        frame = frame_type.FromString(bytes.fromhex("0a080a01641203737368"))
        self.assertEqual(frame.open.device_id, "d")
        self.assertEqual(frame.open.service, "ssh")
        self.assertEqual([f.name for f in frame.open.DESCRIPTOR.fields], ["device_id", "service"])

    # 固定四个外部RPC路径及双向流语义，防止单边生成可编译但不可互通的接口。
    def test_public_rpc_paths_and_streaming(self):
        expected = {
            "DeviceAccessService": {"ConnectDevice": (True, True), "AttachTunnel": (True, True)},
            "RemoteAccessService": {"ListDevices": (False, False), "OpenTunnel": (True, True)},
        }
        for name, methods in expected.items():
            service = self.pool.FindServiceByName(f"{PACKAGE}.{name}")
            actual = {method.name: (method.client_streaming, method.server_streaming)
                      for method in service.methods}
            self.assertEqual(actual, methods)

    # 未知未来字段在旧客户端往返时保留，保证只增字段的兼容扩展。
    def test_unknown_field_survives_old_client_roundtrip(self):
        wire = bytes.fromhex("220100a00607")
        self.assertEqual(self.message("TunnelFrame").FromString(wire).SerializeToString(), wire)


if __name__ == "__main__":
    unittest.main()
