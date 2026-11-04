#!/usr/bin/env python3
"""校验 rekit.py 与 dsh-plugin-rea 的版本号一致。

单仓多组件最容易出的问题就是版本漂移：插件发了 0.2.0，部署器还写着 0.1.0。
CI 每次跑这个脚本，本地也能手动跑。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    py_src = (ROOT / "rekit.py").read_text()
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', py_src, re.M)
    if not m:
        print("✗ rekit.py 里找不到 __version__", file=sys.stderr)
        return 1
    py_version = m.group(1)

    pkg = json.loads((ROOT / "dsh-plugin-rea/package.json").read_text())
    js_version = pkg.get("version")

    if js_version != py_version:
        print(
            f"✗ 版本不一致：rekit.py = {py_version}，dsh-plugin-rea = {js_version}",
            file=sys.stderr,
        )
        return 1

    print(f"✓ 版本一致：{py_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
