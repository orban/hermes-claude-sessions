from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]


def _load_root_entrypoint() -> ModuleType:
    module_name = "claude_sessions_entrypoint"
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
        sys.modules.pop(module_name, None)
    return module


def test_root_entrypoint_exposes_plugin_register() -> None:
    entrypoint = _load_root_entrypoint()

    assert entrypoint.register.__name__ == "register"
    assert entrypoint.register.__module__.endswith(".hermes_claude_sessions.plugin")


def test_register_does_not_import_characterization() -> None:
    sys.modules.pop("hermes_claude_sessions.plugin", None)
    sys.modules.pop("hermes_claude_sessions.characterization", None)

    from hermes_claude_sessions.plugin import register

    register(object())

    assert "hermes_claude_sessions.characterization" not in sys.modules
