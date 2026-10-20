"""验证各语言构建前的规范镜像，发现跨仓库协议漂移即失败。"""

import argparse
import hashlib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    canonical = Path(__file__).resolve().parents[1] / "platform/v1/remote.proto"
    expected = canonical.read_bytes()
    mirrors = [
        args.workspace / "MskDSPPlatform/platform-proto/platform/v1/remote.proto",
        args.workspace / "MskDSP/platform-proto/platform/v1/remote.proto",
        args.workspace / "MskDSPUpper/src-tauri/proto-platform-agent/platform/v1/remote.proto",
    ]
    failed = False
    for mirror in mirrors:
        if not mirror.is_file():
            print(f"失败：消费端协议不存在：{mirror}")
            failed = True
        elif mirror.read_bytes() != expected:
            print(f"失败：消费端协议与规范不一致：{mirror}")
            failed = True
        else:
            print(f"一致：{mirror}")
    print(f"规范 SHA256：{hashlib.sha256(expected).hexdigest()}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
