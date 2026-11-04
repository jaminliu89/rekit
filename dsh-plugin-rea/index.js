/**
 * dsh-plugin-rea — 把 REA 与 Craft Anything 两个 MCP 服务器挂进 DeepSeek Harness。
 *
 * 设计要点：
 * - 不在 patch 里写死绝对路径（GUI 启动的 PATH 很短，裸命令名也解析不到），
 *   路径在 apply() 里按 HOME / 环境变量 / 常见安装位置计算。
 * - 依赖 @deepseek-ai/dsh-mcp-client（profile 的 node_modules 里已有），
 *   本包只做「组装」，不重复实现 MCP 协议。
 * - 可执行文件不存在就不挂，避免 DSH 启动时对着不存在的命令反复 spawn 失败。
 */
import { homedir } from 'node:os'
import { existsSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

/** cordis 诊断用的插件名 */
export const name = 'rea-suite'

/** 不依赖其它服务；mcp-client 自己会 inject ['tools'] */
export const inject = []

const HOME = homedir()

/**
 * 取第一个存在的候选路径，都不存在就退回「最后一个非空候选」作为兜底。
 *
 * 注意不能写成 `... || candidates[0]`：调用方习惯把 config 值放在第一位，
 * 而 config 通常是 undefined，兜底就会退成 undefined，等于把路径整个弄丢。
 */
const firstExisting = (...candidates) => {
  const list = candidates.filter(Boolean)
  return list.find((p) => existsSync(p)) ?? list[list.length - 1]
}

/**
 * 解析 @deepseek-ai/dsh-mcp-client。
 *
 * 正常情况一句 import 就够，但 profile 的 node_modules 布局随安装方式而变
 * （pnpm 软链 / DSH 自己的 symlink farm / 纯 npx 缓存）。解析不到时若直接抛
 * ERR_MODULE_NOT_FOUND，用户只能看到一个和本插件毫无关系的裸模块错误，
 * 所以这里做多路回退，最后再给一句能照做的提示。
 */
async function loadMcpClient() {
  try {
    return await import('@deepseek-ai/dsh-mcp-client')
  } catch {}

  const roots = [
    join(HOME, '.dsh/profiles/node_modules'),
    join(HOME, '.dsh/node_modules'),
    '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/node_modules',
    join(HOME, 'Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/node_modules'),
  ]
  const entries = ['lib/index.js', 'index.js', 'dist/index.js']
  for (const root of roots) {
    for (const entry of entries) {
      const f = join(root, '@deepseek-ai/dsh-mcp-client', entry)
      if (existsSync(f)) {
        try {
          return await import(pathToFileURL(f).href)
        } catch {}
      }
    }
  }

  // 兜底：npx 缓存里 DSH 自己用的那一份
  const npx = join(HOME, '.npm/_npx')
  if (existsSync(npx)) {
    for (const d of readdirSync(npx)) {
      const f = join(npx, d, 'node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js')
      if (existsSync(f)) {
        try {
          return await import(pathToFileURL(f).href)
        } catch {}
      }
    }
  }

  throw new Error(
    '[dsh-plugin-rea] 找不到 @deepseek-ai/dsh-mcp-client。'
      + '请在 DSH 里重装本插件（dsh plugin add dsh-plugin-rea），或先运行 rekit install。',
  )
}

const McpClient = await loadMcpClient()

/** REA 可执行文件：环境变量 > npm 全局 > Homebrew 前缀 */
function reaBin(config) {
  return firstExisting(
    config.reaBin,
    process.env.REKIT_REA_BIN,
    join(HOME, '.npm-global/bin/rea'),
    '/opt/homebrew/bin/rea',
    '/usr/local/bin/rea',
  )
}

/** Craft Anything CLI：环境变量 > 应用包内 */
function craftBin(config) {
  return firstExisting(
    config.craftBin,
    process.env.REKIT_CRAFT_BIN,
    '/Applications/Craft Anything.app/Contents/MacOS/craft-anything-cli',
    join(HOME, 'Applications/Craft Anything.app/Contents/MacOS/craft-anything-cli'),
  )
}

/** Ghidra 安装目录：配置 > 环境变量 > ~/tools 下最新的 ghidra_* > 常见默认位置 */
function ghidraDir(config) {
  const explicit = firstExisting(config.ghidraInstallDir, process.env.GHIDRA_INSTALL_DIR)
  if (explicit) return explicit
  // 版本号不该写死在插件里，扫一遍 ~/tools 取字典序最大的那个
  try {
    const base = join(HOME, 'tools')
    const hits = readdirSync(base).filter((d) => d.startsWith('ghidra_')).sort()
    if (hits.length > 0) return join(base, hits[hits.length - 1])
  } catch {}
  return join(HOME, 'tools/ghidra_12.1.4_PUBLIC')
}

function javaHome(config) {
  return firstExisting(
    config.javaHome,
    process.env.JAVA_HOME,
    '/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home',
    '/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home',
  )
}

function hopperLauncher(config) {
  return firstExisting(
    config.hopperLauncher,
    process.env.HOPPER_LAUNCHER_PATH,
    '/Applications/Hopper Disassembler.app/Contents/MacOS/hopper',
    join(HOME, 'Applications/Hopper Disassembler.app/Contents/MacOS/hopper'),
  )
}

/** 尽力写一条日志；cordis 的 logger 在不同版本里形态不一，失败就算了 */
function warn(ctx, msg) {
  try {
    ctx.logger?.('rea-suite')?.warn?.(msg)
  } catch {}
}

export function apply(ctx, config = {}) {
  const enabled = config.enable ?? ['rea', 'craft-anything']

  if (enabled.includes('rea')) {
    const bin = reaBin(config)
    if (existsSync(bin)) {
      ctx.plugin(McpClient, {
        transport: 'stdio',
        serverName: 'rea',
        command: bin,
        args: ['mcp'],
        env: {
          GHIDRA_INSTALL_DIR: ghidraDir(config),
          JAVA_HOME: javaHome(config),
          HOPPER_LAUNCHER_PATH: hopperLauncher(config),
          // 跳过 Hopper 的 Apple Events 授权，直接用 headless 的 Ghidra
          REA_ANALYSIS_PROVIDER: config.analysisProvider ?? 'ghidra',
          ...(config.env ?? {}),
        },
        toolCallTimeoutMs: config.toolCallTimeoutMs ?? 180000,
      })
    } else {
      warn(ctx, `跳过 rea：找不到可执行文件 ${bin}`)
    }
  }

  if (enabled.includes('craft-anything')) {
    const bin = craftBin(config)
    if (existsSync(bin)) {
      ctx.plugin(McpClient, {
        transport: 'stdio',
        serverName: 'craft-anything',
        command: bin,
        args: ['mcp'],
        toolCallTimeoutMs: config.toolCallTimeoutMs ?? 180000,
      })
    } else {
      warn(ctx, `跳过 craft-anything：找不到可执行文件 ${bin}`)
    }
  }
}
