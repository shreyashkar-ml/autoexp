from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import shlex
from typing import Any, NamedTuple


class ValidationResult(NamedTuple):
    allowed: bool
    reason: str = ""


ALLOWED_COMMANDS: set[str] = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "cp",
    "mv",
    "mkdir",
    "rm",
    "touch",
    "chmod",
    "unzip",
    "pwd",
    "cd",
    "echo",
    "printf",
    "curl",
    "which",
    "env",
    "python",
    "python3",
    "npm",
    "npx",
    "node",
    "git",
    "ps",
    "lsof",
    "sleep",
    "pkill",
    "init.sh",
    "pytest",
}

COMMANDS_NEEDING_EXTRA_VALIDATION: set[str] = {"pkill", "chmod", "init.sh", "rm"}
PYTEST_NODE_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ALLOWED_TIMEOUT_SEC = 1800


def split_command_segments(command_string: str) -> list[str]:
    segments = re.split(r"\s*(?:&&|\|\|)\s*", command_string)
    result: list[str] = []
    for segment in segments:
        sub_segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', segment)
        for sub in sub_segments:
            sub = sub.strip()
            if sub:
                result.append(sub)
    return result


def extract_commands(command_string: str) -> list[str]:
    commands: list[str] = []
    segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', command_string)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
        except ValueError:
            return []

        if not tokens:
            continue

        expect_command = True
        for token in tokens:
            if token in ("|", "||", "&&", "&"):
                expect_command = True
                continue

            if token in {
                "if",
                "then",
                "else",
                "elif",
                "fi",
                "for",
                "while",
                "until",
                "do",
                "done",
                "case",
                "esac",
                "in",
                "!",
                "{",
                "}",
            }:
                continue

            if token.startswith("-"):
                continue

            if "=" in token and not token.startswith("="):
                continue

            if expect_command:
                commands.append(os.path.basename(token))
                expect_command = False

    return commands


def validate_pkill_command(command_string: str) -> ValidationResult:
    allowed_process_names = {"node", "npm", "npx", "vite", "next", "pytest"}

    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return ValidationResult(False, "Could not parse pkill command")

    if not tokens:
        return ValidationResult(False, "Empty pkill command")

    args: list[str] = []
    for token in tokens[1:]:
        if not token.startswith("-"):
            args.append(token)
    if not args:
        return ValidationResult(False, "pkill requires a process name")

    target = args[-1]
    if " " in target:
        target = target.split()[0]

    if target in allowed_process_names:
        return ValidationResult(True)
    return ValidationResult(False, f"pkill only allowed for dev processes: {allowed_process_names}")


def validate_chmod_command(command_string: str) -> ValidationResult:
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return ValidationResult(False, "Could not parse chmod command")

    if not tokens or tokens[0] != "chmod":
        return ValidationResult(False, "Not a chmod command")

    mode: str | None = None
    files: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            return ValidationResult(False, "chmod flags are not allowed")
        if mode is None:
            mode = token
        else:
            files.append(token)

    if mode is None:
        return ValidationResult(False, "chmod requires a mode")
    if not files:
        return ValidationResult(False, "chmod requires at least one file")
    if not re.match(r"^[ugoa]*\+x$", mode):
        return ValidationResult(False, f"chmod only allowed with +x mode, got: {mode}")
    return ValidationResult(True)


def validate_init_script(command_string: str) -> ValidationResult:
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return ValidationResult(False, "Could not parse init script command")

    if not tokens:
        return ValidationResult(False, "Empty command")

    script = tokens[0]
    if script == "./init.sh" or script.endswith("/init.sh"):
        return ValidationResult(True)
    return ValidationResult(False, f"Only ./init.sh is allowed, got: {script}")


