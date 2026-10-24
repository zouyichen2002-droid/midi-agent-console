"""Which tool calls are dangerous enough to need a long-press, and a one-line summary
of each call for the human.

The shell text is looked at in two normalised forms:
  * command form: argv lists joined, quotes removed, escaping backslashes dropped
    (so `"rm" -rf` and `r\\m -rf` read as rm), used for the command rules;
  * path form: quotes removed, backslashes turned into slashes (so
    `$env:USERPROFILE\\.claude\\settings.json` keeps its path), used for protected paths.
Rules are searched across the whole command, so `ls && rm -rf x` cannot hide behind a
harmless prefix. Protected paths count when the command writes, moves or deletes
(redirects, Set-Content, cp, mv, rm, sed -i ...); merely reading them stays normal.
A shell or write call whose arguments are unknown is dangerous (fail closed).
This is a best-effort list, not a sandbox: anything it misses still needs a tap.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

_GIT = (r"\bgit(?:\.exe)?\b(?:\s+(?:-C\s+\S+|-c\s+\S+|--[\w-]+(?:=\S+)?|-[A-Za-z]))*\s+")  # git + global options
_RM = r"(?:remove-item|ri|del|erase|rd|rmdir|rm)"

_SHELL_DANGER = [
    (_GIT + r"(?:subtree\s+)?push\b", "git push"),
    (_GIT + r"(?:reset\b.*--hard|clean\b.*\s-\w*f|checkout\s+(?:--\s|\.(?:\s|$)|-f\b)|"
            r"restore\s+(?!--staged)|switch\b.*--discard-changes|branch\s+-D|rebase\b|filter-branch|"
            r"filter-repo|update-ref\s+-d|stash\s+(?:clear|drop)|gc\b.*--prune|reflog\s+(?:expire|delete))",
     "git history rewrite / discard"),
    (r"--force(?:-with-lease)?(?=\s|=|$)", "--force"),
    (r"(?<![\w-])rm\s+(?!.*--cached)(?:\S+\s+)*-\w*[rf]\w*\b|(?<![\w-])rm\s+(?:\S+\s+)*--(?:recursive|force)\b",
     "rm -r / -f"),
    (r"(?<![\w-])" + _RM + r"\b[^|;&\n]*(?:\s|^)\S*\*", "delete with a wildcard"),
    (r"(?<![\w-])" + _RM + r"\b(?![^|;&\n]*--cached)[^|;&\n]*\s-(?:r|rec|recu|recur|recurs|recurse|fo|forc|force)\w*\b",
     "recursive / forced delete"),
    (r"\b(?:rd|rmdir)\b[^|;&\n]*\s/s\b|\b(?:del|erase)\b[^|;&\n]*\s/[sq]\b", "rd /s, del /s"),
    (r"\|\s*(?:%\s*\{[^}]*\.delete\(|(?:remove-item|ri|del|rm)\b)", "piped delete"),
    (r"\bxargs\s+(?:\S+\s+)*\S*rm\b|-exec(?:dir)?\s+\S*rm\b|\bfind\b[^|;&\n]*\s-delete\b", "bulk delete"),
    (r"\b(?:rmtree|rmsync|unlinksync|rimraf|removedirs|unlink)\b|\.delete\(|\[io\.(?:directory|file)\]::delete",
     "scripted delete"),
    (r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;\n]*\|\s*(?:sudo\s+)?"
     r"(?:sh|bash|zsh|python\d?|node|pwsh|powershell|iex|invoke-expression)\b", "pipe download to a shell"),
    (r"<\(\s*(?:curl|wget)\b", "run a downloaded script"),
    (r"(?:curl|wget)\b[^;\n]*(?:&&|;)\s*(?:sudo\s+)?(?:ba|z)?sh\b", "download then run"),
    (r"\biex\b|\binvoke-expression\b|downloadstring\(|\bnet\.webclient\b", "invoke-expression"),
    (r"\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-e(?:c|nc|nco|ncod|ncode|ncoded|ncodedcommand)?\b(?!-)",
     "encoded PowerShell"),
    (r"&\s*\$\w+|&\s*\(\s*['\"]?\w*['\"]?\s*\+|\$\{?IFS\b|\$\w+\s+-r\w*f|\$\w+\s+-rf", "indirect command"),
    (r"\b(?:format-volume|stop-computer|restart-computer|shutdown|bcdedit|vssadmin|diskpart|mkfs)\b|"
     r"\bformat\s+[a-z]:", "power / disk / boot"),
    (r"\breg\s+(?:delete|add|import)\b|\bremove-itemproperty\b|\bset-itemproperty\b.*hk(?:lm|cu)", "registry"),
    (r"\bset-executionpolicy\b", "Set-ExecutionPolicy"),
    (r"(?<![\w-])(?:taskkill|stop-process|kill|pkill|killall)(?![\w-])", "kill processes"),
    (r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\buv\s+publish\b|\bgh\s+release\s+create\b|"
     r"\bgh\s+repo\s+(?:delete|edit)\b", "publish / repo settings"),
    (r"\bdrop\s+(?:table|database)\b|\btruncate\s+table\b", "SQL drop"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b|\bdd\s+if=", "recursive perms / dd"),
    (r"\bsudo\b|\brunas\b|\bstart-process\b.*-verb\s+runas", "elevation"),
]
# secrets: dangerous on any access from a shell (reading them is how they leak)
_SECRET_PATHS = [
    (r"(?:^|/)\.env(?!\.(?:example|sample|template|dist)\b)(?:\.\w+)?$", ".env"),
    (r"(?:^|/)\.ssh/", ".ssh"),
    (r"\.(?:pem|key|pfx|p12)$", "key file"),
    (r"midi-agent-console/token$", "this console's token"),
]
# configuration that controls agents / CI / this gate: dangerous when written
_PATH_DANGER = _SECRET_PATHS + [
    (r"(?:^|/)\.git/(?:hooks|config)", ".git hooks / config"),
    (r"(?:^|/)\.github/workflows/", "CI workflow"),
    (r"(?:^|/)\.claude/settings[^/]*\.json$|(?:^|/)\.claude\.json$", "Claude settings (could remove this gate)"),
    (r"(?:^|/)\.mcp\.json$", "MCP config"),
    (r"(?:^|/)\.codex/", "Codex config"),
    (r"cordis\.patch\.yml$", "DSH profile"),
    (r"midi-agent-console/audit\.jsonl$", "this console's audit log"),
]
# a shell command that writes, moves or deletes (only then do protected paths matter)
_WRITES = re.compile(
    r">|\b(?:set-content|add-content|out-file|new-item|ni|copy-item|cp|copy|move-item|mv|move|tee|"
    r"remove-item|ri|del|erase|rm|rmdir|rd|ren|rename-item|install|truncate|chmod|chown)\b|\bsed\s+-i\b|"
    r"\b(?:git\s+(?:checkout|restore|mv|rm))\b", re.IGNORECASE)
# tool names across harnesses: Claude Code (Bash/Write/Edit...), DSH (pwsh/bash/write/edit/
# str_replace_editor), Codex (Bash, shell with argv lists, apply_patch whose command is the patch)
_SHELL_TOOLS = {"Bash", "PowerShell", "bash", "pwsh", "powershell", "shell", "local_shell"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit", "str_replace_editor"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
_HW_TOOLS = ("bind_control", "teleop_start", "reset_emergency_stop")


def _join(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return shlex.join(str(c) for c in command)
    return str(command or "")


def normalize(command: Any) -> str:
    """Command form: drop quotes, escaping backslashes, cmd ^ escapes, repeated spaces."""
    s = re.sub(r"[\"'`]", "", _join(command))
    s = re.sub(r"\\(?=[A-Za-z])", "", s)  # r\m -> rm, \rm -> rm
    s = s.replace("^", "")
    return re.sub(r"\s+", " ", s).strip()


def path_form(command: Any) -> str:
    """Path form: drop quotes, turn backslashes into slashes; paths stay intact."""
    s = re.sub(r"[\"'`]", "", _join(command)).replace("\\", "/")
    return re.sub(r"\s+", " ", s).strip()


def _dangerous_path(path: str, rules=None) -> str | None:
    p = path.strip().replace("\\", "/").rstrip(". ")  # Windows ignores trailing dots / spaces
    p = re.sub(r"::\$\w+$", "", p)  # NTFS alternate stream: ".env::$DATA"
    for pat, why in rules or _PATH_DANGER:
        if re.search(pat, p, re.IGNORECASE):
            return why
    return None


def _dangerous_path_in_text(cmd: str, rules=None) -> str | None:
    for token in re.split(r"[\s>|;&(),=]+", cmd):
        if token:
            why = _dangerous_path(token, rules)
            if why:
                return why
    return None


def classify(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """-> ("dangerous" | "normal", reason)."""
    tool_input = tool_input or {}
    base = tool_name.split("__")[-1]  # mcp__server__tool -> tool
    if base in _HW_TOOLS:
        return "dangerous", f"{base} (moves hardware / changes keyboard control)"
    if base == "device_send":
        cmd = str(tool_input.get("command", "")).strip().upper()
        if cmd.startswith(("DRIVE", "SERVO", "RESET")):
            return "dangerous", f"hardware motion ({cmd.split()[0]})"
        return "normal", ""
    if tool_name == "apply_patch":
        patch = str(tool_input.get("command") or tool_input.get("input") or tool_input.get("patch") or "")
        if not patch:
            return "dangerous", "patch content unknown"
        if re.search(r"^\*\*\* Delete File:", patch, re.MULTILINE):
            return "dangerous", "patch deletes a file"
        for a, b in _PATCH_FILE.findall(patch):
            why = _dangerous_path(a or b)
            if why:
                return "dangerous", why
        return "normal", ""
    if tool_name in _SHELL_TOOLS:
        raw = tool_input.get("command")
        if not raw:
            return "dangerous", "command unknown"
        for form in (normalize(raw), path_form(raw)):
            for pat, why in _SHELL_DANGER:
                if re.search(pat, form, re.IGNORECASE):
                    return "dangerous", why
        paths = path_form(raw)
        why = _dangerous_path_in_text(paths, _SECRET_PATHS)
        if why:
            return "dangerous", f"touches {why}"
        if _WRITES.search(paths):
            why = _dangerous_path_in_text(paths)
            if why:
                return "dangerous", f"writes {why}"
    if tool_name in _WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
        if not path:
            return "dangerous", "target file unknown"
        why = _dangerous_path(path)
        if why:
            return "dangerous", why
    return "normal", ""


def summarize(tool_name: str, tool_input: dict[str, Any], limit: int = 160) -> str:
    tool_input = tool_input or {}
    for key in ("command", "file_path", "notebook_path", "path", "url", "pattern", "query", "prompt", "template"):
        if tool_input.get(key):
            v = tool_input[key]
            text = " ".join((shlex.join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)).split())
            return f"{tool_name}: {text[:limit]}" + ("…" if len(text) > limit else "")
    return f"{tool_name} (no arguments shown)"
