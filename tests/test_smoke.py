"""deepseekharness_bridge 的冒烟测试。

这些测试刻意不依赖 N.E.K.O. 宿主或真实 harness：只验证插件自身的纯逻辑
（manifest 契约、工具名、参数转换），因此可以在没有运行环境的机器上跑。

插件目录通过 ``NEKO_PLUGIN_DIR`` 或相对本文件推断获得，不依赖 pytest 的包
导入机制——插件根目录的 ``__init__.py`` 导入 ``plugin.sdk``，在 CI 里并不存在。
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path

import pytest

# 优先用环境变量，其次相对本文件推断（tests/ 的上一级）。
PLUGIN_DIR = Path(
    os.environ.get("NEKO_PLUGIN_DIR") or Path(__file__).resolve().parent.parent
).resolve()


def _manifest() -> dict:
    with (PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_plugin_dir_resolves() -> None:
    """推断或配置的插件目录必须真的包含 plugin.toml。"""
    assert (PLUGIN_DIR / "plugin.toml").is_file(), f"未找到 plugin.toml：{PLUGIN_DIR}"


def test_manifest_required_fields() -> None:
    plugin = _manifest()["plugin"]

    assert plugin["id"] == "deepseekharness_bridge"
    assert plugin["name"]
    assert plugin["version"]
    assert plugin["entry"].startswith("plugin.plugins.deepseekharness_bridge:")


def test_folder_name_matches_plugin_id() -> None:
    """分发时源码目录名必须与插件 ID 一致，否则安装包按 ID 建目录会错位。"""
    assert PLUGIN_DIR.name == _manifest()["plugin"]["id"]


def test_runtime_flags_are_booleans() -> None:
    runtime = _manifest()["plugin_runtime"]
    assert isinstance(runtime["enabled"], bool)
    assert isinstance(runtime["auto_start"], bool)
    assert 0 < runtime["timeout"] <= 300


def test_entry_reference_is_well_formed() -> None:
    """入口必须形如 module.path:ClassName。"""
    entry = _manifest()["plugin"]["entry"]
    module_path, _, class_name = entry.partition(":")
    assert re.fullmatch(r"[A-Za-z_][\w.]*", module_path)
    assert re.fullmatch(r"[A-Za-z_]\w*", class_name)
    assert class_name.endswith("Plugin")


def test_no_requirements_txt() -> None:
    """包内不允许 requirements.txt；第三方依赖必须走 pyproject + vendor/。"""
    assert not (PLUGIN_DIR / "requirements.txt").exists()


def test_declares_no_external_dependencies() -> None:
    """本插件零第三方依赖：SDK 是可选项，运行时动态探测。"""
    pyproject = tomllib.loads((PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == []


def test_entry_module_syntax_is_valid() -> None:
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    ast.parse(source)


def test_entries_are_async() -> None:
    """运行时触发的 @plugin_entry / @llm_tool 处理器必须是 async def。"""
    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    decorated = {"plugin_entry", "llm_tool"}

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            name = deco.func.id if isinstance(deco, ast.Call) else getattr(deco, "id", None)
            if name in decorated:
                checked += 1
                assert isinstance(node, ast.AsyncFunctionDef), (
                    f"{node.name} 用 @{name} 装饰但不是 async def"
                )

    assert checked >= 3, "预期的入口数量没到，说明 AST 检查没生效"


def test_no_secrets_in_source() -> None:
    """源码里不能出现硬编码密钥。"""
    for path in sorted(PLUGIN_DIR.rglob("*.py")):
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{16,}", text), f"{path.name} 疑似含密钥"


def test_logs_do_not_record_task_body() -> None:
    """运行日志只记元数据。

    用 AST 取出每个 logger 调用的参数，检查其中没有名为 ``task`` 的变量
    （那会把任务正文写进日志）。日志文案里出现 "task" 这个词是允许的，
    如 "dispatching harness task"。
    """
    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "logger":
            continue

        for arg in node.args:
            names = {
                n.id for n in ast.walk(arg) if isinstance(n, ast.Name)
            }
            assert "task" not in names, (
                f"logger.{func.attr}() 的参数里出现 task 变量（第 {node.lineno} 行）"
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
