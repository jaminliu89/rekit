#!/usr/bin/env python3
"""rekit — 逆向工程链路一键部署器。

把 REA（逆向证据引擎）+ Craft Anything（重建闭环）+ REAP 技能包
部署到本机所有支持的 Agent，并保持幂等。

设计原则：
1. 能交给官方安装器的就交给官方安装器（rea setup / craft setup / dsh plugin），
   只在没有官方通道的地方（DSH bundle、reasonix TOML）手写配置。
2. 任何改动前先备份为 *.rekit-backup；重复执行不产生变化。
3. 不改全局 JAVA_HOME（Ghidra 要 21，很多项目要 17），只在启动 rea 时注入。

用法：
    python3 rekit.py doctor
    python3 rekit.py plan
    python3 rekit.py install
    python3 rekit.py verify
    python3 rekit.py agents
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HOME = Path.home()

# 与 dsh-plugin-rea/package.json 的 version 保持一致（CI 会校验）
__version__ = "0.2.0"

# ---------------------------------------------------------------- 常量

REA_BIN = HOME / ".npm-global/bin/rea"
REA_MODULE = HOME / ".npm-global/lib/node_modules/rea-agents/scripts/rea.mjs"
GHIDRA_DIR = HOME / "tools/ghidra_12.1.4_PUBLIC"
JAVA_HOME = "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"
HOPPER = "/Applications/Hopper Disassembler.app/Contents/MacOS/hopper"
CRAFT_BIN = "/Applications/Craft Anything.app/Contents/MacOS/craft-anything-cli"
NPM = "/opt/homebrew/bin/npm"
BREW_NODE = "/opt/homebrew/bin/node"
DSH_CLI = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
# DSH 插件源码：优先用仓库内自带的副本（clone 下来就能直接跑），
# 其次回退到开发机上的独立目录。REKIT_PLUGIN_SRC 可以强行指定。
_HERE = Path(__file__).resolve().parent
DSH_PLUGIN_SRC = next(
    (
        p for p in (
            Path(os.environ["REKIT_PLUGIN_SRC"]) if os.environ.get("REKIT_PLUGIN_SRC") else None,
            _HERE / "dsh-plugin-rea",
            HOME / "Developer/Codex/dsh-plugin-rea",
        )
        if p is not None and (p / "index.js").exists()
    ),
    _HERE / "dsh-plugin-rea",
)
REAP_SRC = HOME / "Developer/Codex/REA-Prompts/Skills"

# 技能源：REA 取自 npm 包本体（永远与版本同步），Craft 取自官方 setup 落下的副本
REA_SKILL_SRC = HOME / ".npm-global/lib/node_modules/rea-agents/skills/reverse-engineer-anything"
CRAFT_SKILL_SRC = HOME / ".claude/skills/craft-anything"
# 分发前先快照到这里，避免「源目录恰好就是分发目标」时自己删掉自己
SKILL_STAGE = HOME / ".cache/rekit/skills"
# 暂存目录名（短）与落盘目录名（规范）的映射
SKILL_NAMES = {"rea": "reverse-engineer-anything", "craft": "craft-anything"}

REA_ENV = {
    "GHIDRA_INSTALL_DIR": str(GHIDRA_DIR),
    "JAVA_HOME": JAVA_HOME,
    "HOPPER_LAUNCHER_PATH": HOPPER,
    "REA_ANALYSIS_PROVIDER": "ghidra",
}

# Agent 注册表：name -> 适配器与路径
# kind: json（mcpServers 风格）/ codex_toml / dsh / reasonix
# servers_path: JSON 里服务器字典的位置，默认 ["mcpServers"]
AGENTS: dict[str, dict] = {
    "claude_code": {"label": "Claude Code", "kind": "json", "path": HOME / ".claude.json", "craft": "claude_code"},
    "cursor": {"label": "Cursor", "kind": "json", "path": HOME / ".cursor/mcp.json", "craft": "cursor"},
    "workbuddy": {"label": "WorkBuddy", "kind": "json", "path": HOME / ".workbuddy/mcp.json", "craft": "workbuddy"},
    "kimi_code": {"label": "Kimi Code", "kind": "json", "path": HOME / ".kimi-code/mcp.json", "craft": "kimi_code"},
    "qwen_code": {"label": "Qwen Code", "kind": "json", "path": HOME / ".qwen/settings.json", "craft": "qwen_code"},
    "zcode": {"label": "ZCode", "kind": "json", "path": HOME / ".zcode/cli/config.json",
              "servers_path": ["mcp", "servers"], "craft": "zcode"},
    "codex": {"label": "Codex", "kind": "codex_toml", "path": HOME / ".codex/config.toml", "craft": "codex"},
    "dsh": {"label": "DeepSeek Harness", "kind": "dsh", "path": HOME / ".dsh", "craft": None},
    "reasonix": {"label": "reasonix", "kind": "reasonix", "path": HOME / ".reasonix/config.toml", "craft": None},
}

# 技能分发目标
SKILL_DIRS = {
    "~/.claude/skills": ["reap", "rea", "craft"],
    "~/.codex/skills": ["reap", "rea"],
    "~/.hermes/skills": ["reap", "rea"],
    "~/.workbuddy-ai/skills": ["reap", "craft"],
    "~/.workbuddy/skills": ["rea", "craft"],
    "~/.dsh/skills": ["reap", "rea", "craft"],
    "~/.reasonix/skills": ["reap", "rea", "craft"],
}

C = {
    "ok": "\033[32m✓\033[0m",
    "bad": "\033[31m✗\033[0m",
    "warn": "\033[33m!\033[0m",
    "dim": "\033[2m",
    "off": "\033[0m",
}


# ---------------------------------------------------------------- 工具函数

def run(cmd: list[str], timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    """执行命令，返回 (退出码, 合并输出)。不抛异常。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd[:2])}"


def rea(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """调用 rea，强制注入 provider 环境。"""
    return run([BREW_NODE, str(REA_MODULE), *args], timeout=timeout, env=REA_ENV)


def backup(path: Path) -> None:
    """首次改动前备份一次，不覆盖已有备份。"""
    if not path.exists():
        return
    bak = path.with_suffix(path.suffix + ".rekit-backup")
    if not bak.exists():
        shutil.copy2(path, bak)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path: Path, data: dict) -> None:
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- 探测

@dataclass
class Report:
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (状态, 名称, 说明)

    def add(self, ok: bool | None, name: str, detail: str = "") -> None:
        self.rows.append(("ok" if ok is True else "bad" if ok is False else "warn", name, detail))

    def render(self) -> str:
        width = max((len(n) for _, n, _ in self.rows), default=10) + 2
        out = []
        for state, name, detail in self.rows:
            out.append(f"  {C[state]} {name.ljust(width)} {C['dim']}{detail}{C['off']}")
        return "\n".join(out)


def node_version() -> tuple[int, ...] | None:
    code, out = run([BREW_NODE, "-v"])
    if code != 0:
        return None
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def node_ok(ver: tuple[int, ...] | None) -> bool:
    """REA 6.8.0 engines: ^22.23.4 || ^24.22.1 || >=26.12.1"""
    if not ver:
        return False
    major, minor, patch = ver
    if major == 22:
        return (minor, patch) >= (23, 4)
    if major == 24:
        return (minor, patch) >= (22, 1)
    if major >= 26:
        return (major, minor, patch) >= (26, 12, 1)
    return major > 26


