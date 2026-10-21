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

# Model catalog the running DeepSeek Harness ships, read from its own source
# (@deepseek-ai/dsh-llm-deepseek DEFAULT_MODELS in 0.2.1-rc.2). The headless
# profile disables the account provider, so the online /models listing is not
# available and this built-in catalog is the source of truth.
#
# Keep in sync when DSH ships new models; an unknown id fails at request time
# with MODEL_UNSUPPORTED.
_MODEL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "id": "deepseek-flash",
        "name": "DeepSeek-V4.1-Flash",
        "description": "默认选择：快且支持图像输入，适合大多数任务。",
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek-V4-Pro",
        "description": "更强的编码与推理，适合复杂任务，成本更高。",
    },
)

_DEFAULT_MODEL = "deepseek-flash"

# Model ids the catalog offers, for config validation and the panel dropdown.
_KNOWN_MODELS = tuple(entry["id"] for entry in _MODEL_CATALOG)


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
        # Set when the CLI is the one bundled inside the DSH desktop app:
        # (exe, cli.js) — that pair is how it must be launched.
        self._dsh_desktop_launcher: tuple[str, str] | None = None
        self._runs: dict[str, dict[str, Any]] = {}
        self._dsh_bridge_state: str = "unknown"
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        # Resolve the harness once at startup so the first tool call is not
        # the thing that discovers a broken install.
        self._sdk_available = self._probe_sdk()
        self._dsh_executable = self._resolve_dsh_executable()

        # Bootstrap the other direction: make sure the DSH side of the bridge
        # is installed, so starting this plugin is the only step a user needs.
        dsh_state = await self._ensure_dsh_bridge_installed()

        self.logger.info(
            "deepseekharness_bridge ready (sdk=%s, dsh=%s, dsh_bridge=%s)",
            "available" if self._sdk_available else "missing",
            self._dsh_executable or "not found",
            dsh_state,
        )
        return Ok({
            "status": "ready",
            "sdk": self._sdk_available,
            "dsh_bridge": dsh_state,
        })

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
            "dsh_bridge": self._dsh_bridge_state,
            "settings": {
                "model": str(await self._cfg("model", _DEFAULT_MODEL)),
                "profile": str(await self._cfg("profile", "headless")),
                "task_timeout": float(await self._cfg("task_timeout", 600)),
                "workspace": str(await self._workspace()),
                "repo_path": str(await self._cfg("repo_path", "")),
            },
            # The panel renders this as a dropdown, so a user picks a model
            # instead of typing an id that may not exist.
            "models": [dict(entry) for entry in _MODEL_CATALOG],
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

    @ui.action(refresh_context=True)
    @plugin_entry(
        id="panel_set_model",
        name="切换模型",
        description="选择 DeepSeek Harness 执行任务时使用的模型。",
        input_schema={
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "模型 ID",
                    "enum": list(_KNOWN_MODELS),
                }
            },
            "required": ["model"],
        },
    )
    async def panel_set_model(self, model: str, **_):
        """Persist the chosen model so later runs use it.

        The catalog is the built-in one the running DSH ships; the enum in the
        schema is what makes the panel render a dropdown instead of a free-text
        field, so a user cannot pick an id DSH does not know.
        """
        if model not in _KNOWN_MODELS:
            return Err(SdkError(
                f"未知模型 {model}；可用：{', '.join(_KNOWN_MODELS)}"
            ))

        await self.config.update({"deepseekharness_bridge.model": model})
        self.logger.info("model switched to %s", model)
        return Ok({"ok": True, "model": model})

    # --- DSH side bootstrap --------------------------------------------

    async def _ensure_dsh_bridge_installed(self) -> str:
        """Make sure the DSH side of the bridge is ready to load.

        The N.E.K.O. plugin and the dsh bundle ship together, so starting this
        plugin is the only step a user should need. This checks the DSH desktop
        profile and installs the bundle when it is missing or incomplete.

        Returns a short state string for logs and the panel: ``installed``,
        ``repaired``, ``installed-fresh``, or a reason it was skipped
        (``no-dsh``, ``failed``).
        """
        try:
            return await asyncio.to_thread(self._ensure_dsh_bridge_installed_sync)
        except Exception as exc:
            # A failure here must never stop the plugin from loading: the
            # N.E.K.O. -> DSH direction works without the DSH-side bundle.
            self.logger.warning("dsh bridge bootstrap failed: %s", exc)
            return "failed"

    def _find_dsh_install(self) -> Path | None:
        """Locate a DeepSeek Harness desktop installation."""
        # 1. Explicit config wins.
        configured = self.metadata.get("dsh_install_path") if self.metadata else None
        if configured:
            candidate = Path(str(configured)).expanduser()
            if (candidate / "DeepSeek Harness.exe").exists():
                return candidate

        # 2. Standard install locations.
        roots = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DeepSeek Harness",
            Path("D:/DeepSeek Harness"),
            Path(os.environ.get("PROGRAMFILES", "")) / "DeepSeek Harness",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "DeepSeek Harness",
        ]
        for root in roots:
            try:
                if (root / "DeepSeek Harness.exe").exists():
                    return root
            except OSError:
                continue

        # 3. Fall back to the running process, if any.
        exe = self._find_dsh_process_exe()
        if exe:
            return exe.parent

        return None

    def _find_dsh_process_exe(self) -> Path | None:
        """Find the DSH desktop exe from the process table (Windows only)."""
        if sys.platform != "win32":
            return None
        try:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq DeepSeek Harness.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if "DeepSeek Harness.exe" not in out.stdout:
                return None
        except Exception:
            return None

        # tasklist gives the image name, not the path. Ask WMI instead, which
        # does report ExecutablePath.
        try:
            import subprocess

            ps = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-Process -Name 'DeepSeek Harness' | Select-Object -First 1).Path",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            line = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
            if line and Path(line).exists():
                return Path(line)
        except Exception:
            pass
        return None

    def _dsh_bundle_source(self) -> Path | None:
        """Where the dsh-plugin-neko bundle lives.

        The embedded copy under ``bundled/dsh-plugin-neko`` is the primary
        source: it ships inside this plugin, so the bootstrap works no matter
        where the user installed the N.E.K.O. plugin from. A sibling checkout
        is checked as a fallback for source-tree development.
        """
        candidates = [
            self.plugin_dir / "bundled" / "dsh-plugin-neko",
            self.plugin_dir.parent.parent.parent / "dsh-plugin-neko",
            self.plugin_dir.parent.parent.parent.parent / "dsh-plugin-neko",
        ]
        for candidate in candidates:
            if (candidate / "package.json").exists() and (candidate / "lib" / "index.js").exists():
                return candidate
        return None

    def _ensure_dsh_bridge_installed_sync(self) -> str:
        """Synchronous body of the DSH-side bootstrap."""
        dsh = self._find_dsh_install()
        if dsh is None:
            self._dsh_bridge_state = "no-dsh"
            self.logger.info("no DeepSeek Harness installation found; DSH-side bridge skipped")
            return "no-dsh"

        source = self._dsh_bundle_source()
        if source is None:
            self._dsh_bridge_state = "no-bundle"
            self.logger.warning(
                "dsh-plugin-neko bundle not found beside the plugin; DSH-side bridge skipped"
            )
            return "no-bundle"

        home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
        profile_dir = home / "profiles" / "desktop"
        target = profile_dir / "node_modules" / "dsh-plugin-neko"

        manifest_ok = (target / "package.json").exists()
        entry_ok = (target / "lib" / "index.js").exists()
        registered = self._profile_lists_bundle(profile_dir)

        if manifest_ok and entry_ok and registered:
            self._dsh_bridge_state = "installed"
            return "installed"

        # Copy the bundle into the profile. A copy (not a symlink) keeps the
        # install self-contained: the profile survives the source tree moving.
        if not (manifest_ok and entry_ok):
            target.mkdir(parents=True, exist_ok=True)
            for name in ("package.json", "cordis.patch.yml", "README.md"):
                src = source / name
                if src.exists():
                    shutil.copyfile(src, target / name)
            lib_src = source / "lib"
            if lib_src.exists():
                shutil.copytree(lib_src, target / "lib", dirs_exist_ok=True)
            self.logger.info("copied dsh-plugin-neko into %s", target)

        if not registered:
            self._register_bundle_in_profile(profile_dir)

        # Something was already there: this run fixed it up rather than doing
        # a clean install.
        state = "repaired" if (manifest_ok or entry_ok or registered) else "installed-fresh"
        self._dsh_bridge_state = state
        return state

    def _profile_lists_bundle(self, profile_dir: Path) -> bool:
        """Whether the profile manifest already lists the bundle."""
        import json

        manifest = profile_dir / "package.json"
        if not manifest.exists():
            return False
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
        return "dsh-plugin-neko" in bundles

    def _register_bundle_in_profile(self, profile_dir: Path) -> None:
        """Add the bundle to the profile manifest and its dependency list."""
        import json

        manifest = profile_dir / "package.json"
        if not manifest.exists():
            self.logger.warning("DSH desktop profile manifest not found at %s", manifest)
            return

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.logger.warning("cannot read DSH profile manifest: %s", exc)
            return

        dsh_section = data.setdefault("dsh", {}).setdefault("profile", {})
        bundles = dsh_section.setdefault("bundles", [])
        if "dsh-plugin-neko" not in bundles:
            bundles.append("dsh-plugin-neko")

        deps = data.setdefault("dependencies", {})
        deps["dsh-plugin-neko"] = "file:" + str(
            (profile_dir / "node_modules" / "dsh-plugin-neko").as_posix()
        )

        manifest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self.logger.info("registered dsh-plugin-neko in %s", manifest)

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

        # The desktop app puts no `dsh` on PATH, but it ships a working CLI
        # inside its own asar. This is the common install, so check it before
        # giving up.
        desktop = self._find_dsh_install()
        if desktop is not None:
            exe = desktop / "DeepSeek Harness.exe"
            asar = desktop / "resources" / "app.asar"
            # cli.js lives *inside* app.asar, a single archive file, so
            # Path.exists() on the full path is always False — only the app's
            # own Electron runtime resolves asar entries. Probe the archive
            # itself and let the CLI report a missing entry point if the layout
            # ever changes.
            cli = (
                asar / "dsh" / "node_modules"
                / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js"
            )
            if exe.exists() and asar.exists():
                # That pair is how the desktop CLI must be launched: the exe
                # doubles as a node runtime under ELECTRON_RUN_AS_NODE.
                self._dsh_desktop_launcher = (str(exe), str(cli))
                self.logger.info("using the DSH desktop CLI at %s", cli)
                return str(cli)

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
        model = str(await self._cfg("model", _DEFAULT_MODEL))
        profile = str(await self._cfg("profile", "headless"))
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
        """Run through the ``dsh`` CLI as a one-shot subprocess.

        Two launch shapes: a real ``dsh`` binary (source checkout or PATH), and
        the CLI bundled inside the DSH desktop app, which has to be started
        through that app's exe with ELECTRON_RUN_AS_NODE=1.

        The headless profile takes the task as a positional argument and prints
        the final answer to stdout — there is no `run` subcommand and no
        ``--prompt-file``. The task therefore does land in argv; the trade-off
        is accepted because this is a local, single-user process and the CLI
        offers no alternative.
        """
        repo = str(await self._cfg("repo_path", ""))
        launcher = self._dsh_desktop_launcher

        if launcher is not None and executable == launcher[1]:
            argv = [launcher[0], "--expose-internals", launcher[1]]
            cwd = str(workspace)
        elif repo and "node_modules" in executable:
            argv = [executable]
            cwd = repo
        else:
            argv = [executable]
            cwd = str(workspace)

        argv += ["--profile", profile]

        # No --session-id here: the CLI only accepts an id for a session that
        # already exists, and each run starts a fresh one. The session id is
        # still tracked locally for the run log and panel.
        if model:
            # The task is positional and goes last, so every flag precedes it.
            argv += ["--model", model]

        argv.append(task)

        env = dict(os.environ)
        env.setdefault("DSH_WORKSPACE", str(workspace))
        if launcher is not None:
            # Makes the exe behave as a node runtime rather than opening the GUI.
            env["ELECTRON_RUN_AS_NODE"] = "1"

        proc = await asyncio.create_subprocess_exec(
            *argv,
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