def validate_rm_command(command_string: str) -> ValidationResult:
    dangerous_paths = {
        "/",
        "/etc",
        "/usr",
        "/var",
        "/bin",
        "/sbin",
        "/lib",
        "/opt",
        "/boot",
        "/root",
        "/home",
        "/Users",
        "/System",
        "/Library",
        "/Applications",
        "/private",
        "~",
    }

    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return ValidationResult(False, "Could not parse rm command")

    if not tokens or tokens[0] != "rm":
        return ValidationResult(False, "Not an rm command")

    paths: list[str] = []
    for token in tokens[1:]:
        if not token.startswith("-"):
            paths.append(token)

    if not paths:
        return ValidationResult(False, "rm requires at least one path")

    for path in paths:
        normalized = path.rstrip("/") or "/"
        if normalized in dangerous_paths:
            return ValidationResult(False, f"rm on system directory '{path}' is not allowed")

        for dangerous in dangerous_paths:
            if dangerous == "/":
                continue
            if normalized == dangerous or (
                normalized.startswith(dangerous + "/") and normalized.count("/") <= dangerous.count("/") + 1
            ):
                return ValidationResult(False, f"rm too close to system directory '{dangerous}' is not allowed")

        if path == "/*" or path.startswith("/*"):
            return ValidationResult(False, "rm on root wildcard is not allowed")

    return ValidationResult(True)


def get_command_for_validation(cmd: str, segments: list[str]) -> str:
    for segment in segments:
        if cmd in extract_commands(segment):
            return segment
    return ""


def validate_command(command: str) -> ValidationResult:
    commands = extract_commands(command)
    if not commands:
        return ValidationResult(False, f"Could not parse command for security validation: {command}")

    segments = split_command_segments(command)
    for cmd in commands:
        if cmd not in ALLOWED_COMMANDS:
            return ValidationResult(False, f"Command '{cmd}' is not in the allowed commands list")

        if cmd in COMMANDS_NEEDING_EXTRA_VALIDATION:
            cmd_segment = get_command_for_validation(cmd, segments) or command
            if cmd == "pkill":
                result = validate_pkill_command(cmd_segment)
            elif cmd == "chmod":
                result = validate_chmod_command(cmd_segment)
            elif cmd == "init.sh":
                result = validate_init_script(cmd_segment)
            else:
                result = validate_rm_command(cmd_segment)
            if not result.allowed:
                return result

    return ValidationResult(True)


def validate_repo_relative_path(path_value: str) -> ValidationResult:
    normalized = path_value.replace("\\", "/").strip()
    if not normalized:
        return ValidationResult(False, "path is empty")
    if "\x00" in normalized:
        return ValidationResult(False, "path contains null byte")
    if normalized.startswith("/"):
        return ValidationResult(False, "absolute paths are not allowed")

    pure = PurePosixPath(normalized)
    if any(part in {"..", ""} for part in pure.parts):
        return ValidationResult(False, "path traversal is not allowed")

    return ValidationResult(True)


def validate_pytest_target(target: str) -> ValidationResult:
    value = target.strip()
    if not value:
        return ValidationResult(False, "pytest target is empty")

    chunks = value.split("::")
    path_part = chunks[0]
    path_check = validate_repo_relative_path(path_part)
    if not path_check.allowed:
        return ValidationResult(False, f"invalid pytest target path: {path_check.reason}")

    if len(chunks) > 3:
        return ValidationResult(False, "pytest target has too many node segments")

    for segment in chunks[1:]:
        if not PYTEST_NODE_SEGMENT_RE.match(segment):
            return ValidationResult(False, f"invalid pytest node segment: {segment}")

    return ValidationResult(True)


def validate_timeout(timeout_sec: int, *, min_timeout: int = 1, max_timeout: int = MAX_ALLOWED_TIMEOUT_SEC) -> ValidationResult:
    if timeout_sec < min_timeout:
        return ValidationResult(False, f"timeout_sec must be >= {min_timeout}")
    if timeout_sec > max_timeout:
        return ValidationResult(False, f"timeout_sec must be <= {max_timeout}")
    return ValidationResult(True)


def guardrail_summary() -> dict[str, Any]:
    return {
        "allowed_commands": sorted(ALLOWED_COMMANDS),
        "extra_validation_commands": sorted(COMMANDS_NEEDING_EXTRA_VALIDATION),
        "max_allowed_timeout_sec": MAX_ALLOWED_TIMEOUT_SEC,
        "notes": [
            "policy.py gates command execution based on security.py decisions",
            "autocheck uses linked pytest targets from verifier.yaml and feature criteria",
        ],
    }


def bash_security_hook(input_data: dict[str, Any]) -> dict[str, str]:
    if input_data.get("tool_name") != "Bash":
        return {}

    command = str(input_data.get("tool_input", {}).get("command", ""))
    if not command:
        return {}

    result = validate_command(command)
    if result.allowed:
        return {}
    return {"decision": "block", "reason": result.reason}
