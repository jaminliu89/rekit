# 溯链 · rekit

[![CI](https://github.com/jaminliu89/rekit/actions/workflows/ci.yml/badge.svg)](https://github.com/jaminliu89/rekit/actions/workflows/ci.yml)

> 一条命令，把逆向工程的证据链路装进你所有的 AI Agent。

**溯**是追溯——REA 的核心不是工具集合，而是 `evidence_id` 串起来的证据链；
**链**是链条——从二进制到可判定结论的那条流水线，也是把它铺到 9 个客户端的部署链。

```
二进制 → REA（证据）→ SPEC.md（可判定断言）→ 重建 → verify_reconstruction → 不行就再来
```

---

## 这个仓库装什么

| 组件 | 是什么 | 在本仓库 |
|---|---|---|
| **REA** | 逆向证据引擎。MCP + CLI 双入口，四个 native provider（Ghidra / Hopper / IDA / Kuna），核心是证据契约：`evidence_id`、`record_unknown`、`verify_reconstruction`、`export bundle` | 由 rekit 通过 npm 安装并注册 |
| **Craft Anything** | 净室重建闭环。320 个工具，Study → Build → Check → Ready，clean-room 模式在工具层就拒绝读目标内部 | 由 rekit 调用官方 `setup` 注册 |
| **REAP** | 22 个逆向调研技能包，从固件、内核、Android 到 Electron、反作弊、混淆器 | 由 rekit 分发到各 skills 目录 |
| **rekit** | 上面三样的一键部署器 | `rekit.py` |
| **dsh-plugin-rea** | DeepSeek Harness 的 cordis 插件，一行 patch 挂两个 MCP server | `dsh-plugin-rea/` |

---

## 快速开始

```bash
git clone https://github.com/jaminliu89/rekit.git
cd rekit

python3 rekit.py doctor      # 体检：环境 + 每个 Agent 的注册状态
python3 rekit.py plan        # 只看会改什么，不动文件
python3 rekit.py install     # 真装
python3 rekit.py verify      # 端到端验证：能出证据吗、插件能加载吗
```

想当命令用：

```bash
chmod +x rekit.py
ln -sf "$PWD/rekit.py" ~/.local/bin/rekit
```

只处理其中几个 Agent：

```bash
rekit install --only dsh codex
rekit --profile desktop install --only dsh   # DSH profile 名，默认 desktop
```

### 前置条件

- macOS（`prepareOwnedCommand` 的进程归属机制、`lipo` 切片的判定都是 macOS 专用；Linux 上大部分逻辑可用但没实测）
- Node 满足 REA 的 `engines`：`^22.23.4 || ^24.22.1 || >=26.12.1`
- Ghidra 12.x + JDK 21（`brew install --cask ghidra` / `brew install openjdk@21`）
- 可选：Hopper、jadx、apktool、Craft Anything

---

## 支持的 Agent

| ID | 名称 | 配置形态 | 写入位置 |
|---|---|---|---|
| `claude_code` | Claude Code | JSON `mcpServers` | `~/.claude.json` |
| `cursor` | Cursor | JSON `mcpServers` | `~/.cursor/mcp.json` |
| `workbuddy` | WorkBuddy | JSON `mcpServers` | `~/.workbuddy/mcp.json` |
| `kimi_code` | Kimi Code | JSON `mcpServers` | `~/.kimi-code/mcp.json` |
| `qwen_code` | Qwen Code | JSON `mcpServers` | `~/.qwen/settings.json` |
| `zcode` | ZCode | JSON `mcp.servers` | `~/.zcode/cli/config.json` |
| `codex` | Codex | TOML `[mcp_servers.rea]` | `~/.codex/config.toml` |
| `dsh` | DeepSeek Harness | cordis bundle 插件 | `~/.dsh/profiles/<profile>/` |
| `reasonix` | reasonix | TOML `[[plugins]]` | `~/.reasonix/config.toml` |

三种配置格式（JSON / TOML / YAML patch）各不相同，rekit 把它们收在一个 `AGENTS` 注册表里，
加新 Agent 只要加一行。

---

## 命令

| 命令 | 做什么 |
|---|---|
| `rekit doctor` | 体检：环境 8 项 + 每个 Agent 的注册状态 + 技能落盘数 + `rea doctor` 分组诊断 |
| `rekit plan` | 只打印会改什么，不动文件 |
| `rekit install` | 真装（`--only <agent>...` 限定范围） |
| `rekit skills` | 只重发技能，改完技能包后用 |
| `rekit verify` | 端到端：切一片 Mach-O、真跑一次 Ghidra 分析、真加载一次 DSH 插件 |
| `rekit agents` | 列出支持的 Agent 和各自状态 |

## 三个刻意的设计

**不改全局 `JAVA_HOME`。**
Ghidra 12 要 JDK 21，很多项目要 17。所以只在启动 `rea` 时注入，不污染 shell。

**注册时锁 `REA_ANALYSIS_PROVIDER=ghidra`。**
默认 `auto` 会先试 Hopper，而 Hopper 要 macOS「自动化」授权；没授权时 REA 直接报
`Not authorized to send Apple events`，整个 provider 就废了。锁成 `ghidra` 绕开这条授权链
（Ghidra 走 headless，本来就不需要）。要用 Hopper 时加 `--provider hopper`。

**能交给官方安装器就交给官方。**
Craft 的客户端注册走 `craft-anything-cli setup --client ...`，技能也由它自己装。
rekit 只补它不认识的 Agent，不重复实现。

### 实现上的几个坑（都踩过）

- **幂等 + 备份**：任何被写过的文件旁边留一份 `*.rekit-backup`（只留一次，不覆盖）；
  重复执行不产生变化。
- **技能先快照再分发**：Craft 的官方技能源恰好是 `~/.claude/skills/craft-anything`，
  而 `~/.claude/skills` 本身也是分发目标。直接 `rmtree` + `copytree` 会先删掉源目录
  再从不存在的路径复制——所以先快照到 `~/.cache/rekit/skills/` 再分发。
- **DSH 用实体拷贝而不是 pnpm 软链**：pnpm 会把 `node_modules/dsh-plugin-rea`
  建成指向源码目录的软链，Node 按真实路径解析就找不到 profile 里的依赖。
- **doctor 的 JSON 不能看退出码**：`rea doctor` 只要有失败项就返回非 0，
  但 JSON 照样是完整的。用 `if code == 0` 当门槛会导致永远解析不到。

---

## verify 怎么读

```
验证
  ✓ thin slice        /tmp/rekit_probe_bin
  ! ghidra-java       spawn EPERM：REA 的进程归属探测被 macOS 拦下；但分析已通过，不影响使用
  ✓ ghidra 分析        证据已产出（requested_provider=ghidra）
  ✓ dsh 插件[desktop]   rea-suite → rea, craft-anything
```

`ghidra 分析` 才是真本事：它用 `rea inspect --provider ghidra` 实际跑一遍，
检查有没有 `evidence_id`，成功时还把 evidence 里 `*provider*` / `*engine*` 字段打出来。

`ghidra-java` 那行要单独解释。REA 的 doctor 用一条**独立探测**去 spawn
`$JAVA_HOME/bin/java`，走的是它自己的 macOS 进程归属机制（`prepareOwnedCommand`
会编译一个 Swift 归属助手来托管子进程）；而实际分析启动 Ghidra 走的是另一条路径。
两条路径权限要求不同，于是会出现「探测被拦、分析照跑」。

判定规则：**分析成功就把 java 探测降级为 `!`（提示），不算失败**；分析失败才算 ✗。
退出码只认 ✗。

另外 Ghidra provider 对 **universal** Mach-O 会报 `architecture_unsupported`，
先 `lipo -thin arm64e`（系统二进制是 `arm64e`，不是 `arm64`）。

---

## 已知缺口

`rekit doctor` 会把 `rea doctor` 的未通过项分成两类，`!` 是已知项：

| 项 | 说明 |
|---|---|
| `ida-registration` | 没装 IDA Pro |
| `kuna-installation` | 没装 Kuna |
| `jadx` | 要 `jadx-headless-mcp` 的 JAR（`REA_JADX_MCP_JAR`），不是 Homebrew 的 `jadx` CLI |
| `registration:commandcode` / `devin` | 客户端没装，REA 仍报 drift，属幻影告警 |
| `registration:dsh` | **设计如此**：DSH 由 bundle 插件挂载，`cordis.patch.yml` 里放回 `rea` 会和插件的 `serverName` 撞车 |

---

## dsh-plugin-rea

见 [`dsh-plugin-rea/README.md`](dsh-plugin-rea/README.md)。要点：

- 一行 patch 挂 `rea` 与 `craft-anything` 两个 MCP server，路径全部运行时计算
- `@deepseek-ai/dsh-mcp-client` 多路回退解析（DSH 的 profile 布局随安装方式而变）
- 可执行文件不存在就跳过并 warn，不对着空命令反复 spawn
- 工具调用超时 180s——Ghidra 分析一个中等二进制经常要一两分钟

---

## License

MIT
