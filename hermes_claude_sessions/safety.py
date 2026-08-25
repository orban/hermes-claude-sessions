"""Shared confidentiality helpers for model-facing Claude metadata."""

from __future__ import annotations

from typing import Any


def without_prompt(value: Any, prompt: Any) -> Any:
    """Remove every prompt occurrence without using an aliasable sentinel."""
    if not isinstance(value, str) or not isinstance(prompt, str) or not prompt:
        return value
    redacted = value
    while prompt in redacted:
        redacted = redacted.replace(prompt, "")
    return redacted
