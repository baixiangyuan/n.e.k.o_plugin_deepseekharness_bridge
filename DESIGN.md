# DESIGN — deepseekharness_bridge

## 身份锁定

| 表面 | 值 |
| --- | --- |
| 目录名 | `plugin/plugins/deepseekharness_bridge/` |
| `[plugin].id` | `deepseekharness_bridge` |
| `[plugin].name` | DeepSeek Harness 桥接 |
| `[plugin].entry` | `plugin.plugins.deepseekharness_bridge:DeepseekharnessBridgePlugin` |
| 主类 | `DeepseekharnessBridgePlugin` |

包类型 `plugin`（独立功能），不是 `extension`（无宿主），也不是 `adapter`
（不桥接外部协议进来，而是主动调用本地 harness）。

## 要解决什么

N.E.K.O. 的猫娘擅长对话和短任务，但遇到「读这个仓库、跑测试、改这几处代码」
这类长任务时，缺一个真正能干活的 agent 执行体。DeepSeek Harness (dsh) 正好
是干这个的。本插件把 harness 变成猫娘可以驱动的一个能力。

## 明确不做的事

- **不做 N.E.K.O. 侧的 adapter。** 不把外部协议桥进插件系统。
- **不管会话管理。** session ID 只透传给 harness，不额外维护状态。
- **不碰平台代码。** 全部逻辑在插件目录内。

## 运行方式（三级回退）

优先级从高到低，任何一级不可用都明确报错，不静默降级：

1. **`dsh` CLI** — 账号路由（默认）下优先。只有 CLI 的 patch 覆盖层能重新打开
   headless 关掉的账号栈，也只有它应用沙箱模式和思考强度。
2. **Python SDK** — `import deepseek_harness` 成功、且不需要账号路由时用
   `DeepSeekHarness` 上下文管理器；子进程由 SDK 托管。SDK 路径固定用 API-key
   provider，且不应用沙箱/思考强度。
3. **都不可用** — `status` 报告缺什么，`run_task` 返回可读错误。

选择在 `startup` 时探测一次并存住，避免第一次调用工具时才发现装错了。

## 入口与 LLM 工具

| 名称 | 种类 | 用途 |
| --- | --- | --- |
| `run_task` | `plugin_entry` | 执行一次 harness 任务 |
| `status` | `plugin_entry` | 自检：SDK/CLI 可用性、历史运行数 |
| `dsh_run_task` | `llm_tool` | 上面那个的模型可调用版本 |

`@llm_tool` 是推荐路径：注册、IPC 路由、停机清理都由 SDK 处理，不需要手写
`register_llm_tool` 的样板代码。

## 安全考量

- **任务正文会进 argv。** headless CLI 只接受位置参数，没有 `--prompt-file`，
  所以任务内容会出现在进程列表里（`ps` / 任务管理器可见）。这是本地单用户
  场景下的已知取舍；`.cmd`/`.bat` 启动器会经 cmd.exe 二次解析，任务参数会被
  引号包裹并转义 `%`、`"`。
- **面板显示正文，磁盘日志脱敏。** 面板在内存里展示每次运行的任务正文和模型
  回复；`runs.jsonl` 只记会话 ID、任务长度、耗时、调用模式，不落正文和回复。
- **凭据不落在代码里。** API key 交给 DSH 自己的凭据管理，插件不读不写。
- **路径可预期。** workspace 和日志目录默认落在插件自己的 `storage_dir`
  下，不写用户目录根部。

## 已知限制

- CLI 路径按 headless profile 当前形状调用：任务作为位置参数、只认
  `--session-id` / `--json`，没有 `run` 子命令也没有 `--prompt-file`。DSH 处于
  developer preview，官方明确说会有破坏性变更，参数可能变。
- SDK 路径下 `repo_path` 被当作隔离 home 的根使用，要求该目录可写。
- 单次任务默认超时 600 秒，长任务需要在配置里调大。

## 验证

`tests/test_smoke.py` 只测纯逻辑（manifest 契约、目录名一致性、async 入口、
无密钥泄漏），不依赖宿主和真实 harness，可在 CI 里跑。
真实联调需要：N.E.K.O. 运行中 + DSH 已安装，见仓库根的 README。
