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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()

# 与 dsh-plugin-rea/package.json 的 version 保持一致（CI 会校验）
__version__ = "0.1.0"

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
    args = p.parse_args()
    return {"doctor": cmd_doctor, "plan": cmd_plan, "install": cmd_install,
            "skills": cmd_skills, "verify": cmd_verify, "agents": cmd_agents}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
