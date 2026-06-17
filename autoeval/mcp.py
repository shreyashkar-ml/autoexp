import json
import sys

from .project import get_run, project_root, read_json, report_instruction as read_report_instruction, script_manifest, set_report_instruction as set_project_report_instruction, write_report_instruction as write_project_report_instruction
from .runtime import (
    diff_runs,
    list_runs,
    read_logs,
    read_output_files,
    read_report_bundle,
    read_script_params,
    restore,
    run_autoeval,
    run_report,
    run_source,
    save_script_file,
    storage,
    workspace,
    write_script_params,
)


PROTOCOL_VERSION = "2025-06-18"


def json_text(data):
    return json.dumps(data, indent=2)


def text_result(data):
    return {
        "content": [{"type": "text", "text": json_text(data)}],
        "structuredContent": data,
    }


def text_prompt(text):
    return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def tool_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS = {
    "workspace": {"description": "Read Autoeval workspace metadata.", "schema": tool_schema()},
    "contract": {"description": "Read the Autoeval workspace contract.", "schema": tool_schema()},
    "script_manifest": {"description": "Read script/stage.json.", "schema": tool_schema()},
    "script_params": {"description": "Read script params and schema.", "schema": tool_schema()},
    "list_runs": {"description": "List recent Autoeval runs.", "schema": tool_schema({"limit": {"type": "integer", "default": 20}})},
    "read_run": {"description": "Read a run metadata row.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "read_run_source": {"description": "Read copied source files for a run.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "read_output_files": {"description": "Read output artifacts for a run.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "read_final_report": {"description": "Read a generated report if present.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "read_report_bundle": {"description": "Read report_bundle.json for a run.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "read_logs": {"description": "Read run logs.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
    "report_instruction": {"description": "Read active report instruction.", "schema": tool_schema()},
    "write_report_instruction": {"description": "Write active project report instruction text.", "schema": tool_schema({"text": {"type": "string"}}, ["text"])},
    "write_script_file": {
        "description": "Create a versioned run snapshot with edited script content.",
        "schema": tool_schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "run_id": {"type": "string"},
                "save_as": {"type": "string"},
            },
            ["path", "content"],
        ),
    },
    "write_script_params": {"description": "Write script/params.json.", "schema": tool_schema({"params": {"type": "object"}}, ["params"])},
    "set_report_instruction": {"description": "Configure project report instruction file.", "schema": tool_schema({"path": {"type": "string"}}, ["path"])},
    "storage": {
        "description": "Promote source or a run into Autoeval storage.",
        "schema": tool_schema({"run_id": {"type": "string"}, "label": {"type": "string"}, "message": {"type": "string"}}),
    },
    "run": {"description": "Run Autoeval through the same runtime path as the CLI.", "schema": tool_schema({"run_id": {"type": "string"}})},
    "diff_runs": {"description": "Diff source/config between two runs.", "schema": tool_schema({"run_a": {"type": "string"}, "run_b": {"type": "string"}}, ["run_a", "run_b"])},
    "restore_run": {"description": "Restore script/config from a run.", "schema": tool_schema({"run_id": {"type": "string"}}, ["run_id"])},
}


PROMPTS = {
    "autoeval/improve-script": "Read autoeval.md, inspect recent runs and the selected run source, improve script behavior, save a versioned script snapshot, run Autoeval, then summarize run_id, status, output files, logs, and report path.",
    "autoeval/debug-failed-run": "Read autoeval.md, inspect the failed run metadata, logs, source, and report bundle. Identify the likely failure cause and propose or create a versioned script fix.",
    "autoeval/write-report": "Read autoeval.md and the active project report instruction. Use runs/{run_id}/report/report_bundle.json to write report artifacts under that run's report directory.",
}


def resource_list():
    return [
        {"uri": "autoeval://workspace", "name": "workspace", "mimeType": "application/json"},
        {"uri": "autoeval://contract", "name": "contract", "mimeType": "text/markdown"},
        {"uri": "autoeval://config", "name": "config", "mimeType": "application/json"},
        {"uri": "autoeval://script/manifest", "name": "script manifest", "mimeType": "application/json"},
        {"uri": "autoeval://script/params", "name": "script params", "mimeType": "application/json"},
        {"uri": "autoeval://runs/latest", "name": "latest runs", "mimeType": "application/json"},
        {"uri": "autoeval://report-instruction", "name": "report instruction", "mimeType": "application/json"},
    ]


def read_resource(uri):
    root = project_root()
    if uri == "autoeval://workspace":
        return "application/json", json_text(workspace(root))
    if uri == "autoeval://contract":
        return "text/markdown", (root / "autoeval.md").read_text()
    if uri == "autoeval://config":
        return "application/json", json_text(read_json(root / "autoeval.json"))
    if uri == "autoeval://script/manifest":
        return "application/json", json_text(script_manifest(root))
    if uri == "autoeval://script/params":
        return "application/json", json_text(read_script_params(root))
    if uri == "autoeval://runs/latest":
        return "application/json", json_text({"runs": list_runs(root=root)})
    if uri == "autoeval://report-instruction":
        return "application/json", json_text(read_report_instruction(root))

    prefix = "autoeval://runs/"
    if uri.startswith(prefix):
        parts = uri[len(prefix):].split("/")
        run_id = parts[0]
        section = parts[1] if len(parts) > 1 else ""
        if not section:
            return "application/json", json_text(get_run(run_id, root))
        if section == "source":
            return "application/json", json_text(run_source(run_id, root))
        if section == "output":
            return "application/json", json_text(read_output_files(run_id, root))
        if section == "report":
            return "application/json", json_text(run_report(run_id, root))
        if section == "logs":
            return "application/json", json_text(read_logs(run_id, root))

    raise ValueError(f"unknown resource: {uri}")


def call_tool(name, args):
    args = args or {}
    root = project_root()
    if name == "workspace":
        return workspace(root)
    if name == "contract":
        return {"text": (root / "autoeval.md").read_text()}
    if name == "script_manifest":
        return script_manifest(root)
    if name == "script_params":
        return read_script_params(root)
    if name == "list_runs":
        return {"runs": list_runs(int(args.get("limit", 20)), root)}
    if name == "read_run":
        return get_run(args["run_id"], root)
    if name == "read_run_source":
        return run_source(args["run_id"], root)
    if name == "read_output_files":
        return read_output_files(args["run_id"], root)
    if name == "read_final_report":
        return run_report(args["run_id"], root)
    if name == "read_report_bundle":
        return read_report_bundle(args["run_id"], root)
    if name == "read_logs":
        return read_logs(args["run_id"], root)
    if name == "report_instruction":
        return read_report_instruction(root)
    if name == "write_report_instruction":
        return write_project_report_instruction(args["text"], root)
    if name == "write_script_file":
        return save_script_file(args["path"], args["content"], root=root, source_run_id=args.get("run_id"), save_as=args.get("save_as"))
    if name == "write_script_params":
        return write_script_params(args["params"], root)
    if name == "set_report_instruction":
        return {"path": set_project_report_instruction(args["path"], root)}
    if name == "storage":
        return storage(args.get("run_id"), args.get("label"), args.get("message"), root)
    if name == "run":
        return run_autoeval(args.get("run_id"), root)
    if name == "diff_runs":
        return {"diff": diff_runs(args["run_a"], args["run_b"], root)}
    if name == "restore_run":
        return restore(args["run_id"], root)
    raise ValueError(f"unknown tool: {name}")


def handle(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "autoeval", "version": "0.1.0"},
            "capabilities": {"resources": {}, "tools": {}, "prompts": {}},
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return {}
    if method == "resources/list":
        return {"resources": resource_list()}
    if method == "resources/read":
        uri = params["uri"]
        mime_type, text = read_resource(uri)
        return {"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": spec["description"], "inputSchema": spec["schema"]} for name, spec in TOOLS.items()]}
    if method == "tools/call":
        return text_result(call_tool(params["name"], params.get("arguments") or {}))
    if method == "prompts/list":
        return {"prompts": [{"name": name, "description": text} for name, text in PROMPTS.items()]}
    if method == "prompts/get":
        name = params["name"]
        if name not in PROMPTS:
            raise ValueError(f"unknown prompt: {name}")
        return text_prompt(PROMPTS[name])
    raise ValueError(f"unsupported method: {method}")


def write_message(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if "id" not in message:
                handle(message)
                continue
            result = handle(message)
            if result is not None:
                write_message({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(exc)}}
            if "message" in locals() and isinstance(message, dict) and "id" in message:
                response["id"] = message["id"]
            write_message(response)


def main():
    serve()


if __name__ == "__main__":
    main()
