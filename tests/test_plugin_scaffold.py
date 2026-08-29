from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]


def _load_root_entrypoint() -> ModuleType:
    module_name = "claude_sessions_entrypoint"
    namespace = f"{module_name}."
    existing = {
        name: module
        for name, module in sys.modules.items()
        if name == module_name or name.startswith(namespace)
    }
    for name in existing:
        sys.modules.pop(name)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(namespace):
                sys.modules.pop(name)
        sys.modules.update(existing)
    return module


def test_root_entrypoint_exposes_plugin_register() -> None:
    entrypoint = _load_root_entrypoint()

    assert entrypoint.register.__name__ == "register"
    assert entrypoint.register.__module__.endswith(".hermes_claude_sessions.plugin")
    assert not any(
        name == "claude_sessions_entrypoint"
        or name.startswith("claude_sessions_entrypoint.")
        for name in sys.modules
    )


def test_register_does_not_import_characterization() -> None:
    sys.modules.pop("hermes_claude_sessions.plugin", None)
    sys.modules.pop("tests.characterization.support", None)

    from hermes_claude_sessions.plugin import register

    register(object())

    assert "tests.characterization.support" not in sys.modules


def test_distribution_contains_only_runtime_package_at_manifest_version() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    manifest_version = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "plugin.yaml").read_text().splitlines()
        if line.startswith("version:")
    )

    assert config["project"]["version"] == manifest_version == "0.5.0"
    discovery = config["tool"]["setuptools"]["packages"]["find"]
    assert discovery["include"] == ["hermes_claude_sessions*"]
    assert discovery["exclude"] == ["tests*", "skills*"]
    assert discovery["namespaces"] is False
    assert not (ROOT / "hermes_claude_sessions" / "characterization.py").exists()
    assert (ROOT / "tests" / "characterization" / "support.py").is_file()
    package_data = config["tool"]["setuptools"]["package-data"]
    assert package_data["hermes_claude_sessions"] == [
        "claude_plugin/.claude-plugin/plugin.json",
        "claude_plugin/hooks/hooks.json",
        "claude_plugin/hooks/event_bridge.py",
        "claude_plugin/hooks/permission_bridge.py",
    ]
