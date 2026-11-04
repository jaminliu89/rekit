# dsh-plugin-rea

DeepSeek Harness 的 cordis 插件：把 **REA** 和 **Craft Anything** 两个 MCP 服务器
一次性挂进 DSH，不写死任何机器相关的绝对路径。

## 装

```bash
dsh plugin --profile desktop add "$PWD"     # 在本目录执行
```

或者直接用 rekit（它会做实体拷贝并登记 bundle，绕开 pnpm 软链的解析问题）：

```bash
rekit install --only dsh
```

装完重启 DSH 生效。

## 它挂什么

| server | 命令 | 用途 |
|---|---|---|
| `rea` | `~/.npm-global/bin/rea mcp` | 逆向证据引擎：Ghidra / Hopper / IDA / Kuna provider |
| `craft-anything` | `Craft Anything.app/.../craft-anything-cli mcp` | 净室重建闭环：Study → Build → Check → Ready |

两个都带 `toolCallTimeoutMs: 180000`——Ghidra 分析一个中等二进制经常要一两分钟，
默认超时会中途掐断，留下半截证据。

`rea` 这一条会注入四个环境变量：

```
GHIDRA_INSTALL_DIR    ~/tools/ghidra_12.1.4_PUBLIC
JAVA_HOME             /opt/homebrew/opt/openjdk@21/...
HOPPER_LAUNCHER_PATH  /Applications/Hopper Disassembler.app/Contents/MacOS/hopper
REA_ANALYSIS_PROVIDER ghidra
```

最后一条默认锁 `ghidra`：`auto` 会先试 Hopper，而 Hopper 要 macOS「自动化」授权，
没授权时 REA 直接报 `Not authorized to send Apple events` 然后整个 provider 不可用。
Ghidra 走 headless，不需要这条授权链。

## 配置

`cordis.patch.yml` 里 `config: {}` 是空的，需要时按下面这些键覆盖（全部可选）：

| 键 | 默认 | 说明 |
|---|---|---|
| `reaBin` | 自动探测 | REA 可执行文件 |
| `craftBin` | 自动探测 | Craft Anything CLI |
| `ghidraInstallDir` | `~/tools/ghidra_12.1.4_PUBLIC` | Ghidra 根目录 |
| `javaHome` | Homebrew openjdk@21 | JDK 21 |
| `hopperLauncher` | `/Applications/.../hopper` | Hopper 可执行文件 |
| `analysisProvider` | `ghidra` | 改 `hopper` 前先给授权 |
| `enable` | `["rea", "craft-anything"]` | 只挂其中一个 |
| `toolCallTimeoutMs` | `180000` | 工具调用超时 |
| `env` | `{}` | 追加/覆盖注入的环境变量 |

例：只挂 REA，并把超时放到 10 分钟：

```yaml
- insert:
    - id: rea-suite
      name: dsh-plugin-rea
      config:
        enable: ["rea"]
        toolCallTimeoutMs: 600000
```

## 两个容易踩的坑

**依赖解析**。插件 `import '@deepseek-ai/dsh-mcp-client'`。DSH 的 profile 布局随安装方式而变
（pnpm 软链 / DSH 自己的 symlink farm / 纯 npx 缓存），一旦解析不到，报出来的是个和本插件
毫无关系的 `ERR_MODULE_NOT_FOUND`。所以 `index.js` 里做了多路回退：
先正常 import，失败就依次找 `~/.dsh/profiles/node_modules`、`~/.dsh/node_modules`、
应用包内 runtime、最后扫 `~/.npm/_npx/*`。全都找不到才抛一句能照做的提示。

**可执行文件不存在就不挂**。以前的做法是无论 `rea` 在不在都注册，DSH 启动后会对着
不存在的命令反复 spawn 失败，日志里全是噪声。现在先 `existsSync` 判断，缺了就跳过并 warn。

## 验证

不用开 DSH 也能验证插件本身：

```bash
node --input-type=module -e '
const mod = await import(process.argv[1])
const got = []
mod.apply({ plugin: (p, c) => got.push([p.name, c.serverName, c.command]) }, {})
console.log(mod.name, got)
' ~/.dsh/profiles/desktop/node_modules/dsh-plugin-rea/index.js
```

应输出 `rea-suite [ [ 'mcp-client', 'rea', ... ], [ 'mcp-client', 'craft-anything', ... ] ]`。

`rekit verify` 内置了同样的探针，输出一行 `✓ dsh 插件[desktop] rea-suite → rea, craft-anything`。
