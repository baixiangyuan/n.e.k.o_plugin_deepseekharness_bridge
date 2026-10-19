# dsh_bridge

让 N.E.K.O. 的猫娘把长任务、代码类任务交给本地 **DeepSeek Harness**（`dsh`）执行，
并把结果读回对话。

## 能力

| 入口 | 用途 |
| --- | --- |
| `run_task` | 执行一次 harness 任务，返回最终回复 |
| `status` | 报告 SDK / CLI 可用性与历史运行记录 |
| `dsh_run_task` | 上面 `run_task` 的 LLM 工具版本，供模型在对话中直接调用 |

## 运行方式

插件按以下优先级选择 harness：

1. **Python SDK** — 环境里能 `import deepseek_harness` 时使用，通过
   `DeepSeekHarness` 上下文管理器运行，子进程由 SDK 管理。
2. **`dsh` CLI** — SDK 不可用但 PATH 或配置里能找到 `dsh` 时，以一次性子进程调用。
3. **都不可用** — `status` 会明确报告，`run_task` 返回可读错误，不会静默失败。

## 配置

配置写在运行配置的 `[dsh_bridge]` 段（见 `plugin.toml` 的默认值）：

| 键 | 说明 |
| --- | --- |
| `repo_path` | DeepSeek Harness 源码检出路径；SDK 模式下作为隔离 home 的根 |
| `dsh_executable` | 显式指定 `dsh` 可执行文件 |
| `workspace` | harness 的工作目录，默认为插件存储目录下的 `workspace/` |
| `profile` | 传给 `dsh` 的 profile，默认 `sdk-minimal` |
| `model` | 每次运行使用的模型 ID |
| `task_timeout` | 单次任务超时（秒），默认 600 |
| `log_dir` | 运行日志目录，默认为 `storage_dir/runs` |

## 日志

每次运行追加一行 JSON 到 `<log_dir>/runs.jsonl`，只记录会话 ID、任务长度、
耗时和调用模式，不记录任务正文。

## 已知限制

- 会话延续依赖 DSH 自身的 session ID；本插件只透传，不维护额外状态。
- CLI 回退路径依赖 `dsh run --prompt-file` 等参数，不同 DSH 版本可能不同，
  若你的版本不一致，请改用 SDK 路径。
