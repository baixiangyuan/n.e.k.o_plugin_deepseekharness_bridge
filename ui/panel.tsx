import {
  ActionButton,
  ActionForm,
  Alert,
  Card,
  DataTable,
  Grid,
  KeyValue,
  Page,
  Section,
  Stack,
  StatCard,
  StatusBadge,
  Text,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

/**
 * deepseekharness_bridge 面板。
 *
 * 三段：harness 可用性、配置摘要、最近运行记录，加一个任务执行表单。
 * 业务逻辑都在 Python 的 @ui.context / @ui.action 里，这里只做呈现和本地
 * UI 状态。
 *
 * 文案直接用中文作 i18n key：`t` 的签名是 (key, params)，没有 defaultValue，
 * 取不到翻译时回退显示 key 本身，所以 key 写成可读文案就是可用的默认值。
 */

type HarnessState = {
  sdk_available?: boolean
  dsh_executable?: string | null
  ready?: boolean
}

type Settings = {
  model?: string
  profile?: string
  task_timeout?: number
  workspace?: string
  repo_path?: string
}

type RunRecord = {
  session_id: string
  task_chars: number
  elapsed_seconds: number
  mode: string
  ok: boolean
}

type State = {
  harness?: HarnessState
  settings?: Settings
  runs?: RunRecord[]
  runs_total?: number
  dsh_bridge?: string
}

function findAction(actions: HostedAction[], id: string): HostedAction | undefined {
  return actions.find((action) => action.id === id || action.entry_id === id)
}

export default function Panel(props: PluginSurfaceProps<State>) {
  const { actions, state, t } = props

  const harness = state.harness || {}
  const settings = state.settings || {}
  const runs = state.runs || []
  const bridge = state.dsh_bridge || "unknown"

  const refresh = findAction(actions, "panel_refresh")
  const runTask = findAction(actions, "panel_run_task")

  // The DSH-side bundle is bootstrapped on startup, so this normally reads
  // "installed". Anything else explains what is missing.
  const bridgeLabel: Record<string, string> = {
    installed: "已就绪",
    repaired: "已自动修复",
    "installed-fresh": "已自动安装",
    "no-dsh": "未检测到 DSH",
    "no-bundle": "缺少内嵌 bundle",
    failed: "自动安装失败",
    unknown: "尚未检查",
  }
  const bridgeTone =
    bridge === "installed" || bridge === "repaired" || bridge === "installed-fresh"
      ? "success"
      : bridge === "unknown"
        ? "default"
        : "warning"

  return (
    <Page
      title={props.plugin.name}
      subtitle={t("驱动本地 DeepSeek Harness 执行长任务。")}
    >
      <Stack gap={16}>
        <Section>
          <Grid cols={2} gap={12}>
            <StatCard label={t("Harness 状态")} value={harness.ready ? t("可用") : t("未就绪")} />
            <StatCard label={t("累计运行")} value={String(state.runs_total ?? 0)} />
            <StatCard
              label={t("调用方式")}
              value={harness.sdk_available ? t("Python SDK") : t("dsh CLI")}
            />
            <Card title={t("DSH 侧桥接")}>
              <StatusBadge tone={bridgeTone} label={bridgeLabel[bridge] || bridge} />
            </Card>
          </Grid>
        </Section>

        {!harness.ready ? (
          <Alert tone="warning">
            {t(
              "未找到 DeepSeek Harness。请安装 Python SDK（pip install deepseek-harness-sdk），或在配置中指定 repo_path / dsh_executable。",
            )}
          </Alert>
        ) : null}

        <Card title={t("Harness")}>
          <Stack gap={12}>
            <KeyValue
              items={[
                {
                  label: t("Python SDK"),
                  value: (
                    <StatusBadge
                      tone={harness.sdk_available ? "success" : "default"}
                      status={harness.sdk_available ? "ok" : "missing"}
                    />
                  ),
                },
                {
                  label: t("dsh 可执行文件"),
                  value: harness.dsh_executable || "—",
                },
                { label: t("模型"), value: settings.model || "—" },
                { label: t("Profile"), value: settings.profile || "—" },
                {
                  label: t("任务超时（秒）"),
                  value: String(settings.task_timeout ?? "—"),
                },
                { label: t("工作目录"), value: settings.workspace || "—" },
              ]}
            />
            {refresh ? <ActionButton action={refresh} label={t("刷新")} /> : null}
          </Stack>
        </Card>

        <Card title={t("运行任务")}>
          {runTask ? (
            <ActionForm
              action={runTask}
              submitLabel={t("交给 harness 执行")}
              successMessage={t("任务已提交。")}
            />
          ) : (
            <Text>{t("任务入口未暴露。")}</Text>
          )}
        </Card>

        <Card title={t("最近运行")}>
          <DataTable<RunRecord>
            data={runs}
            rowKey="session_id"
            emptyText={t("还没有运行记录。")}
            maxRows={20}
            columns={[
              { key: "session_id", label: t("会话") },
              { key: "task_chars", label: t("任务长度") },
              { key: "elapsed_seconds", label: t("耗时（秒）") },
              { key: "mode", label: t("方式") },
              {
                key: "ok",
                label: t("结果"),
                render: (row) => (
                  <StatusBadge
                    tone={row.ok ? "success" : "danger"}
                    status={row.ok ? "ok" : "failed"}
                  />
                ),
              },
            ]}
          />
        </Card>
      </Stack>
    </Page>
  )
}
