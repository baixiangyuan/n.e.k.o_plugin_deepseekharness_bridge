"""DeepSeek Harness bridge plugin for N.E.K.O.

Drives a local DeepSeek Harness (``dsh``) process so the catgirl can hand it
long, code-shaped tasks and read the result back into the conversation.

The harness is spawned through its Python SDK (``deepseek_harness``) when that
package is importable, and falls back to invoking the ``dsh`` CLI directly when
it is not. Both paths write into the same run log, so a failed SDK install is
visible rather than silent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

# DeepSeek Harness 支持的常见模型；UI 只展示其中一部分，这里供校验提示使用。
_KNOWN_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v41-flash",
    "deepseek-chat",
    "deepseek-reasoner",
)


def _truncate(text: str, limit: int = 6000) -> str:
    """Keep tool payloads small enough for a prompt without hiding that they were cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} chars omitted]"


@neko_plugin
class DeepseekharnessBridgePlugin(NekoPluginBase):
    """Expose a local DeepSeek Harness to N.E.K.O. as LLM tools."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._sdk_available: bool | None = None
        self._dsh_executable: str | None = None
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        # Resolve the harness once at startup so the first tool call is not
        # the thing that discovers a broken install.
        self._sdk_available = self._probe_sdk()
        self._dsh_executable = self._resolve_dsh_executable()
        self.logger.info(
            "deepseekharness_bridge ready (sdk=%s, dsh=%s)",
            "available" if self._sdk_available else "missing",
            self._dsh_executable or "not found",
        )
        return Ok({"status": "ready", "sdk": self._sdk_available})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        # Nothing to tear down: harness runs are one-shot subprocesses that the
        # SDK context manager owns. Just report what we saw.
        self.logger.info(
            "deepseekharness_bridge stopping after %d run(s)", len(self._runs)
        )
        return Ok({"status": "stopped", "runs": len(self._runs)})

    # --- hosted UI -----------------------------------------------------

    @ui.context(id="dashboard")
    async def dashboard(self):
        """面板状态：harness 可用性、当前配置摘要、最近运行记录。

        只回传元数据（会话 ID、长度、耗时、模式），不回传任务正文或模型
        回复——面板可能被围观，日志同理。
        """
        runs = [
            {
                "session_id": record["session_id"],
                "task_chars": record["task_chars"],
                "elapsed_seconds": record["elapsed_seconds"],
                "mode": record["mode"],
                "ok": record["ok"],
            }
            for record in list(self._runs.values())[-20:]
        ]

        return {
            "harness": {
                "sdk_available": self._sdk_available,
                "dsh_executable": self._dsh_executable,
                "ready": bool(self._sdk_available or self._dsh_executable),
            },
            "settings": {
                "model": str(await self._cfg("model", _KNOWN_MODELS[0])),
                "profile": str(await self._cfg("profile", "sdk-minimal")),
                "task_timeout": float(await self._cfg("task_timeout", 600)),
                "workspace": str(await self._workspace()),
                "repo_path": str(await self._cfg("repo_path", "")),
            },
            "runs": runs,
            "runs_total": len(self._runs),
        }

    @ui.action(refresh_context=True)
    @plugin_entry(
        id="panel_refresh",
        name="刷新面板",
        description="重新读取 harness 可用性与运行记录。",
    )
    async def panel_refresh(self, **_):
        # 状态由 @ui.context 现取，这里只需要一个成功的信号让面板重渲染。
        return Ok({"status": "refreshed"})

    @ui.action(refresh_context=True)
    @plugin_entry(
        id="panel_run_task",
        name="运行任务",
        description="把任务交给本地 DeepSeek Harness 执行。",
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要 harness 执行的任务，写清楚目标和验收标准",
                }
            },
            "required": ["task"],
        },
        timeout=900.0,
    )
    async def panel_run_task(self, task: str, **_):
        """面板专用入口：复用 run_task，但不把完整回复塞进面板状态。"""
        if not task or not task.strip():
            return Err(SdkError("任务不能为空"))

        outcome = await self.run_task(task)
        if isinstance(outcome, Err):
            return outcome

        payload = outcome.value
        return Ok({
            "ok": True,
            "session_id": payload["session_id"],
            "elapsed_seconds": payload["elapsed_seconds"],
            "mode": payload["mode"],
            "response_preview": payload["response"][:400],
        })

    # --- configuration -------------------------------------------------
    async def _cfg(self, key: str, default: Any = None) -> Any:
        """Read one dotted config value, tolerating an unreadable host config."""
        try:
            return await self.config.get(f"deepseekharness_bridge.{key}", default=default)
        except Exception as exc:  # host config unavailable — keep defaults
            self.logger.warning("config read failed for %s: %s", key, exc)
            return default

    async def _log_dir(self) -> Path:
        configured = await self._cfg("log_dir", "")
        if configured:
            path = Path(str(configured)).expanduser()
        else:
            path = self.storage_dir / "runs"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("cannot create run log dir %s: %s", path, exc)
        return path

    async def _workspace(self) -> Path:
        configured = await self._cfg("workspace", "")
        if configured:
            path = Path(str(configured)).expanduser()
        else:
            path = self.storage_dir / "workspace"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("cannot create workspace %s: %s", path, exc)
        return path

    # --- harness resolution -------------------------------------------

    def _probe_sdk(self) -> bool:
        """Whether the DeepSeek Harness Python SDK is installed here.

        Uses ``find_spec`` rather than importing: probing should not execute
        the SDK's import-time side effects, and the market's ruff config runs
        with ``--ignore-noqa``, so an ``# noqa: F401`` on a probe import would
        not suppress the unused-import warning.
        """
        try:
            return importlib.util.find_spec("deepseek_harness") is not None
        except (ImportError, ValueError):
            return False

    def _resolve_dsh_executable(self) -> str | None:
        """Find a usable ``dsh`` command, preferring the configured repo checkout."""
        configured = self.metadata.get("dsh_executable") if self.metadata else None
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return str(candidate)
            self.logger.warning("configured dsh_executable missing: %s", candidate)

        found = shutil.which("dsh")
        if found:
            return found

        # A source checkout exposes dsh through pnpm; look for the repo root.
        repo = self.metadata.get("repo_path") if self.metadata else None
        if repo:
            root = Path(str(repo)).expanduser()
            for candidate in (
                root / "node_modules" / ".bin" / "dsh.cmd",
                root / "node_modules" / ".bin" / "dsh",
            ):
                if candidate.exists():
                    return str(candidate)
        return None

    # --- entry points --------------------------------------------------

    @plugin_entry(
        id="run_task",
        name="让 DeepSeek Harness 执行任务",
        description=(
            "把一个较长或偏代码的任务交给本地 DeepSeek Harness 执行，"
            "例如读写文件、跑测试、分析仓库，然后返回它的最终回复。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要 harness 执行的任务描述"},
                "session_id": {
                    "type": "string",
                    "description": "可选的会话 ID，用于在多次调用间延续同一个会话",
                },
            },
            "required": ["task"],
        },
        timeout=900.0,
    )
    async def run_task(self, task: str, session_id: str = "", **_):
        if not task or not task.strip():
            return Err(SdkError("task 不能为空"))

        session = (session_id or "").strip() or f"neko-{int(time.time())}"
        self.logger.info("dispatching harness task (session=%s, len=%d)", session, len(task))

        try:
            outcome = await self._invoke_harness(task, session)
        except Exception as exc:
            self.logger.exception("harness run failed")
            return Err(SdkError(f"DeepSeek Harness 执行失败：{exc}"))

        return Ok(outcome)

    @plugin_entry(
        id="status",
        name="Harness 状态",
        description="检查本地 DeepSeek Harness 是否可用，以及本插件的运行记录。",
    )
    async def status(self, **_):
        return Ok({
            "sdk_available": self._sdk_available,
            "dsh_executable": self._dsh_executable,
            "python": sys.version.split()[0],
            "runs_recorded": len(self._runs),
        })

    # --- LLM tools -----------------------------------------------------

    @llm_tool(
        name="dsh_run_task",
        description=(
            "把代码、文件、测试或仓库分析类的长任务交给本地 DeepSeek Harness 执行，"
            "并取回它的最终回复。适合需要读写文件、运行命令或多步骤推理的任务；"
            "不适合闲聊或一句话就能答完的问题。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要 DeepSeek Harness 执行的任务，写清楚目标和验收标准",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，用于延续同一个 harness 会话",
                },
            },
            "required": ["task"],
        },
        timeout=900.0,
    )
    async def dsh_run_task(self, *, task: str, session_id: str = ""):
        outcome = await self.run_task(task, session_id=session_id)
        if isinstance(outcome, Ok):
            return outcome.value
        return {"ok": False, "error": str(getattr(outcome, "error", outcome))}

    # --- harness invocation --------------------------------------------

    async def _invoke_harness(self, task: str, session: str) -> dict[str, Any]:
        """Run one harness task, preferring the SDK and falling back to the CLI."""
        started = time.time()
        workspace = await self._workspace()
        model = str(await self._cfg("model", _KNOWN_MODELS[0]))
        profile = str(await self._cfg("profile", "sdk-minimal"))
        timeout = float(await self._cfg("task_timeout", 600))
        repo = str(await self._cfg("repo_path", ""))

        if self._sdk_available:
            result = await self._run_via_sdk(task, session, workspace, model, profile, repo, timeout)
        elif self._dsh_executable:
            result = await self._run_via_cli(task, session, workspace, model, profile, self._dsh_executable, timeout)
        else:
            raise RuntimeError(
                "未找到 DeepSeek Harness。请安装 Python SDK "
                "(pip install deepseek-harness-sdk) 或在配置中指定 repo_path / dsh_executable。"
            )

        elapsed = round(time.time() - started, 2)
        record = {
            "session_id": session,
            "task_chars": len(task),
            "elapsed_seconds": elapsed,
            "mode": "sdk" if self._sdk_available else "cli",
            "ok": True,
        }
        self._runs[session] = record
        await self._append_run_log(record)

        return {
            "ok": True,
            "session_id": session,
            "response": _truncate(str(result)),
            "elapsed_seconds": elapsed,
            "mode": record["mode"],
        }

    async def _run_via_sdk(
        self,
        task: str,
        session: str,
        workspace: Path,
        model: str,
        profile: str,
        repo: str,
        timeout: float,
    ) -> str:
        """Run through the DeepSeek Harness Python SDK."""
        from deepseek_harness import DeepSeekHarness

        kwargs: dict[str, Any] = {
            "provider": "deepseek-official",
            "model": model,
            "cwd": str(workspace),
            "profile": profile,
        }
        if repo:
            # The SDK expects an isolated home; the repo checkout is not one, so
            # only pass it when the user explicitly configured a checkout.
            kwargs["dsh_home"] = str(Path(repo).expanduser() / ".dsh-home")

        def _blocking_run() -> str:
            with DeepSeekHarness(**kwargs) as harness:
                result = harness.run(task, session_id=session)
                return str(getattr(result, "final_response", result))

        return await asyncio.wait_for(
            asyncio.to_thread(_blocking_run), timeout=timeout
        )

    async def _run_via_cli(
        self,
        task: str,
        session: str,
        workspace: Path,
        model: str,
        profile: str,
        executable: str,
        timeout: float,
    ) -> str:
        """Run through the ``dsh`` CLI as a one-shot subprocess."""
        repo = str(await self._cfg("repo_path", ""))
        if repo and "node_modules" in executable:
            cwd = repo
        else:
            cwd = str(workspace)

        # `dsh run` prints the final assistant response to stdout; a prompt file
        # keeps the task out of argv and out of the process table.
        prompt_file = workspace / f".dsh-prompt-{session}.txt"
        prompt_file.write_text(task, encoding="utf-8")

        cmd = [
            executable,
            "run",
            "--profile", profile,
            "--model", model,
            "--prompt-file", str(prompt_file),
            "--session-id", session,
        ]

        env = dict(os.environ)
        env.setdefault("DSH_WORKSPACE", str(workspace))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        finally:
            if proc.returncode is None:
                proc.kill()
            try:
                prompt_file.unlink()
            except OSError:
                pass

        text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not text:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"dsh 退出码 {proc.returncode}: {_truncate(err, 1500)}")
        if stderr:
            self.logger.debug("dsh stderr: %s", _truncate(stderr.decode('utf-8', errors='replace'), 1500))
        return text

    # --- run log -------------------------------------------------------

    async def _append_run_log(self, record: dict[str, Any]) -> None:
        """Append one JSON line per run so operators can audit usage."""
        import json

        log_dir = await self._log_dir()
        path = log_dir / "runs.jsonl"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.logger.warning("cannot append run log: %s", exc)