def rea_version() -> str | None:
    code, out = rea(["--version"], timeout=60)
    return out.strip().splitlines()[0] if code == 0 else None


def craft_doctor() -> dict | None:
    if not Path(CRAFT_BIN).exists():
        return None
    code, out = run([CRAFT_BIN, "doctor"], timeout=180)
    try:
        return json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return None


def dsh_profile_dir(profile: str) -> Path:
    return HOME / ".dsh/profiles" / profile


def dsh_plugin_installed(profile: str) -> bool:
    nm = dsh_profile_dir(profile) / "node_modules/dsh-plugin-rea"
    pkg = dsh_profile_dir(profile) / "package.json"
    if not nm.exists() or not pkg.exists():
        return False
    bundles = load_json(pkg).get("dsh", {}).get("profile", {}).get("bundles", [])
    return "dsh-plugin-rea" in bundles


def reasonix_has(name: str) -> bool:
    toml = HOME / ".reasonix/config.toml"
    if not toml.exists():
        return False
    return re.search(rf'\[\[plugins\]\]\s*\n\s*name\s*=\s*"{re.escape(name)}"', toml.read_text()) is not None


def _servers_dict(data: dict, servers_path: list[str]) -> dict:
    """按 servers_path 逐层取出（必要时创建）服务器字典。"""
    node = data
    for key in servers_path:
        node = node.setdefault(key, {})
    return node


def json_has_rea(path: Path, servers_path: list[str] | None = None) -> bool:
    return "rea" in _servers_dict(load_json(path), servers_path or ["mcpServers"])


# ---------------------------------------------------------------- 各步骤

def step_node(report: Report, apply: bool) -> bool:
    ver = node_version()
    if node_ok(ver):
        report.add(True, "node", "v" + ".".join(map(str, ver)))
        return True
    if not apply:
        report.add(False, "node", f"{ver} 不满足 REA 要求，需 brew upgrade node")
        return False
    code, _ = run(["/opt/homebrew/bin/brew", "upgrade", "node"], timeout=900)
    ver = node_version()
    ok = node_ok(ver)
    report.add(ok, "node", "v" + ".".join(map(str, ver)) if ver else "升级失败")
    return ok


def step_rea(report: Report, apply: bool) -> bool:
    ver = rea_version()
    if ver and not apply:
        report.add(True, "rea", f"v{ver}")
        return True
    if apply:
        code, out = run([NPM, "i", "-g", "rea-agents@latest"], timeout=600)
        ver = rea_version()
        report.add(bool(ver), "rea", f"v{ver}" if ver else out[-160:])
        return bool(ver)
    report.add(False, "rea", "未安装")
    return False


def step_providers(report: Report, apply: bool) -> None:
    report.add(GHIDRA_DIR.exists(), "ghidra", str(GHIDRA_DIR) if GHIDRA_DIR.exists() else "缺；brew install --cask ghidra")
    report.add(Path(JAVA_HOME).exists(), "jdk21", JAVA_HOME)
    report.add(Path(HOPPER).exists(), "hopper", HOPPER)
    for name, cmd in (("jadx", "/opt/homebrew/bin/jadx"), ("apktool", "/opt/homebrew/bin/apktool")):
        report.add(Path(cmd).exists(), name, cmd)


def step_craft(report: Report, apply: bool) -> bool:
    d = craft_doctor()
    if d is None:
        report.add(False, "craft-anything", "未安装")
        return False
    report.add(bool(d.get("healthy")), "craft-anything", f"healthy={d.get('healthy')}")
    return True


def _copy_skill(src: Path, dst: Path) -> bool:
    """把技能目录复制到 dst。

    dst 可能是软链、文件或目录，一律先清掉；若 dst 与 src 指向同一份则跳过，
    否则会出现「先 rmtree 掉源目录再 copytree」的自杀式崩溃。
    """
    if not src.is_dir():
        return False
    try:
        if dst.exists() and dst.resolve() == src.resolve():
            return False
    except OSError:
        pass
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return True


def stage_skills() -> dict[str, Path]:
    """把 rea / craft 技能快照到暂存区，返回 kind -> 快照路径。

    Craft 的官方技能默认就落在 ~/.claude/skills/craft-anything，而它同时也是一个
    分发目标；先快照再分发才能既保留源头又不出自毁。
    """
    SKILL_STAGE.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    for kind, src in (("rea", REA_SKILL_SRC), ("craft", CRAFT_SKILL_SRC)):
        dst = SKILL_STAGE / kind
        if src.is_dir():
            _copy_skill(src, dst)
        if dst.is_dir():
            staged[kind] = dst
    return staged


def count_skills() -> int:
    """数一遍各 skills 目录里已经落地的 REAP / REA / Craft 技能。"""
    n = 0
    for target, kinds in SKILL_DIRS.items():
        dest = Path(os.path.expanduser(target))
        if not dest.is_dir():
            continue
        for kind in kinds:
            if kind == "reap":
                n += len(list(dest.glob("reap-*")))
            elif (dest / SKILL_NAMES[kind]).is_dir():
                n += 1
    return n


def step_skills(report: Report, apply: bool) -> int:
    """把 REAP / REA / Craft 技能分发到各 agent 目录。返回安装数量。"""
    reap = sorted(REAP_SRC.glob("reap-*")) if REAP_SRC.exists() else []
    staged = stage_skills() if apply else {}
    sources = {"rea": staged.get("rea", REA_SKILL_SRC), "craft": staged.get("craft", CRAFT_SKILL_SRC)}
    missing = [k for k, v in sources.items() if not v.is_dir()]
    total = 0
    for target, kinds in SKILL_DIRS.items():
        dest = Path(os.path.expanduser(target))
        if not dest.parent.exists():
            continue
        if apply:
            dest.mkdir(parents=True, exist_ok=True)
            if "reap" in kinds:
                for s in reap:
                    if _copy_skill(s, dest / s.name):
                        total += 1
            for kind in ("rea", "craft"):
                if kind in kinds and _copy_skill(sources[kind], dest / SKILL_NAMES[kind]):
                    total += 1
    detail = f"{total} 项" if apply else f"已分发 {count_skills()} 项"
    if missing:
        detail += f"；缺源: {', '.join(missing)}"
    report.add(not missing and (total > 0 or not apply), "skills", detail)
    return total


def step_register_json(report: Report, apply: bool, agents: list[str]) -> None:
    """JSON 类 agent：直接写服务器字典（craft 交给官方 setup）。"""
    for key in agents:
        meta = AGENTS[key]
        if meta["kind"] != "json":
            continue
        path: Path = meta["path"]
        if not path.parent.exists():
            continue
        spath = meta.get("servers_path", ["mcpServers"])
        if json_has_rea(path, spath) and not apply:
            report.add(True, f"rea@{key}", str(path))
            continue
        if apply:
            data = load_json(path)
            servers = _servers_dict(data, spath)
            entry = servers.setdefault("rea", {"command": str(REA_BIN), "args": ["mcp"]})
            entry.setdefault("command", str(REA_BIN))
            entry.setdefault("args", ["mcp"])
            entry["env"] = {**entry.get("env", {}), **REA_ENV}
            entry.pop("disabled", None)
            save_json(path, data)
            report.add(True, f"rea@{key}", str(path))
        else:
            report.add(False, f"rea@{key}", "未注册")


