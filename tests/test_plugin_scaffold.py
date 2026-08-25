from __future__ import annotations

import importlib.util
import sys
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
    sys.modules.pop("hermes_claude_sessions.characterization", None)

    from hermes_claude_sessions.plugin import register

    register(object())

    assert "hermes_claude_sessions.characterization" not in sys.modules
