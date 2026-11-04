# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-11-05

从「装好」到「能跑出可判定的结论」。0.1.0 解决的是链路铺不铺得开，这个版本解决流程跑不跑得动。

### 新增

- **`rekit case` 案件工作台**：把 S0–S5 落成可执行的五个动作
  - `case new`：建案件目录，锁定目标 `sha256`，生成 `SCOPE.md`（授权、允许/禁止动作、时间盒）；
    universal Mach-O 自动 `lipo -thin`（优先 `arm64e`，Ghidra 不收通用包）
  - `case capture`：调 `rea` 取证，递归收集所有 `evidence_id` 登记进 `case.json`，
    原始 JSON 落 `evidence/NN-*.json`；支持 `--` 后透传任意 rea 子命令
  - `case claim`：追加可判定断言，可挂证据、可挂校验命令；证据未登记时直接拒绝
  - `case check`：逐条判定，写 `verdict.json` 并重生成 `SPEC.md`
  - `case report`：出 `REPORT.md`（范围、取证记录、断言表、残留 UNKNOWN）
  - `case list`：列出所有案件与判定状态
- **fail-closed 判定**：`verified` / `characterized` / `unknown` / `dangling` / `contradicted`，
  整体 `ready` / `open` / `failed` / `unknown`，退出码 `0` / `1` / `2` / `1`，可直接接 CI
- **`scripts/selftest_case.py`**：状态机自测，覆盖五种判定状态与整体状态流转，CI 每次跑
- `SPEC.md` 定为生成物：`case.json` 是唯一输入，避免「解析 markdown 表格」和双份维护

### 修复

- `case capture` 的 `--` 分隔符被当成 rea 子命令传给 rea（`REMAINDER` 会把 `--` 一起吞下）
- rea 的第二类失败信封 `{"code": "COMMAND_NOT_FOUND", "message": …}` 没有 `error` 字段，
  只看 `error` 会把它当成功；现在两类都识别，且没有产出 `evidence_id` 一律判失败
- `file(1)` 对 universal 二进制会吐整段架构清单，格式描述压成 `Mach-O universal (x86_64, arm64e)`

### 说明

- 版本号跟随仓库统一递增：插件本体这版没有改动，只是与部署器保持同版本。
- `rekit case` 与 REA 的 `build-reconstruction-obligation-ledger` 是分层关系：
  前者用于日常迭代判断「现在算不算做完了」，后者输入完整 evidence bundle 与 reviewed
  obligations，产出带闭包摘要的交付级台账。

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

[0.2.0]: https://github.com/jaminliu89/rekit/releases/tag/v0.2.0
[0.1.0]: https://github.com/jaminliu89/rekit/releases/tag/v0.1.0