def codex_has_rea(path: Path) -> bool:
    if not path.exists():
        return False
    return re.search(r"^\[mcp_servers\.rea\]", path.read_text(), re.M) is not None


def step_register_codex(report: Report, apply: bool) -> None:
    """Codex 用 TOML：追加 [mcp_servers.rea] 段，已存在则跳过。"""
    path: Path = AGENTS["codex"]["path"]
    if not path.exists():
        return
    if codex_has_rea(path):
        if apply:
            # 已存在：重写 env 行
            text = path.read_text()
            backup(path)
            pairs = ", ".join(f'{k} = "{v}"' for k, v in REA_ENV.items())
            text = re.sub(
                r"(\[mcp_servers\.rea\][^\[]*?)(?=\n\[|\Z)",
                lambda m: re.sub(r"^env\s*=.*$", f"env = {{ {pairs} }}", m.group(1), flags=re.M)
                if re.search(r"^env\s*=", m.group(1), re.M)
                else m.group(1).rstrip() + f"\nenv = {{ {pairs} }}\n",
                text, flags=re.S,
            )
            path.write_text(text)
        report.add(True, "rea@codex", str(path))
        return
    if not apply:
        report.add(False, "rea@codex", "未注册")
        return
    backup(path)
    pairs = ", ".join(f'{k} = "{v}"' for k, v in REA_ENV.items())
    block = (
        "\n[mcp_servers.rea]\n"
        f'command = "{REA_BIN}"\n'
        'args = ["mcp"]\n'
        f"env = {{ {pairs} }}\n"
    )
    path.write_text(path.read_text().rstrip() + "\n" + block)
    report.add(True, "rea@codex", str(path))


def step_register_reasonix(report: Report, apply: bool) -> None:
    toml = HOME / ".reasonix/config.toml"
    if not toml.exists():
        return
    targets = [("rea", str(REA_BIN), ["mcp"], REA_ENV), ("craft-anything", CRAFT_BIN, ["mcp"], {})]
    if apply:
        backup(toml)
        text = toml.read_text()
        for name, cmd, args, env in targets:
            if reasonix_has(name):
                # 已存在：只补 env
                text = _ensure_toml_env(text, name, env)
            else:
                block = _toml_block(name, cmd, args, env)
                text = text.rstrip() + "\n\n" + block + "\n"
        toml.write_text(text)
        report.add(True, "reasonix", "rea + craft-anything")
    else:
        ok = reasonix_has("rea") and reasonix_has("craft-anything")
        report.add(ok, "reasonix", "已注册" if ok else "未注册")


def _toml_block(name: str, cmd: str, args: list[str], env: dict) -> str:
    arglist = ", ".join(f'"{a}"' for a in args)
    lines = ["[[plugins]]", f'name    = "{name}"', f'command = "{cmd}"', f"args    = [{arglist}]"]
    if env:
        pairs = ", ".join(f'{k} = "{v}"' for k, v in env.items())
        lines.append(f"env     = {{ {pairs} }}")
    return "\n".join(lines)


def _ensure_toml_env(text: str, name: str, env: dict) -> str:
    if not env:
        return text
    pattern = re.compile(
        rf'(\[\[plugins\]\]\s*\n\s*name\s*=\s*"{re.escape(name)}"[^\[]*?)(?=\n\[\[|\Z)',
        re.S,
    )
    m = pattern.search(text)
    if not m:
        return text
    block = m.group(1)
    pairs = ", ".join(f'{k} = "{v}"' for k, v in env.items())
    if re.search(r"^env\s*=", block, re.M):
        block = re.sub(r"^env\s*=.*$", f"env     = {{ {pairs} }}", block, flags=re.M)
    else:
        block = block.rstrip() + f"\nenv     = {{ {pairs} }}"
    return text[: m.start(1)] + block + text[m.end(1):]


def step_dsh(report: Report, apply: bool, profile: str) -> None:
    """DSH：把 dsh-plugin-rea 实体拷贝进 profile 并登记 bundle。"""
    pdir = dsh_profile_dir(profile)
    if not pdir.exists():
        report.add(None, "dsh", f"profile {profile} 不存在，跳过")
        return
    if dsh_plugin_installed(profile) and not apply:
        report.add(True, "dsh", f"plugin 已装（{profile}）")
        return
    if not apply:
        report.add(False, "dsh", "plugin 未装")
        return
    if not DSH_PLUGIN_SRC.exists():
        report.add(False, "dsh", f"插件源码缺失：{DSH_PLUGIN_SRC}")
        return
    # pnpm 建的是软链，Node 按真实路径解析会找不到 dsh-mcp-client，所以实体拷贝
    dest = pdir / "node_modules/dsh-plugin-rea"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(DSH_PLUGIN_SRC, dest)
    pkg = pdir / "package.json"
    data = load_json(pkg)
    data.setdefault("dependencies", {})["dsh-plugin-rea"] = "file:./node_modules/dsh-plugin-rea"
    bundles = data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    if "dsh-plugin-rea" not in bundles:
        bundles.append("dsh-plugin-rea")
    save_json(pkg, data)
    report.add(True, "dsh", f"plugin 已装（{profile}），重启后生效")


# 直接 import 插件并调 apply()，确认 cordis 契约与两个 server 配置都成立。
# 这比「文件在不在」有意义：依赖解析、导出名、inject 都会在这一步暴露。
DSH_PROBE_JS = """
const mod = await import(process.argv[2])
const got = []
const ctx = { plugin: (p, cfg) => got.push({ plugin: p?.name, server: cfg?.serverName, command: cfg?.command }) }
mod.apply(ctx, {})
console.log(JSON.stringify({ name: mod.name, servers: got }))
"""


def dsh_probe(profile: str) -> tuple[bool, str]:
    """用 node 真跑一遍插件，返回 (是否通过, 说明)。"""
    plug = dsh_profile_dir(profile) / "node_modules/dsh-plugin-rea/index.js"
    if not plug.exists():
        return False, f"插件未装进 profile[{profile}]"
    script = Path(tempfile.gettempdir()) / "rekit_dsh_probe.mjs"
    script.write_text(DSH_PROBE_JS)
    code, out = run([BREW_NODE, str(script), str(plug)], timeout=120)
    if code != 0:
        first = (out.strip().splitlines() or [""])[0]
        return False, f"apply 失败：{first[:100]}"
    try:
        data = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return False, "探针输出无法解析"
    names = [s["server"] for s in data.get("servers", [])]
    return bool(names), f"{data.get('name')} → {', '.join(names) or '无 server'}"


def step_craft_register(report: Report, apply: bool, agents: list[str]) -> None:
    clients = [AGENTS[a]["craft"] for a in agents if AGENTS[a].get("craft")]
    if not Path(CRAFT_BIN).exists() or not clients:
        return
    if not apply:
        report.add(None, "craft@agents", "待注册")
        return
    cmd = [CRAFT_BIN, "setup", "--yes"]
    for c in clients:
        cmd += ["--client", c]
    code, out = run(cmd, timeout=300)
    report.add(code == 0, "craft@agents", f"{len(clients)} 个客户端" if code == 0 else out[-160:])


