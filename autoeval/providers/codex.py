import json
import shutil
import subprocess
import threading
from typing import Any

from ..config import RepoPaths, utc_now_iso, write_json
from ..provider_surface import (
    NormalizedProviderEvent,
    ProviderAdapter,
    ProviderExecutionResult,
    ProviderLaunchRequest,
    ProviderSessionEnvelope,
    provider_last_message_file,
    provider_normalized_trace_file,
    provider_prompt_file,
    provider_raw_trace_file,
    provider_result_file,
)


class CodexProviderAdapter(ProviderAdapter):
    name = "codex"
    proto_doc_url = "https://tessl.io/registry/tessl/npm-openai--codex/0.39.0/files/docs/protocol-mode.md"
    pinned_proto_command = ["npx", "-y", "@openai/codex@0.39.0", "proto"]

    def detect_capabilities(self) -> dict[str, Any]:
        binary = shutil.which("codex")
        exec_help = self._safe_help(["codex", "exec", "--help"]) if binary else {"ok": False, "stdout": "", "stderr": ""}
        local_proto_help = self._safe_help(["codex", "proto", "--help"]) if binary else {"ok": False, "stdout": "", "stderr": ""}

        supports_exec_json = bool(binary) and exec_help["ok"] and "--json" in exec_help["stdout"]
        supports_local_proto = bool(binary) and local_proto_help["ok"] and "protocol" in local_proto_help["stdout"].lower()
        pinned_proto_help = {"ok": False, "stdout": "", "stderr": ""}
        if not supports_local_proto and not supports_exec_json:
            pinned_proto_help = self._safe_help(self.pinned_proto_command + ["--help"])
        supports_pinned_proto = pinned_proto_help["ok"] and "protocol" in pinned_proto_help["stdout"].lower()

        if supports_local_proto:
            transport = "proto"
            command = ["codex", "proto"]
        elif supports_pinned_proto:
            transport = "proto"
            command = list(self.pinned_proto_command)
        elif supports_exec_json:
            transport = "exec_json"
            command = ["codex", "exec"]
        else:
            transport = "unknown"
            command = []

        return {
            "available": supports_local_proto or supports_pinned_proto or supports_exec_json,
            "provider": self.name,
            "binary": binary,
            "transport": transport,
            "supports_local_proto": supports_local_proto,
            "supports_pinned_proto": supports_pinned_proto,
            "supports_exec_json": supports_exec_json,
            "command": command,
            "proto_doc_url": self.proto_doc_url,
        }

    def launch(
        self,
        *,
        paths: RepoPaths,
        session: ProviderSessionEnvelope,
        request: ProviderLaunchRequest,
    ) -> ProviderExecutionResult:
        capabilities = self.detect_capabilities()
        if not capabilities.get("available", False):
            result = ProviderExecutionResult(
                ok=False,
                provider=self.name,
                transport="unavailable",
                command=[],
                session_file=request.session_file,
                prompt_file=str(provider_prompt_file(paths, request.run_id, self.name)),
                raw_trace_file=str(provider_raw_trace_file(paths, request.run_id, self.name)),
                normalized_trace_file=str(provider_normalized_trace_file(paths, request.run_id, self.name)),
                last_message_file=str(provider_last_message_file(paths, request.run_id, self.name)),
                error=str(capabilities.get("reason", "provider unavailable")),
                metadata={"capabilities": capabilities},
            )
            write_json(provider_result_file(paths, request.run_id, self.name), result.model_dump())
            return result

        transport = str(capabilities.get("transport", "exec_json"))
        prompt_text = self._build_prompt(session, session_file=request.session_file)
        prompt_file = provider_prompt_file(paths, request.run_id, self.name)
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt_text, encoding="utf-8")

        if transport == "proto":
            command = self._build_proto_command(capabilities=capabilities, request=request)
            process_input = self._build_proto_submission(request.run_id, prompt_text)
        else:
            command = self._build_exec_json_command(paths, request)
            process_input = prompt_text

        raw_trace = provider_raw_trace_file(paths, request.run_id, self.name)
        exit_code, raw_output, timed_out = self._run_command(
            command=command,
            cwd=str(paths.repo),
            process_input=process_input,
            timeout_sec=request.timeout_sec,
            raw_trace=raw_trace,
        )

        normalized_events = self._normalize_output(raw_output, transport=transport)
        completed_event_seen = any(
            item.normalized_type in {"provider.turn_completed", "provider.final_response"} for item in normalized_events
        )
        normalized_trace = provider_normalized_trace_file(paths, request.run_id, self.name)
        normalized_trace.write_text(
            "\n".join(json.dumps(item.model_dump(), sort_keys=True) for item in normalized_events) + ("\n" if normalized_events else ""),
            encoding="utf-8",
        )

        last_message = provider_last_message_file(paths, request.run_id, self.name)
        final_output = self._extract_final_output(normalized_events)
        last_message.write_text(final_output, encoding="utf-8")
        error = None
        if timed_out and not completed_event_seen:
            error = f"codex provider launch timed out after {request.timeout_sec}s"
        elif not timed_out and exit_code != 0:
            error = self._extract_error(normalized_events) or f"codex exited with code {exit_code}"

        result = ProviderExecutionResult(
            ok=(exit_code == 0 and not timed_out) or (timed_out and completed_event_seen),
            provider=self.name,
            transport=transport,
            command=command,
            session_file=request.session_file,
            prompt_file=str(prompt_file),
            raw_trace_file=str(raw_trace),
            normalized_trace_file=str(normalized_trace),
            last_message_file=str(last_message),
            exit_code=exit_code,
            final_output=final_output,
            error=error,
            event_count=len(normalized_events),
            metadata={
                "capabilities": capabilities,
                "completed_at": utc_now_iso(),
                "protocol_doc": self.proto_doc_url,
                "timed_out": timed_out,
                "completed_event_seen": completed_event_seen,
                "timeout_sec": request.timeout_sec,
            },
        )
        write_json(provider_result_file(paths, request.run_id, self.name), result.model_dump())
        return result

    def _run_command(
        self,
        *,
        command: list[str],
        cwd: str,
        process_input: str,
        timeout_sec: int | None,
        raw_trace,
    ) -> tuple[int | None, str, bool]:
        output_chunks: list[str] = []
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _drain_stdout() -> None:
            if process.stdout is None:
                raw_trace.write_text("", encoding="utf-8")
                return
            with raw_trace.open("w", encoding="utf-8") as handle:
                for chunk in process.stdout:
                    output_chunks.append(chunk)
                    handle.write(chunk)
                    handle.flush()

        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()

        try:
            if process.stdin is not None:
                process.stdin.write(process_input)
                process.stdin.close()
        except BrokenPipeError:
            pass

        timed_out = False
        try:
            if timeout_sec is None:
                process.wait()
            else:
                process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()

        reader.join(timeout=5)
        if process.stdout is not None:
            remaining = process.stdout.read() or ""
            if remaining:
                output_chunks.append(remaining)
                with raw_trace.open("a", encoding="utf-8") as handle:
                    handle.write(remaining)

        exit_code = None if timed_out else process.returncode
        return exit_code, "".join(output_chunks), timed_out

    def _safe_help(self, command: list[str]) -> dict[str, Any]:
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "stdout": "", "stderr": str(exc)}
        return {"ok": result.returncode == 0, "stdout": result.stdout or "", "stderr": result.stderr or ""}

    def _coerce_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _build_prompt(self, session: ProviderSessionEnvelope, *, session_file: str) -> str:
        task = session.run.task
        artifact_targets = ", ".join(
            f"{item.target_file} ({item.artifact_state})" for item in session.artifact_generation
        )
        lines = [
            "You are running through the autoeval provider connector.",
            f"Repository root: {session.run.repo_root}",
            f"Task: {task}",
            f"Execution mode: {session.run.mode}",
            f"Authoritative provider session file: {session_file}",
            f"Authoritative active context file: {session.active_context.file}",
            f"Tool catalog file: {session.tool_catalog.file}",
            f"Artifact instruction targets: {artifact_targets}",
            "",
            "Required behavior:",
            "- Read the provider session and active context files before acting.",
            "- Read artifact_generation entries from provider_session.json and use their template instructions when creating or updating research/implementation/plan/review/feature_list artifacts.",
            "- If an artifact target is missing or empty, author the real artifact content in that file before proceeding with other work.",
            "- Never create or modify verifier.yaml; it is user-owned and lives at the repository root outside .autoeval.",
            "- Use the autoeval CLI tool surface for harness actions and status transitions.",
            "- Follow the loop steps from the provider session contract.",
            "- Do not edit harness-owned immutable task metadata directly.",
            "",
            "Tool execution path:",
            "- Use `autoeval tools ...` commands for harness interactions.",
            "- Use `autoeval verifier sync --repo .` when you need linked pytest targets.",
            "- Use `autoeval tools guardrail-check` before terminal commands when required by the contract.",
        ]
        return "\n".join(lines).strip() + "\n"

    def _build_proto_submission(self, run_id: str, prompt_text: str) -> str:
        payload = {
            "id": f"{run_id}-initial",
            "op": {
                "type": "user_input",
                "items": [
                    {
                        "type": "text",
                        "text": prompt_text,
                        "text_elements": [],
                    }
                ],
            },
        }
        return json.dumps(payload) + "\n"

    def _build_exec_json_command(self, paths: RepoPaths, request: ProviderLaunchRequest) -> list[str]:
        last_message = str(provider_last_message_file(paths, request.run_id, self.name))
        command = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            str(paths.repo),
            "--sandbox",
            request.sandbox_mode,
            "--output-last-message",
            last_message,
            "-",
        ]
        if request.config_profile:
            command[2:2] = ["-p", request.config_profile]
        if request.model:
            command[2:2] = ["-m", request.model]
        if request.extra_args:
            command[-1:-1] = list(request.extra_args)
        return command

    def _build_proto_command(self, *, capabilities: dict[str, Any], request: ProviderLaunchRequest) -> list[str]:
        command = list(capabilities.get("command", self.pinned_proto_command))
        command.extend(["-c", f'sandbox_mode="{request.sandbox_mode}"'])
        command.extend(["-c", 'model_reasoning_effort="high"'])
        if request.model:
            command.extend(["-c", f'model="{request.model}"'])
        if request.extra_args:
            command.extend(request.extra_args)
        return command

    def _normalize_output(self, raw_output: str, *, transport: str) -> list[NormalizedProviderEvent]:
        events: list[NormalizedProviderEvent] = []
        for index, raw_line in enumerate(raw_output.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                events.append(
                    NormalizedProviderEvent(
                        sequence=index,
                        provider=self.name,
                        transport=transport,
                        raw_type="log",
                        normalized_type="provider.log",
                        message=line,
                    )
                )
                continue

            if transport == "proto" and isinstance(payload.get("msg"), dict):
                raw_type = str(payload["msg"].get("type", "unknown"))
            else:
                raw_type = str(payload.get("type", "unknown"))
            events.append(
                NormalizedProviderEvent(
                    sequence=index,
                    provider=self.name,
                    transport=transport,
                    raw_type=raw_type,
                    normalized_type=self._normalize_type(raw_type, payload),
                    message=self._extract_message(payload),
                    payload=payload,
                )
            )
        return events

    def _normalize_type(self, raw_type: str, payload: dict[str, Any]) -> str:
        if raw_type in {"SessionConfigured", "session_configured"}:
            return "provider.session_configured"
        if raw_type in {"FinalResponse", "final_response"}:
            return "provider.final_response"
        if raw_type in {"StreamingResponse", "streaming_response"}:
            return "provider.streaming_response"
        if raw_type == "agent_message":
            return "provider.agent_message"
        if raw_type == "agent_message_delta":
            return "provider.agent_message_delta"
        if raw_type == "agent_reasoning":
            return "provider.agent_reasoning"
        if raw_type == "agent_reasoning_delta":
            return "provider.agent_reasoning_delta"
        if raw_type == "exec_command_begin":
            return "provider.exec_command_begin"
        if raw_type == "exec_command_output_delta":
            return "provider.exec_command_output_delta"
        if raw_type == "exec_command_end":
            return "provider.exec_command_end"
        if raw_type == "request_user_input":
            return "provider.request_user_input"
        if raw_type == "dynamic_tool_call_request":
            return "provider.tool_call_request"
        if raw_type == "dynamic_tool_call_response":
            return "provider.tool_call_response"
        if raw_type == "warning":
            return "provider.warning"
        if raw_type == "thread.started":
            return "provider.session_started"
        if raw_type in {"turn.started", "task_started", "turn_started"}:
            return "provider.turn_started"
        if raw_type in {"turn.completed", "task_complete", "turn_complete"}:
            return "provider.turn_completed"
        if raw_type == "error":
            return "provider.error"
        if raw_type == "item.completed":
            item_type = str(payload.get("item", {}).get("type", ""))
            if item_type == "message":
                return "provider.message_completed"
            if item_type == "error":
                return "provider.error"
            return "provider.item_completed"
        if raw_type.endswith(".delta"):
            return "provider.message_delta"
        return "provider.raw_event"

    def _extract_message(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("msg"), dict):
            msg = payload["msg"]
            if isinstance(msg.get("data"), dict):
                data = msg["data"]
                for key in ("text", "message", "content", "delta", "last_agent_message"):
                    if isinstance(data.get(key), str):
                        return str(data[key])
            for key in ("message", "text", "content", "delta", "last_agent_message"):
                if isinstance(msg.get(key), str):
                    return str(msg[key])
            if isinstance(msg.get("message"), str):
                return str(msg["message"])
        for key in ("message", "text", "content", "delta", "last_agent_message"):
            if isinstance(payload.get(key), str):
                return str(payload[key])
        item = payload.get("item")
        if isinstance(item, dict):
            if isinstance(item.get("message"), str):
                return str(item["message"])
            if isinstance(item.get("text"), str):
                return str(item["text"])
        return ""

    def _extract_error(self, events: list[NormalizedProviderEvent]) -> str | None:
        for item in reversed(events):
            if item.normalized_type == "provider.error" and item.message:
                return item.message
        return None

    def _extract_final_output(self, events: list[NormalizedProviderEvent]) -> str:
        for item in reversed(events):
            if item.message:
                return item.message
        return ""
