"""Hermes native entrypoint for Claude sessions."""

if __package__:
    from .hermes_claude_sessions.plugin import register
else:
    from hermes_claude_sessions.plugin import register

__all__ = ["register"]