# ---------------------------------------------------------------- 案件工作台
#
# 把 S0–S5 从文档变成可执行的三步：
#   rekit case new      生成案件目录（SCOPE.md + case.json），锁定 sha256
#   rekit case capture  调 rea 取证，登记 evidence_id
#   rekit case check    逐条判定 claim，fail-closed
#
# 判定规则是这套工具的核心，刻意保守：
#   - 没有 evidence_id 的断言最多只能到 characterized，永远到不了 verified
#   - 引用了未登记的 evidence_id 直接判 dangling（等于伪造证据）
#   - 校验命令能跑就真跑，退出码不符判 contradicted
# 这条规则的意义在于：结论不可能比证据更强。

CASES_ROOT = Path(os.environ.get("REKIT_CASES") or (HOME / ".rekit/cases"))
CASE_SCHEMA = "rekit.case/1"
VERDICT_SCHEMA = "rekit.verdict/1"
CLAIM_KINDS = ("behavior", "interface", "data", "error", "side-effect", "constraint")
# 判定严重度：数字越大越糟，汇总时取最糟的一条
SEVERITY = {"verified": 0, "characterized": 1, "unknown": 2, "dangling": 3, "contradicted": 4}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_target(path: Path) -> dict:
    """目标可能是单文件，也可能是 .app / JS 工程这种目录。

    目录用「相对路径 + 大小 + 内容哈希」的确定性摘要，超过预算就停止读内容并标记
    truncated——宁可标注能力边界，也不假装算完了。
    """
    if path.is_file():
        return {"sha256": sha256_of(path), "size": path.stat().st_size, "files": 1, "truncated": False}

    h = hashlib.sha256()
    total = files = 0
    truncated = False
    budget = 512 << 20
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(names):
            fp = Path(root) / name
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(path).as_posix()
            size = fp.stat().st_size
            files += 1
            total += size
            h.update(f"{rel}\0{size}\0".encode())
            if truncated or total > budget:
                truncated = True
                h.update(b"-")
            else:
                h.update(bytes.fromhex(sha256_of(fp)))
    return {"sha256": h.hexdigest(), "size": total, "files": files, "truncated": truncated}


def file_kind(path: Path) -> str:
    """取一行格式描述。universal 时 file(1) 会吐整段架构清单，压成一句。"""
    exe = shutil.which("file") or "/usr/bin/file"
    code, out = run([exe, "-b", str(path)], timeout=30)
    if code != 0:
        return ""
    first = out.strip().splitlines()[0].strip() if out.strip() else ""
    if "universal binary" in first:
        code, archs = run(["/usr/bin/lipo", "-archs", str(path)], timeout=30)
        return f"Mach-O universal ({', '.join(archs.split())})" if code == 0 else "Mach-O universal"
    return first


def thin_universal(path: Path, work: Path) -> tuple[Path, str | None]:
    """Ghidra provider 对 universal Mach-O 直接拒绝，先切成单片。

    系统二进制常见 arm64e（不是 arm64），优先级按这个来。
    """
    if "universal" not in file_kind(path):
        return path, None
    code, out = run(["/usr/bin/lipo", "-archs", str(path)], timeout=30)
    archs = out.split() if code == 0 else []
    for arch in ("arm64e", "arm64", "x86_64"):
        if arch not in archs:
            continue
        work.mkdir(parents=True, exist_ok=True)
        dest = work / f"{path.name}.{arch}"
        code, _ = run(["/usr/bin/lipo", "-thin", arch, str(path), "-output", str(dest)], timeout=90)
        if code == 0:
            return dest, arch
    return path, None


