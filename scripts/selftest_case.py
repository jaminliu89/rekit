#!/usr/bin/env python3
"""案件工作台的状态机自测。

只依赖标准库，不联网、不调用 rea：把 evidence_id 直接写进 case.json 模拟取证，
然后验证判定在五种状态下都给出正确结论。CI 每次跑，防止 fail-closed 规则被改坏。

    python3 scripts/selftest_case.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REKIT = ROOT / "rekit.py"
PY = sys.executable

failures: list[str] = []


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(REKIT), *args], capture_output=True, text=True)


def expect(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: 期望 {want!r}，实际 {got!r}")
        print(f"  ✗ {label}  期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ✓ {label}")


def verdict_of(case: Path) -> dict:
    return json.loads((case / "verdict.json").read_text())


def claim_status(case: Path, cid: str) -> str:
    for row in verdict_of(case)["claims"]:
        if row["id"] == cid:
            return row["verdict"]
    return "missing"


def patch_case(case: Path, fn) -> None:
    p = case / "case.json"
    data = json.loads(p.read_text())
    fn(data)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rekit-selftest-"))
    target = Path("/bin/echo")
    if not target.exists():
        print("跳过：/bin/echo 不存在")
        return 0

    try:
        print("1. 新建案件")
        r = run("case", "new", str(target), "--dir", str(tmp), "--no-thin", "--force")
        expect("new 退出码", r.returncode, 0)
        case = tmp / "echo"
        for name in ("case.json", "SCOPE.md", "SPEC.md"):
            expect(f"生成 {name}", (case / name).exists(), True)

        data = json.loads((case / "case.json").read_text())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        expect("sha256 锁定", data["target"]["sha256"], digest)
        expect("初始无断言", data["claims"], [])

        print("2. 未登记的证据要被拒绝")
        r = run("case", "claim", "--case", str(case), "--statement", "x", "--evidence", "ev_nope")
        expect("拒绝伪造 id 的退出码", r.returncode, 2)

        print("3. 注入证据后加三条断言")
        patch_case(case, lambda d: d["evidence"].__setitem__(
            "ev_test_a", {"command": "rea inspect", "at": "t", "file": "evidence/01.json", "label": "inspect"}))
        run("case", "claim", "--case", str(case), "--id", "C1",
            "--statement", "无证据但校验通过", "--check", "true")
        run("case", "claim", "--case", str(case), "--id", "C2",
            "--statement", "有证据没校验", "--evidence", "ev_test_a")
        run("case", "claim", "--case", str(case), "--id", "C3",
            "--statement", "有证据且校验通过", "--evidence", "ev_test_a", "--check", "true")

        r = run("case", "check", "--case", str(case))
        expect("open 的退出码", r.returncode, 1)
        expect("C1 无证据只能到 characterized", claim_status(case, "C1"), "characterized")
        expect("C2 有证据未校验", claim_status(case, "C2"), "characterized")
        expect("C3 证据+校验", claim_status(case, "C3"), "verified")
        expect("汇总状态", verdict_of(case)["status"], "open")

        print("4. 全部补齐后应为 ready")
        def fill(d: dict) -> None:
            for c in d["claims"]:
                if not c["evidence"]:
                    c["evidence"] = ["ev_test_a"]
                if c["id"] == "C2":  # 原本人工判定，补一条校验
                    c["check"] = {"kind": "shell", "run": "true", "expect_exit": 0}

        patch_case(case, fill)
        r = run("case", "check", "--case", str(case))
        expect("ready 的退出码", r.returncode, 0)
        expect("汇总状态", verdict_of(case)["status"], "ready")

        print("5. 校验失败应判 contradicted 并整体 failed")
        patch_case(case, lambda d: [c["check"].update({"run": "false"})
                                    for c in d["claims"] if c["id"] == "C3"])
        r = run("case", "check", "--case", str(case))
        expect("failed 的退出码", r.returncode, 2)
        expect("C3 被推翻", claim_status(case, "C3"), "contradicted")
        expect("汇总状态", verdict_of(case)["status"], "failed")

        print("6. 引用不存在的证据应判 dangling")
        patch_case(case, lambda d: [c.update({"evidence": ["ev_forged"]})
                                    for c in d["claims"] if c["id"] == "C3"])
        r = run("case", "check", "--case", str(case))
        expect("dangling 的退出码", r.returncode, 2)
        expect("C3 判定", claim_status(case, "C3"), "dangling")

        print("7. 报告与列表")
        r = run("case", "report", "--case", str(case))
        expect("report 退出码", r.returncode, 0)
        report = (case / "REPORT.md").read_text()
        expect("报告含判定", "**failed**" in report, True)
        r = run("case", "list", "--dir", str(tmp))
        expect("list 退出码", r.returncode, 0)
        expect("list 含案件", "echo" in r.stdout, True)

        print("8. SPEC.md 是生成物，判定结果要落进去")
        spec = (case / "SPEC.md").read_text()
        expect("SPEC 标注状态", "**failed**" in spec, True)
        expect("SPEC 禁止手改", "请勿手改" in spec, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
