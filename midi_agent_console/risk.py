"""Which tool calls are dangerous enough to need a long-press, and a one-line summary
of each call for the human. Dangerous rules are checked against the whole command
string first, so `ls && rm -rf x` cannot hide behind a harmless prefix."""
from __future__ import annotations

import re
from typing import Any

_SHELL_DANGER = [
    (r"\brm\s+(-[a-z]*[rf][a-z]*\b|--recursive|--force)", "rm -r/-f"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|branch\s+-D|rebase\b)", "git history rewrite"),
    (r"--force\b|\s-f\s+origin\b", "--force"),
    (r"(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|python|pwsh|powershell)\b", "pipe download to shell"),
    (r"\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|]*\|\s*(iex|invoke-expression)\b", "download | iex"),
    (r"\bremove-item\b[^\n]*-(r|rec|recurse|fo|force)\b", "Remove-Item -Recurse/-Force"),
    (r"\b(rd|rmdir)\s+/s\b|\bdel\s+/[sq]\b", "rd /s, del /s"),
    (r"\b(format-volume|stop-computer|restart-computer|shutdown)\b", "power / disk"),
    (r"\bset-executionpolicy\b", "Set-ExecutionPolicy"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\buv\s+publish\b|\bgh\s+release\s+create\b", "publish"),
    (r"\bdrop\s+(table|database)\b|\btruncate\s+table\b", "SQL drop"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b|\bdd\s+if=", "recursive perms / dd"),
    (r"\bmkfs\b|\bdiskpart\b", "disk tools"),
]
_PATH_DANGER = [
    (r"(^|[\\/])\.env(\.|$)", ".env"),
    (r"(^|[\\/])\.ssh[\\/]", ".ssh"),
    (r"\.(pem|key|pfx|p12)$", "key file"),
    (r"(^|[\\/])\.git[\\/]hooks[\\/]", ".git/hooks"),
    (r"(^|[\\/])\.github[\\/]workflows[\\/]", "CI workflow"),
    (r"(^|[\\/])\.claude[\\/]settings[^\\/]*\.json$", "Claude settings (could remove this gate)"),
    (r"(^|[\\/])\.mcp\.json$", "MCP config"),
]
_SHELL_TOOLS = {"Bash", "PowerShell"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def classify(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """-> ("dangerous" | "normal", reason)."""
    if tool_name in _SHELL_TOOLS:
        cmd = str(tool_input.get("command", ""))
        for pat, why in _SHELL_DANGER:
            if re.search(pat, cmd, re.IGNORECASE):
                return "dangerous", why
    if tool_name in _WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        for pat, why in _PATH_DANGER:
            if re.search(pat, path, re.IGNORECASE):
                return "dangerous", why
    if tool_name.endswith("reset_emergency_stop"):
        return "dangerous", "clearing the emergency stop"
    return "normal", ""


def summarize(tool_name: str, tool_input: dict[str, Any], limit: int = 160) -> str:
    for key in ("command", "file_path", "notebook_path", "url", "pattern", "query", "prompt"):
        if tool_input.get(key):
            text = " ".join(str(tool_input[key]).split())
            return f"{tool_name}: {text[:limit]}" + ("…" if len(text) > limit else "")
    return tool_name