def slugify(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-").lower() or "case"


def case_path(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser()
        return (p if p.is_absolute() else Path.cwd() / p).resolve()
    if (Path.cwd() / "case.json").exists():
        return Path.cwd()
    raise SystemExit("找不到案件：加 --case <目录>，或先进到案件目录里执行")


def load_case(d: Path) -> dict:
    f = d / "case.json"
    if not f.exists():
        raise SystemExit(f"{d} 里没有 case.json，先 rekit case new")
    return load_json(f)


def save_case(d: Path, data: dict) -> None:
    data["updated"] = _now()
    (d / "case.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_verdict(d: Path) -> dict | None:
    f = d / "verdict.json"
    return load_json(f) if f.exists() else None


def collect_evidence_ids(node, acc: list | None = None) -> list:
    """证据 id 可能出现在任意深度，递归全收，去重保序。"""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "evidence_id" and isinstance(v, str):
                acc.append(v)
            else:
                collect_evidence_ids(v, acc)
    elif isinstance(node, list):
        for x in node:
            collect_evidence_ids(x, acc)
    return list(dict.fromkeys(acc))


def parse_json_payload(text: str):
    """从 rea 的输出里捞出最终 JSON。

    rea 会先吐若干 JSONL 进度行（{"rea_progress": …}）再吐结果，
    所以不能简单地「从第一个 { 开始 json.loads」——那样解析到的是进度行。
    """
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    best = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "rea_progress" not in obj:
            best = obj
    if best is not None:
        return best

    # 兜底：逐位置 raw_decode，取最后一个能完整解析的对象
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "rea_progress" not in obj:
            best = obj
    return best


def cell(text: str) -> str:
    """Markdown 表格单元格：竖线要转义，换行压平。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def write_scope(d: Path, data: dict) -> None:
    t = data["target"]
    s = data["scope"]
    thin = f"`{t['effective_path']}`（{t['arch']} 单片）" if t.get("arch") else "原件（非 universal）"
    scale = f"{t['size']} 字节"
    if t.get("files", 1) > 1:
        scale = f"{t['size']} 字节 / {t['files']} 个文件"
    if t.get("truncated"):
        scale += "（超过 512MB 预算，只摘要了部分内容）"
    body = f"""# SCOPE — {data['name']}

> S0 范围与授权。这份文件是给人和审计看的，判定不读它。

## 目标

| 项 | 值 |
|---|---|
| 路径 | `{t['path']}` |
| sha256 | `{t['sha256']}` |
| 规模 | {scale} |
| 格式 | {cell(t['kind'])} |
| 分析用 | {thin} |
| provider | `{data['provider']}` |

## 授权

| 项 | 值 |
|---|---|
| 授权人 | {s.get('authorized_by') or '（待填）'} |
| 时间盒 | {s.get('timebox_hours')} 小时 |
| 允许动作 | {', '.join(s.get('allowed') or [])} |
| 禁止动作 | {', '.join(s.get('forbidden') or [])} |

> 只分析你有权分析的产物。sha256 是这次工作的锚点：目标文件一变，
> 之前所有证据都不再对应当前字节，必须重开案件。

## 完成标准

`rekit case check` 返回 `ready`：所有必需 claim 都被证据 + 校验同时支撑。
返回 `open` 表示还有断言缺证据，`failed` 表示有断言被证据推翻或引用了不存在的证据。
"""
    (d / "SCOPE.md").write_text(body)


def write_spec(d: Path, data: dict, verdict: dict | None) -> None:
    """SPEC.md 是生成物，不是输入。避免 markdown 解析与 case.json 双份维护。"""
    rows = []
    by_id = {c["id"]: c for c in verdict.get("claims", [])} if verdict else {}
    for c in data.get("claims", []):
        v = by_id.get(c["id"], {})
        ev = c.get("evidence") or []
        ev_cell = ", ".join(f"`{e[:14]}…`" for e in ev[:2])
        if len(ev) > 2:
            ev_cell += f" +{len(ev) - 2}"
        rows.append(
            f"| {c['id']} | {c['kind']} | {'是' if c['required'] else '否'} | {cell(c['statement'])} "
            f"| {v.get('verdict', '—')} | {ev_cell or '—'} | {cell(v.get('reason', c.get('note', '')))} |"
        )
    if not rows:
        rows = ["| — | — | — | （还没有断言，用 `rekit case claim` 加） | — | — | — |"]

    status = verdict["status"] if verdict else "unjudged"
    summary = ""
    if verdict:
        s = verdict["summary"]
        summary = (f"\n判定：**{status}** — 必需 {s['required']} 条，"
                   f"verified {s['verified']} 条，未决 {s['required_open']} 条。\n")
    body = f"""# SPEC — {data['name']}

> 由 `rekit case check` 生成，请勿手改。改 case.json，或用 `rekit case claim` 追加断言。

目标：`{data['target']['path']}`
sha256：`{data['target']['sha256']}`
provider：`{data['provider']}`
{summary}
| ID | 类型 | 必需 | 断言 | 判定 | 证据 | 说明 |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

## 判定口径

- **verified**：有 evidence_id，且校验命令通过。唯一能算「做完了」的状态。
- **characterized**：有证据但没做校验，或校验通过但没有证据锚点。
- **unknown**：没证据。
- **dangling**：引用了未登记的 evidence_id。
- **contradicted**：校验命令退出码不符，断言被推翻。

没有证据的断言写进 SPEC 不算错，但永远不会被算作已验证——这就是 fail-closed。
"""
    (d / "SPEC.md").write_text(body)


def judge_claim(claim: dict, evidence: dict, cwd: Path) -> dict:
    ids = claim.get("evidence") or []
    missing = [e for e in ids if e not in evidence]
    if missing:
        return {"verdict": "dangling", "reason": f"证据未登记：{', '.join(missing)}"}
    check = claim.get("check") or {"kind": "manual"}
    if check.get("kind") == "shell":
        code, out = run_shell(check.get("run", ""), cwd, timeout=check.get("timeout", 120))
        want = check.get("expect_exit", 0)
        if code != want:
            tail = out.strip().splitlines()[-1][:60] if out.strip() else "无输出"
            return {"verdict": "contradicted", "reason": f"校验退出码 {code}（期望 {want}）：{tail}"}
        if not ids:
            return {"verdict": "characterized", "reason": "校验通过，但没有证据锚点"}
        return {"verdict": "verified", "reason": "证据 + 校验均通过"}
    if ids:
        return {"verdict": "characterized", "reason": "已取证，未做校验"}
    return {"verdict": "unknown", "reason": "无证据"}


def run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(["/bin/sh", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def summarize(rows: list[dict]) -> dict:
    required = [r for r in rows if r["required"]]
    worst = max((SEVERITY[r["verdict"]] for r in rows), default=0)
    if any(r["verdict"] in ("contradicted", "dangling") for r in rows):
        status = "failed"
    elif not rows:
        status = "unknown"
    elif any(r["verdict"] != "verified" for r in required):
        status = "open"
    else:
        status = "ready"
    return {
        "status": status,
        "summary": {
            "total": len(rows),
            "required": len(required),
            "verified": sum(1 for r in rows if r["verdict"] == "verified"),
            "required_open": sum(1 for r in required if r["verdict"] != "verified"),
            "worst_severity": worst,
        },
    }


# ---------------------------------------------------------------- case 子命令

def case_new(args) -> int:
    target = Path(args.target).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if not target.exists():
        print(f"目标不存在：{target}")
        return 2

    name = args.name or target.name
    root = Path(args.dir).expanduser() if args.dir else CASES_ROOT
    d = root / slugify(name)
    if (d / "case.json").exists() and not args.force:
        print(f"案件已存在：{d}（加 --force 覆盖）")
        return 2
    (d / "evidence").mkdir(parents=True, exist_ok=True)

    effective, arch = (target, None) if args.no_thin else thin_universal(target, d / "work")
    digest = digest_target(target)
    eff_digest = digest_target(effective)
    data = {
        "schema": CASE_SCHEMA,
        "name": name,
        "created": _now(),
        "target": {
            "path": str(target),
            "sha256": digest["sha256"],
            "size": digest["size"],
            "files": digest["files"],
            "truncated": digest["truncated"],
            "kind": file_kind(target),
            "arch": arch,
            "effective_path": str(effective),
            "effective_sha256": eff_digest["sha256"],
        },
        "provider": args.provider,
        "scope": {
            "authorized_by": "",
            "allowed": ["静态分析", "切片", "反编译", "本地快照"],
            "forbidden": ["执行目标", "联网抓取", "修改原件"],
            "timebox_hours": args.timebox,
        },
        "claims": [],
        "evidence": {},
        "captures": [],
    }
    save_case(d, data)
    write_scope(d, data)
    write_spec(d, data, None)

    t = data["target"]
    print(f"{C['ok']} 案件 {d}")
    print(f"  {C['dim']}目标  {t['path']}{C['off']}")
    print(f"  {C['dim']}sha256 {t['sha256'][:32]}…{C['off']}")
    print(f"  {C['dim']}格式  {cell(t['kind'])}{C['off']}")
    if t.get("files", 1) > 1:
        print(f"  {C['dim']}规模  {t['files']} 个文件 / {t['size']} 字节{C['off']}")
    if t.get("truncated"):
        print(f"  {C['warn']} 超过 512MB 摘要预算，哈希只覆盖了部分内容{C['off']}")
    if arch:
        print(f"  {C['dim']}切片  {t['effective_path']}（{arch}）{C['off']}")
    print(f"\n下一步：{C['dim']}rekit case capture --case {d}{C['off']}")
    return 0


def error_envelope(payload, ids: list) -> tuple[bool, str, str]:
    """识别 rea 的两种失败信封。

    一种带 error 字段，另一种是 {"code": "COMMAND_NOT_FOUND", "message": …}。
    后者没有 error 键，只看 error 字段会把它当成成功——正是踩过的坑。
    """
    if not isinstance(payload, dict):
        return False, "", ""
    if payload.get("error"):
        return True, str(payload.get("code") or payload["error"]), str(payload.get("message") or payload["error"])
    if not ids and isinstance(payload.get("code"), str) and payload.get("message"):
        return True, payload["code"], str(payload["message"])
    return False, "", ""


def case_capture(args) -> int:
    d = case_path(args.case)
    data = load_case(d)
    target = data["target"]["effective_path"]
    provider = args.provider or data.get("provider") or "ghidra"

    extra = list(args.extra)
    while extra and extra[0] == "--":  # REMAINDER 会把分隔符也带进来
        extra.pop(0)

    if extra:
        cmd = extra
        if not any(a in ("--format", "--json") for a in cmd):
            cmd = cmd + ["--format", "json"]
        label = args.label or cmd[0]
    else:
        cmd = ["inspect", target, "--provider", provider, "--format", "json"]
        label = args.label or "inspect"

    code, out = rea(cmd, timeout=args.timeout)
    payload = parse_json_payload(out)
    n = len(data.get("captures", [])) + 1
    stamp = _now()
    ids = collect_evidence_ids(payload) if payload is not None else []
    failed, err_code, err_msg = error_envelope(payload, ids)

    if payload is None or failed:
        if payload is None:
            print(f"{C['bad']} {label}：没有解析出 JSON")
            print(f"  {C['dim']}{out.strip()[:300]}{C['off']}")
            err_code, err_msg = "no-json", ""
        else:
            print(f"{C['bad']} {label}：{brief(err_msg, 90)}")
            detail = (payload.get("details") or {}).get("cleanup_reason") or ""
            if detail:
                print(f"  {C['dim']}{brief(detail)}{C['off']}")
            print(f"  {C['dim']}code={err_code} retryable={payload.get('retryable')}{C['off']}")
        data.setdefault("captures", []).append({
            "label": label, "command": cmd, "ok": False, "at": stamp,
            "error": err_code, "message": err_msg,
        })
        save_case(d, data)
        return 1

    rel = f"evidence/{n:02d}-{slugify(label)}.json"
    (d / rel).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    for eid in ids:
        data.setdefault("evidence", {})[eid] = {
            "command": "rea " + " ".join(cmd), "at": stamp, "file": rel, "label": label,
        }
    data.setdefault("captures", []).append({
        "label": label, "command": cmd, "ok": bool(ids), "at": stamp,
        "evidence_ids": ids, "file": rel,
        **({} if ids else {"error": "no-evidence"}),
    })
    save_case(d, data)
    write_spec(d, data, read_verdict(d))

    if not ids:
        print(f"{C['bad']} {label}：跑通了但没产出 evidence_id，这条结果不能当证据用")
        print(f"  {C['dim']}原始输出已存 {rel}，可以打开看它到底返回了什么{C['off']}")
        return 1
    print(f"{C['ok']} {label}：{len(ids)} 条证据 → {rel}")
    for eid in ids[:4]:
        print(f"  {C['dim']}{eid}{C['off']}")
    if len(ids) > 4:
        print(f"  {C['dim']}… 还有 {len(ids) - 4} 条{C['off']}")
    print(f"\n下一步：{C['dim']}rekit case claim --statement '…' --evidence {ids[0]}{C['off']}")
    return 0


def case_claim(args) -> int:
    d = case_path(args.case)
    data = load_case(d)
    claims = data.setdefault("claims", [])
    cid = args.id or f"C{len(claims) + 1}"
    if any(c["id"] == cid for c in claims):
        print(f"断言已存在：{cid}")
        return 2

    known = data.get("evidence", {})
    unknown = [e for e in (args.evidence or []) if e not in known]
    if unknown:
        print(f"证据未登记：{', '.join(unknown)}")
        print(f"  {C['dim']}先 rekit case capture，或核对 evidence id{C['off']}")
        return 2

    claim = {
        "id": cid,
        "kind": args.kind,
        "statement": args.statement,
        "required": not args.optional,
        "evidence": args.evidence or [],
        "check": ({"kind": "shell", "run": args.check, "expect_exit": 0}
                  if args.check else {"kind": "manual"}),
        "note": args.note or "",
    }
    claims.append(claim)
    save_case(d, data)
    write_spec(d, data, read_verdict(d))
    print(f"{C['ok']} {cid} 已加入（{'必需' if claim['required'] else '可选'}，"
          f"{len(claim['evidence'])} 条证据，{'带校验' if args.check else '人工判定'}）")
    return 0


def case_check(args) -> int:
    d = case_path(args.case)
    data = load_case(d)
    rows = []
    for c in data.get("claims", []):
        v = judge_claim(c, data.get("evidence", {}), d)
        rows.append({
            "id": c["id"], "kind": c["kind"], "statement": c["statement"],
            "required": bool(c.get("required", True)), "evidence": c.get("evidence") or [],
            **v,
        })

    result = summarize(rows)
    verdict = {
        "schema": VERDICT_SCHEMA,
        "case": data["name"],
        "at": _now(),
        "target_sha256": data["target"]["sha256"],
        "status": result["status"],
        "summary": result["summary"],
        "claims": rows,
    }
    (d / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n")
    write_spec(d, data, verdict)

    color = {"ready": C["ok"], "open": C["warn"], "failed": C["bad"], "unknown": C["warn"]}[result["status"]]
    print(f"判定 {color} {result['status']}{C['off']}  {C['dim']}{d}{C['off']}")
    if rows:
        width = max(len(r["id"]) for r in rows) + 1
        for r in rows:
            mark = C["ok"] if r["verdict"] == "verified" else (
                C["bad"] if r["verdict"] in ("contradicted", "dangling") else C["warn"])
            flag = "" if r["required"] else C["dim"] + "(可选) "
            print(f"  {mark} {r['id'].ljust(width)} {flag}{r['verdict']:<14} {C['dim']}{brief(r['reason'], 56)}{C['off']}")
    s = result["summary"]
    print(f"  {C['dim']}共 {s['total']} 条，必需 {s['required']}，verified {s['verified']}，"
          f"未决 {s['required_open']}{C['off']}")
    return {"ready": 0, "open": 1, "failed": 2, "unknown": 1}[result["status"]]


def case_report(args) -> int:
    d = case_path(args.case)
    data = load_case(d)
    verdict = read_verdict(d)
    if verdict is None:
        print("还没有判定结果，先跑 rekit case check")
        return 2

    t, s = data["target"], data["scope"]
    rows = "\n".join(
        f"| {r['id']} | {r['kind']} | {'是' if r['required'] else '否'} | {cell(r['statement'])} "
        f"| **{r['verdict']}** | {cell(r['reason'])} |"
        for r in verdict["claims"]
    ) or "| — | — | — | — | — | — |"
    caps = "\n".join(
        f"| {c['label']} | {'✓' if c['ok'] else '✗'} | {len(c.get('evidence_ids') or [])} | {cell(c.get('error') or '')} |"
        for c in data.get("captures", [])
    ) or "| — | — | — | — |"
    unknown = [r for r in verdict["claims"] if r["verdict"] != "verified"]
    residuals = "\n".join(f"- `{r['id']}` {r['statement']}（{r['verdict']}）" for r in unknown) or "- 无"

    body = f"""# 重建报告 — {data['name']}

判定：**{verdict['status']}**
生成时间：{verdict['at']}
目标 sha256：`{t['sha256']}`
provider：`{data['provider']}`

## 范围

- 授权人：{s.get('authorized_by') or '（未填）'}
- 时间盒：{s.get('timebox_hours')} 小时
- 允许：{', '.join(s.get('allowed') or [])}
- 禁止：{', '.join(s.get('forbidden') or [])}

## 取证记录

| 命令 | 结果 | 证据数 | 备注 |
|---|---|---|---|
{caps}

证据总数：{len(data.get('evidence', {}))}

## 断言判定

| ID | 类型 | 必需 | 断言 | 判定 | 依据 |
|---|---|---|---|---|---|
{rows}

## 残留 UNKNOWN

{residuals}

---

判定口径：只有「有 evidence_id 且校验命令通过」的断言才算 verified。
没有证据的断言不会因为写得像模像样就被采纳——这是 fail-closed 的全部意义。
"""
    (d / "REPORT.md").write_text(body)
    print(f"{C['ok']} {d / 'REPORT.md'}")
    print(f"  {C['dim']}{verdict['status']}，残留 {len(unknown)} 条未验证{C['off']}")
    return 0


def case_list(args) -> int:
    root = Path(args.dir).expanduser() if args.dir else CASES_ROOT
    if not root.exists():
        print(f"{root} 不存在，还没有案件")
        return 0
    found = 0
    for d in sorted(root.iterdir()):
        if not (d / "case.json").exists():
            continue
        found += 1
        data = load_json(d / "case.json")
        verdict = read_verdict(d)
        status = verdict["status"] if verdict else "未判定"
        n = len(data.get("claims", []))
        ev = len(data.get("evidence", {}))
        mark = {"ready": C["ok"], "open": C["warn"], "failed": C["bad"]}.get(status, C["dim"])
        print(f"  {mark} {d.name:<24} {status:<10} {C['dim']}{n} 条断言 / {ev} 条证据  "
              f"{data.get('target', {}).get('path', '')}{C['off']}")
    if not found:
        print(f"{root} 下没有案件")
    return 0


def cmd_case(args) -> int:
    return {
        "new": case_new, "capture": case_capture, "claim": case_claim,
        "check": case_check, "report": case_report, "list": case_list,
    }[args.case_cmd](args)


# ---------------------------------------------------------------- 命令

def cmd_doctor(args) -> int:
    r = Report()
    step_node(r, apply=False)
    step_rea(r, apply=False)
    step_providers(r, apply=False)
    step_craft(r, apply=False)
    print("环境")
    print(r.render())

    r2 = Report()
    for key, meta in AGENTS.items():
        if meta["kind"] == "json":
            p: Path = meta["path"]
            if not p.parent.exists():
                continue
            r2.add(json_has_rea(p, meta.get("servers_path")), f"rea@{key}", str(p))
        elif meta["kind"] == "codex_toml":
            if meta["path"].exists():
                r2.add(codex_has_rea(meta["path"]), f"rea@{key}", str(meta["path"]))
        elif meta["kind"] == "reasonix":
            if meta["path"].exists():
                r2.add(reasonix_has("rea"), "rea@reasonix", str(meta["path"]))
        elif meta["kind"] == "dsh":
            r2.add(dsh_plugin_installed(args.profile), f"plugin@dsh[{args.profile}]", "")
    print("\nAgent 注册")
    print(r2.render())

    r3 = Report()
    step_skills(r3, apply=False)
    print("\n技能")
    print(r3.render())

    code, out = rea(["doctor", "--format", "json"], timeout=180)
    known = {
        "registration:dsh": "DSH 改由 bundle 插件挂载，cordis.patch.yml 里本来就不该再有 rea",
        "registration:commandcode": "本机没装这个客户端",
        "registration:devin": "本机没装这个客户端",
        "ida-registration": "没装 IDA Pro",
        "kuna-installation": "没装 Kuna",
        "jadx": "要 jadx-headless-mcp 的 JAR（REA_JADX_MCP_JAR），不是 brew 的 jadx",
    }
    try:
        d = json.loads(out[out.find("{"):])
    except (ValueError, json.JSONDecodeError):
        return 0
    fails = [c for c in d.get("checks", []) if not c.get("ok")]
    print("\nREA doctor")
    if not fails:
        print(f"  {C['ok']} 全部通过")
        return 0
    expected = [c for c in fails if c["name"] in known]
    real = [c for c in fails if c["name"] not in known]
    for c in real:
        detail = brief(c.get("detail") or "") if c["name"] == "ghidra-java" else (c.get("detail") or "")[:70]
        print(f"  {C['bad']} {c['name']:<26} {detail}")
    for c in expected:
        print(f"  {C['warn']} {c['name']:<26} {known[c['name']]}")
    if any(c["name"] == "ghidra-java" for c in real):
        print(f"  {C['dim']}ghidra-java 是 REA 的独立探测路径，被拦不代表分析不能用；跑 rekit verify 看实际结果{C['off']}")
    return 0


def cmd_agents(args) -> int:
    print(f"{'ID':<12} {'名称':<18} {'类型':<9} 状态")
    for key, meta in AGENTS.items():
        if meta["kind"] == "json":
            exists = meta["path"].parent.exists()
            state = "已注册" if json_has_rea(meta["path"], meta.get("servers_path")) else ("可注册" if exists else "未安装")
        elif meta["kind"] == "codex_toml":
            exists = meta["path"].exists()
            state = "已注册" if codex_has_rea(meta["path"]) else ("可注册" if exists else "未安装")
        elif meta["kind"] == "reasonix":
            exists = meta["path"].exists()
            state = "已注册" if reasonix_has("rea") else ("可注册" if exists else "未安装")
        else:
            exists = dsh_profile_dir(args.profile).exists()
            state = "已装插件" if dsh_plugin_installed(args.profile) else ("可注册" if exists else "未安装")
        print(f"{key:<12} {meta['label']:<18} {meta['kind']:<9} {state}")
    return 0


def cmd_plan(args) -> int:
    print("将执行：")
    print("  1. 校验 node 版本（REA 6.8.0 要求 ^22.23.4 / ^24.22.1 / >=26.12.1）")
    print("  2. npm i -g rea-agents@latest")
    print("  3. 写 mcpServers.rea 到 JSON 类 agent，env 注入 Ghidra/JDK21/Provider")
    print("  4. reasonix：注册 rea 与 craft-anything，补 env")
    print(f"  5. DSH：实体拷贝 dsh-plugin-rea 到 profile[{args.profile}]，登记 bundle")
    print("  6. Craft Anything：调用官方 setup 注册客户端")
    print("  7. 分发 REAP / REA / Craft 技能到各 skills 目录")
    print("\n不改动：全局 JAVA_HOME、其它 MCP 服务器、已存在的备份")
    return 0


def cmd_install(args) -> int:
    agents = args.only or [k for k, m in AGENTS.items()
                           if m["kind"] != "json" or m["path"].parent.exists()]
    print(f"目标 Agent：{', '.join(agents)}\n")

    r = Report()
    if not step_node(r, apply=True):
        print(r.render())
        print(f"\n{C['bad']} node 版本不达标，终止。请先 brew upgrade node。")
        return 1
    step_rea(r, apply=True)
    step_providers(r, apply=True)
    step_craft(r, apply=False)
    print("环境")
    print(r.render())

    r2 = Report()
    step_register_json(r2, apply=True, agents=agents)
    if "codex" in agents:
        step_register_codex(r2, apply=True)
    if "reasonix" in agents:
        step_register_reasonix(r2, apply=True)
    if "dsh" in agents:
        step_dsh(r2, apply=True, profile=args.profile)
    step_craft_register(r2, apply=True, agents=agents)
    print("\n注册")
    print(r2.render())

    r3 = Report()
    step_skills(r3, apply=True)
    print("\n技能")
    print(r3.render())

    print(f"\n{C['ok']} 完成。重启各 Agent 后生效。")
    print(f"{C['dim']}验证：python3 {Path(__file__).name} verify{C['off']}")
    return 0


def brief(detail: str, limit: int = 72) -> str:
    """把 REA 的报错压成一行能看懂的。"""
    if not detail:
        return ""
    if "spawn EPERM" in detail:
        return "spawn EPERM：REA 的进程归属探测被 macOS 拦下"
    return detail if len(detail) <= limit else detail[: limit - 1] + "…"


def provider_hint(evidence: dict) -> str:
    """从 evidence 里挑出「到底用了哪个引擎」的字段，供人一眼核对。"""
    hits: list[str] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and any(t in k.lower() for t in ("provider", "engine", "backend")):
                    hits.append(f"{k}={v}")
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for x in node[:3]:
                walk(x, depth + 1)

    walk(evidence)
    return ", ".join(dict.fromkeys(hits))


def cmd_verify(args) -> int:
    r = Report()
    probe = Path("/tmp/rekit_probe_bin")
    code, _ = run(["/usr/bin/lipo", "-thin", "arm64e", "/bin/echo", "-output", str(probe)])
    r.add(code == 0, "thin slice", str(probe))

    # 先看真本事：能不能用 ghidra provider 跑出证据
    analysis_ok = False
    analysis_detail = ""
    if probe.exists():
        code, out = rea(["inspect", str(probe), "--provider", "ghidra", "--json"], timeout=420)
        analysis_ok = code == 0 and '"evidence_id"' in out
        if analysis_ok:
            try:
                ev = json.loads(out[out.find("{"):])
                hint = provider_hint(ev)
                analysis_detail = f"证据已产出（{hint}）" if hint else "证据已产出"
            except (ValueError, json.JSONDecodeError):
                analysis_detail = "证据已产出"
        else:
            m = re.search(r'"message":"([^"]{0,90})', out)
            analysis_detail = m.group(1) if m else out[:90]

    # doctor 的 java 探测走的是 REA 自己的进程归属机制（prepareOwnedCommand），
    # 和实际分析启动 Ghidra 的路径不是同一条：探测被 macOS 拦下时分析照样能跑。
    # 所以分析成功就把这一项降级为提示，别拿它当失败吓人。
    code, out = rea(["doctor", "--format", "json"], timeout=180)
    java_ok = False
    java_detail = "doctor 没吐出可解析的 JSON"
    # doctor 有失败项时退出码非 0，但 JSON 照样是完整的，不能拿 code 当门槛
    try:
        d = json.loads(out[out.find("{"):])
        for c in d.get("checks", []):
            if c.get("name") == "ghidra-java":
                java_ok = bool(c.get("ok"))
                java_detail = c.get("detail") or ""
                break
    except (ValueError, json.JSONDecodeError):
        pass
    if java_ok:
        r.add(True, "ghidra-java", "JDK 可被 REA 启动")
    elif analysis_ok:
        r.add(None, "ghidra-java", f"{brief(java_detail)}；但分析已通过，不影响使用")
    else:
        r.add(False, "ghidra-java", brief(java_detail) or "JDK 起不来")

    r.add(analysis_ok, "ghidra 分析", analysis_detail)

    ok, detail = dsh_probe(args.profile)
    r.add(ok, f"dsh 插件[{args.profile}]", detail)

    r2 = Report()
    step_skills(r2, apply=False)
    print("验证")
    print(r.render())
    print("\n技能源")
    print(r2.render())
    # warn 不算失败：探测受限但实际能力已验证的情况就该走这条
    return 0 if all(s in ("ok", "warn") for s, _, _ in r.rows) else 1


def cmd_skills(args) -> int:
    r = Report()
    n = step_skills(r, apply=True)
    print("技能")
    print(r.render())
    return 0 if n > 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="rekit", description="逆向工程链路一键部署器")
    p.add_argument("--version", action="version", version=f"rekit {__version__} (溯链)")
    p.add_argument("--profile", default="desktop", help="DSH profile 名（默认 desktop）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="体检")
    sub.add_parser("plan", help="预览改动")
    inst = sub.add_parser("install", help="一键部署")
    inst.add_argument("--only", nargs="*", metavar="AGENT", help=f"只处理这些 agent：{', '.join(AGENTS)}")
    sub.add_parser("skills", help="只分发 REAP / REA / Craft 技能")
    sub.add_parser("verify", help="端到端验证")
    sub.add_parser("agents", help="列出支持的 agent")

    cs = sub.add_parser("case", help="案件工作台：范围 → 取证 → 断言 → 判定")
    csub = cs.add_subparsers(dest="case_cmd", required=True)

    c_new = csub.add_parser("new", help="新建案件（生成 SCOPE.md 并锁定 sha256）")
    c_new.add_argument("target", help="目标路径")
    c_new.add_argument("--name", help="案件名（默认取文件名）")
    c_new.add_argument("--provider", default="ghidra", help="分析 provider（默认 ghidra）")
    c_new.add_argument("--dir", help=f"案件根目录（默认 {CASES_ROOT}）")
    c_new.add_argument("--timebox", type=int, default=8, help="时间盒小时数（默认 8）")
    c_new.add_argument("--no-thin", action="store_true", help="不自动切 universal 二进制")
    c_new.add_argument("--force", action="store_true", help="覆盖已有案件")

    c_cap = csub.add_parser("capture", help="调 rea 取证并登记 evidence_id")
    c_cap.add_argument("--case", help="案件目录（默认当前目录）")
    c_cap.add_argument("--provider", help="覆盖 provider")
    c_cap.add_argument("--label", help="这条记录的标签（默认取子命令名）")
    c_cap.add_argument("--timeout", type=int, default=600, help="超时秒数（默认 600）")
    c_cap.add_argument("extra", nargs=argparse.REMAINDER,
                       help="自定义 rea 子命令与参数（写在 -- 之后）")

    c_claim = csub.add_parser("claim", help="追加一条断言")
    c_claim.add_argument("--case", help="案件目录（默认当前目录）")
    c_claim.add_argument("--statement", required=True, help="断言内容")
    c_claim.add_argument("--kind", default="behavior", choices=CLAIM_KINDS, help="断言类型")
    c_claim.add_argument("--evidence", nargs="*", metavar="EV_ID", help="挂哪些证据")
    c_claim.add_argument("--check", help="校验命令；退出码非 0 判 contradicted")
    c_claim.add_argument("--optional", action="store_true", help="可选断言（不阻塞 ready）")
    c_claim.add_argument("--note", help="备注")
    c_claim.add_argument("--id", help="指定 id（默认 C1、C2…）")

    c_chk = csub.add_parser("check", help="逐条判定（fail-closed）")
    c_chk.add_argument("--case", help="案件目录（默认当前目录）")

    c_rep = csub.add_parser("report", help="生成 REPORT.md")
    c_rep.add_argument("--case", help="案件目录（默认当前目录）")

    c_ls = csub.add_parser("list", help="列出所有案件")
    c_ls.add_argument("--dir", help="案件根目录")

    args = p.parse_args()
    return {"doctor": cmd_doctor, "plan": cmd_plan, "install": cmd_install,
            "skills": cmd_skills, "verify": cmd_verify, "agents": cmd_agents,
            "case": cmd_case}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
