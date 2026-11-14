# deepseekharness_bridge

让 N.E.K.O. 的猫娘把长任务、代码类任务交给本地 **DeepSeek Harness**（`dsh`）执行，
并把结果读回对话。

## 能力

| 入口 | 用途 |
| --- | --- |
| `run_task` | 执行一次 harness 任务，返回最终回复 |
| `status` | 报告 SDK / CLI 可用性与历史运行记录 |
| `dsh_run_task` | 上面 `run_task` 的 LLM 工具版本，供模型在对话中直接调用 |
| `panel_refresh` / `panel_run_task` | 面板专用入口，由 `@ui.action` 暴露 |

## 面板

插件带一个 Hosted TSX 面板（`ui/panel.tsx`），在插件管理器里直接打开，包含：

- **状态总览** — harness 是否就绪、累计运行次数、当前调用方式
- **Harness 详情** — Python SDK 可用性、`dsh` 可执行文件、模型、profile、超时、工作目录
- **运行任务** — 一个表单，直接把任务交给 harness，显示提交结果
- **最近运行** — 最近 20 次运行的会话 ID、任务长度、耗时、调用方式、成败

面板会显示每次运行的任务正文和模型回复，方便直接查看；这些内容只留在内存里，
磁盘上的 `runs.jsonl` 仍然只记元数据（会话 ID、长度、耗时、模式）。

## 运行方式

插件按以下优先级选择 harness：

1. **`dsh` CLI** — 账号路由（默认）下优先走这条：只有 CLI 的 patch 覆盖层能
   重新打开被 headless 关掉的账号栈，也只有它应用沙箱模式和思考强度。
2. **Python SDK** — 仅在 `provider` 不是账号路由、或根本没有 CLI 时使用。
   注意 SDK 路径固定用 API-key provider，且**不应用** `sandbox_mode` /
   `reasoning_effort`。
3. **都不可用** — `status` 会明确报告，`run_task` 返回可读错误，不会静默失败。

想让账号登录、沙箱、思考强度都生效，就用 CLI 路径。

## 配置

配置写在运行配置的 `[deepseekharness_bridge]` 段（见 `plugin.toml` 的默认值）：

| 键 | 说明 |
| --- | --- |
| `repo_path` | DeepSeek Harness 源码检出路径；SDK 模式下作为隔离 home 的根 |
| `dsh_executable` | 显式指定 `dsh` 可执行文件 |
| `dsh_install_path` | DSH 桌面版安装目录，非标准位置时用它，省去自动扫描 |
| `workspace` | harness 的工作目录，默认为插件存储目录下的 `workspace/` |
| `profile` | 传给 `dsh` 的 profile，默认 `headless`（桌面端装法唯一可用的选项） |
| `model` | 模型 ID，默认 `deepseek-flash`；面板上有下拉可切 |
| `task_timeout` | 单次任务超时（秒），默认 600 |
| `log_dir` | 运行日志目录，默认为 `storage_dir/runs` |

## 模型

面板的「模型」卡片列出当前 DSH 支持的模型，选中即用，之后的任务立即生效。

| ID | 名称 | 说明 |
| --- | --- | --- |
| `deepseek-flash` | DeepSeek-V4.1-Flash | 默认，快且支持图像输入 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | 更强编码与推理，成本更高 |

这份目录来自 DSH `0.2.1-rc.2` 内置的 `DEFAULT_MODELS`，同时作为面板下拉的
数据源。账号路由启用后，DSH 还会在线拉一份 `/models` 列表与它合并，所以
新模型通常不用改代码就能出现。

## 凭据

默认走 **DeepSeek 账号**，直接复用你已经在 DSH 里完成的登录，不需要 API key。

headless profile 原本把 `credentials` / `authorization` / `deepseek-account`
三项都设成 `disabled: true`，所以插件每次运行会生成一个 patch 覆盖层把它们
重新打开。这是 cordis 后层覆盖前层的正常用法，不改 DSH 自身，也不动你的登录
状态。

想改用 API key 的话，把配置里的 `provider` 设成 `deepseek-official`，
并提供 `DEEPSEEK_API_KEY`。

## 启动时会改什么

为了让反向桥接开箱即用，插件启动时会检查 DSH 桌面版的 profile，并在缺失或
不完整时把内嵌的 `dsh-plugin-neko` 装进去。这**会写入 `~/.dsh` 下的
profile 配置和 `package.json`**（只增补 `dsh-plugin-neko` 这一项，不动其它条目），
面板上的「DSH 侧桥接」会显示结果：`已就绪` / `已自动修复` / `已自动安装`。

不想要这个副作用时，把反向 bundle 从 profile 里移除即可，N.E.K.O. → DSH
的方向不受影响。

## 日志

每次运行追加一行 JSON 到 `<log_dir>/runs.jsonl`，只记录会话 ID、任务长度、
耗时和调用模式，不记录任务正文。

## 已知限制

- 账号路由下，模型列表依赖 DSH 能连上 `api.deepseek.com`；离线时回落到内置目录。
- 会话延续依赖 DSH 自身的 session ID；本插件只透传，不维护额外状态。
- CLI 路径按 headless profile 的当前形状调用：任务作为位置参数、只认
  `--session-id` / `--json`，没有 `run` 子命令也没有 `--prompt-file`。DSH 处于
  快速迭代期，若你的版本参数不同，请改用 Python SDK 路径（但 SDK 路径不应用
  账号、沙箱和思考强度）。
- 单文件约 1170 行，和 DSH 内部结构耦合较深（patch 里写死了 `credentials`、
  `permission-presets` 等行），DSH 大版本升级后可能需要同步调整。
