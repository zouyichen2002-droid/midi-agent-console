"""Which tool calls are dangerous enough to need a long-press, and a one-line summary
of each call for the human.

The shell text is normalised first (argv lists joined, quotes / backslashes / repeated
spaces removed) so `"rm" -rf`, `r\\m -rf` or `git  push` look like what they do, and the
rules are searched across the whole command, so `ls && rm -rf x` cannot hide behind a
harmless prefix. Protected paths are checked in shell text as well as in file writes.
A shell or write call whose arguments are unknown is dangerous (fail closed).
This is a best-effort list, not a sandbox: anything it misses still needs a tap.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

_GIT = r"\bgit\b(?:\s+(?:-C\s+\S+|-c\s+\S+|--[\w-]+(?:=\S+)?))*\s+"  # git + global options
_RM_ALIASES = r"(?:remove-item|ri|del|erase|rd|rmdir|rm)"

_SHELL_DANGER = [
    (_GIT + r"push\b", "git push"),
    (_GIT + r"(?:reset\b.*--hard|clean\b.*\s-\w*f|checkout\s+(?:--\s|\.(?:\s|$))|restore\s+(?:\.|--)|"
            r"branch\s+-D|rebase\b|filter-branch|filter-repo|update-ref\s+-d|stash\s+(?:clear|drop)|"
            r"gc\b.*--prune|reflog\s+(?:expire|delete))", "git history rewrite / discard"),
    (r"--force\b|--force-with-lease\b", "--force"),
    (r"\brm\s+(?:\S+\s+)*-\w*[rf]\w*\b|\brm\s+(?:\S+\s+)*--(?:recursive|force)\b|\brm\b[^|;&\n]*\*",
     "rm -r / -f / wildcard"),
    (r"\b" + _RM_ALIASES + r"\b[^|;&\n]*\s-(?:r|rec|recu|recur|recurs|recurse|fo|forc|force)\w*\b",
     "recursive / forced delete"),
    (r"\b(?:rd|rmdir)\b[^|;&\n]*\s/s\b|\b(?:del|erase)\b[^|;&\n]*\s/[sq]\b", "rd /s, del /s"),
    (r"\|\s*(?:remove-item|ri|del|rm)\b", "piped delete"),
    (r"\bfind\b[^|;&\n]*\s-(?:delete|exec\s+rm)\b", "find -delete"),
    (r"\b(?:rmtree|rmsync|unlinksync|rimraf|shutil\.rmtree|os\.remove|fs\.rm)\b", "scripted delete"),
    (r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;\n]*\|\s*(?:sudo\s+)?"
     r"(?:sh|bash|zsh|python\d?|node|pwsh|powershell|iex|invoke-expression)\b", "pipe download to a shell"),
    (r"<\(\s*(?:curl|wget)\b", "run a downloaded script"),
    (r"(?:curl|wget)\b[^;\n]*(?:&&|;)\s*(?:sudo\s+)?(?:ba|z)?sh\b", "download then run"),
    (r"\biex\b|\binvoke-expression\b|downloadstring\(|\bnet\.webclient\b", "invoke-expression"),
    (r"\s-e(?:c|nc|nco|ncod|ncode|ncoded|ncodedcommand)\b|\s-encodedcommand\b", "encoded PowerShell"),
    (r"\b(?:format-volume|stop-computer|restart-computer|shutdown|bcdedit|vssadmin|diskpart|mkfs)\b|"
     r"\bformat\s+[a-z]:", "power / disk / boot"),
    (r"\breg\s+(?:delete|add|import)\b|\bremove-itemproperty\b|\bset-itemproperty\b.*hk(?:lm|cu)", "registry"),
    (r"\bset-executionpolicy\b", "Set-ExecutionPolicy"),
    (r"\b(?:taskkill|stop-process|kill|pkill|killall)\b", "kill processes"),
    (r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\buv\s+publish\b|\bgh\s+release\s+create\b|"
     r"\bgh\s+repo\s+(?:delete|edit)\b", "publish / repo settings"),
    (r"\bdrop\s+(?:table|database)\b|\btruncate\s+table\b", "SQL drop"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b|\bdd\s+if=", "recursive perms / dd"),
    (r"\bsudo\b|\brunas\b|\bstart-process\b.*-verb\s+runas", "elevation"),
]
_PATH_DANGER = [
    (r"(?:^|[\\/\s'\"])\.env(?:\.|$|[\s:'\"])", ".env"),
    (r"(?:^|[\\/])\.ssh[\\/]", ".ssh"),
    (r"\.(?:pem|key|pfx|p12)(?:$|[\s'\":])", "key file"),
    (r"(?:^|[\\/])\.git[\\/](?:hooks|config)", ".git hooks / config"),
    (r"(?:^|[\\/])\.github[\\/]workflows[\\/]", "CI workflow"),
    (r"(?:^|[\\/])\.claude[\\/]settings[^\\/]*\.json|(?:^|[\\/])\.claude\.json", "Claude settings (could remove this gate)"),
    (r"(?:^|[\\/])\.mcp\.json", "MCP config"),
    (r"(?:^|[\\/])\.codex[\\/]", "Codex config"),
    (r"cordis\.patch\.yml", "DSH profile"),
    (r"midi-agent-console[\\/](?:token|audit\.jsonl)", "this console's token / audit log"),
]
# tool names across harnesses: Claude Code (Bash/Write/Edit...), DSH (pwsh/bash/write/edit/
# str_replace_editor), Codex (Bash, shell with argv lists, apply_patch whose command is the patch)
_SHELL_TOOLS = {"Bash", "PowerShell", "bash", "pwsh", "powershell", "shell", "local_shell"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit", "str_replace_editor"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
# this console's own MCP tools that move hardware or change who answers the keyboard
_HW_TOOLS = ("bind_control", "teleop_start", "reset_emergency_stop")


def normalize(command: Any) -> str:
    """argv list -> string; drop quotes, escaping backslashes and repeated spaces."""
    if isinstance(command, (list, tuple)):
        command = shlex.join(str(c) for c in command)
    s = str(command or "")
    s = re.sub(r"[\"'`]", "", s)
    s = re.sub(r"\\(?=[A-Za-z])", "", s)  # r\m -> rm (keeps C:\path separators before digits/dots)
    s = s.replace("^", "")  # cmd.exe escape
    return re.sub(r"\s+", " ", s).strip()


def _dangerous_path(path: str) -> str | None:
    p = path.strip().rstrip(". ")  # Windows ignores trailing dots / spaces: "settings.json."
    p = re.sub(r"::\$\w+$", "", p)  # NTFS alternate stream: ".env::$DATA"
    for pat, why in _PATH_DANGER:
        if re.search(pat, p, re.IGNORECASE):
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
        cmd = normalize(raw)
        for pat, why in _SHELL_DANGER:
            if re.search(pat, cmd, re.IGNORECASE):
                return "dangerous", why
        why = _dangerous_path_in_text(cmd)
        if why:
            return "dangerous", f"touches {why}"
    if tool_name in _WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
        if not path:
            return "dangerous", "target file unknown"
        why = _dangerous_path(path)
        if why:
            return "dangerous", why
    return "normal", ""


def _dangerous_path_in_text(cmd: str) -> str | None:
    for token in re.split(r"[\s>|;&(),=]+", cmd):
        if token:
            why = _dangerous_path(token)
            if why:
                return why
    return None


def summarize(tool_name: str, tool_input: dict[str, Any], limit: int = 160) -> str:
    tool_input = tool_input or {}
    for key in ("command", "file_path", "notebook_path", "path", "url", "pattern", "query", "prompt", "template"):
        if tool_input.get(key):
            v = tool_input[key]
            text = " ".join((shlex.join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)).split())
            return f"{tool_name}: {text[:limit]}" + ("…" if len(text) > limit else "")
    return f"{tool_name} (no arguments shown)"
