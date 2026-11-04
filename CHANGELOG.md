# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-04

首个版本。

### 新增

- **rekit**：一条命令把 REA / Craft Anything / REAP 部署到 9 个 Agent
  - 六个命令：`doctor` / `plan` / `install` / `skills` / `verify` / `agents`
  - 三种配置形态（JSON `mcpServers`、TOML `[mcp_servers.x]`、cordis patch）收在一个注册表里
  - node 版本按 REA 的 `engines` 精确判定（`^22.23.4 || ^24.22.1 || >=26.12.1`，不是简单 `>=22`）
  - 注册时注入 `GHIDRA_INSTALL_DIR` / `JAVA_HOME` / `HOPPER_LAUNCHER_PATH` / `REA_ANALYSIS_PROVIDER`，不改全局 `JAVA_HOME`
  - 幂等；改动前留 `*.rekit-backup`，只留一次不覆盖
- **dsh-plugin-rea**：DeepSeek Harness 的 cordis 插件，一行 patch 挂 `rea` 与 `craft-anything` 两个 MCP server
  - 路径全部运行时计算，不写死机器相关绝对路径
  - `@deepseek-ai/dsh-mcp-client` 多路回退解析，适配 pnpm 软链 / symlink farm / npx 缓存
  - 可执行文件不存在就跳过并 warn，不对着空命令反复 spawn
  - 工具调用超时 180s（Ghidra 分析中等二进制经常要一两分钟）
- GitHub Actions CI：Python 3.10 / 3.13 语法与命令可用性、ESM 语法、`dsh.bundle` 契约、导出完整性、无硬编码本机路径、版本一致性

### 修复

- 技能分发时源目录恰好也是分发目标，`rmtree` 先删源再 `copytree`，崩溃并弄丢 `reverse-engineer-anything`
- 技能快照目录用了短名，落盘成 `rea/` 而不是 `reverse-engineer-anything/`
- `rea doctor` 有失败项时退出码非 0，JSON 被 `if code == 0` 挡住，`ghidra-java` 永远打印硬编码文案
- 插件 `firstExisting` 的兜底写成 `candidates[0]`，而首位通常是 `undefined`，GUI 短环境下会丢路径
- Ghidra 路径写死 `ghidra_12.1.4_PUBLIC`，改为扫 `~/tools/ghidra_*` 取最新

### 说明

- `ghidra-java` 在部分机器上探测报 `spawn EPERM`：REA 的 doctor 走自己的 macOS 进程归属机制（`prepareOwnedCommand` + Swift 归属助手），和实际启动 Ghidra 不是同一条路径。探测被拦不影响分析，`verify` 已据此把这一项降级为提示而非失败。
- Ghidra provider 对 universal Mach-O 会报 `architecture_unsupported`，需先 `lipo -thin arm64e`（系统二进制是 `arm64e`，不是 `arm64`）。

[0.1.0]: https://github.com/jaminliu89/rekit/releases/tag/v0.1.0
