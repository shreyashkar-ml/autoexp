import json
import sys

from .autoresearch import for_project as research_for_project
from .reports import (
    report_generation_instruction,
    set_report_instruction as set_project_report_instruction,
    write_report_instruction as write_project_report_instruction,
)
from .runs import get_run
from .runtime import (
    diff_runs,
    list_runs,
    read_logs,
    read_output_files,
    read_report_bundle,
    read_script_params,
    restore,
    run_autoexp,
    run_report,
    run_source,
    save_script_file,
    workspace,
    write_script_params,
)
from .workspace import project_root, read_json, script_manifest


PROTOCOL_VERSION = "2025-06-18"


# ======================================================================
#  JSON-RPC / MCP envelope helpers
# ======================================================================

def json_text(data):
    return json.dumps(data, indent=2)


def text_result(data):
    return {"content": [{"type": "text", "text": json_text(data)}], "structuredContent": data}


def text_prompt(text):
    return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def tool_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _run_id_schema():
    return tool_schema({"run_id": {"type": "string"}}, ["run_id"])


# ======================================================================
#  Tool registry: description, schema, and handler defined together
# ======================================================================
# Each handler takes (root, args) so adding a tool is a single self-contained entry.

TOOLS = {
    "workspace": {
        "description": "Read Autoexp workspace metadata.",
        "schema": tool_schema(),
        "handler": lambda root, a: workspace(root),
    },
    "contract": {
        "description": "Read the Autoexp workspace contract.",
        "schema": tool_schema(),
        "handler": lambda root, a: {"text": (root / "autoexp.md").read_text()},
    },
    "script_manifest": {
        "description": "Read script/stage.json.",
        "schema": tool_schema(),
        "handler": lambda root, a: script_manifest(root),
    },
    "script_params": {
        "description": "Read script params and schema.",
        "schema": tool_schema(),
        "handler": lambda root, a: read_script_params(root),
    },
    "list_runs": {
        "description": "List recent Autoexp runs.",
        "schema": tool_schema({"limit": {"type": "integer", "default": 20}}),
        "handler": lambda root, a: {"runs": list_runs(int(a.get("limit", 20)), root)},
    },
    "read_run": {
        "description": "Read a run metadata row.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: get_run(a["run_id"], root),
    },
    "read_run_source": {
        "description": "Read copied source files for a run.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: run_source(a["run_id"], root),
    },
    "read_output_files": {
        "description": "Read output artifacts for a run.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: read_output_files(a["run_id"], root),
    },
    "read_final_report": {
        "description": "Read a generated report if present.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: run_report(a["run_id"], root),
    },
    "read_report_bundle": {
        "description": "Read report_bundle.json for a run.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: read_report_bundle(a["run_id"], root),
    },
    "read_logs": {
        "description": "Read run logs.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: read_logs(a["run_id"], root),
    },
    "report_instruction": {
        "description": "Read project report guidance joined with Autoexp's report contract.",
        "schema": tool_schema(),
        "handler": lambda root, a: report_generation_instruction(root),
    },
    "write_report_instruction": {
        "description": "Write active project report instruction text.",
        "schema": tool_schema({"text": {"type": "string"}}, ["text"]),
        "handler": lambda root, a: write_project_report_instruction(a["text"], root),
    },
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
        "handler": lambda root, a: save_script_file(
            a["path"], a["content"], root=root, source_run_id=a.get("run_id"), save_as=a.get("save_as")
        ),
    },
    "write_script_params": {
        "description": "Write script/params.json.",
        "schema": tool_schema({"params": {"type": "object"}}, ["params"]),
        "handler": lambda root, a: write_script_params(a["params"], root),
    },
    "set_report_instruction": {
        "description": "Configure project report instruction file.",
        "schema": tool_schema({"path": {"type": "string"}}, ["path"]),
        "handler": lambda root, a: {"path": set_project_report_instruction(a["path"], root)},
    },
    "run": {
        "description": "Run Autoexp through the same runtime path as the CLI.",
        "schema": tool_schema({"run_id": {"type": "string"}}),
        "handler": lambda root, a: run_autoexp(a.get("run_id"), root),
    },
    "diff_runs": {
        "description": "Diff source/config between two runs.",
        "schema": tool_schema({"run_a": {"type": "string"}, "run_b": {"type": "string"}}, ["run_a", "run_b"]),
        "handler": lambda root, a: {"diff": diff_runs(a["run_a"], a["run_b"], root)},
    },
    "restore_run": {
        "description": "Restore script/config from a run.",
        "schema": _run_id_schema(),
        "handler": lambda root, a: restore(a["run_id"], root),
    },
    "research_state": {
        "description": "Read the autoresearch objective, contract, loop, and experiment ledger.",
        "schema": tool_schema(),
        "handler": lambda root, a: research_for_project(root).state(),
    },
    "research_diff": {
        "description": "Read the Git diff for one autoresearch attempt.",
        "schema": tool_schema({"tag": {"type": "string"}}, ["tag"]),
        "handler": lambda root, a: research_for_project(root).diff(a["tag"]),
    },
    "research_begin_attempt": {
        "description": "Record a hypothesis and run one autoresearch experiment.",
        "schema": tool_schema({"hypothesis": {"type": "string"}}, ["hypothesis"]),
        "handler": lambda root, a: research_for_project(root).begin_attempt(a["hypothesis"]),
    },
    "research_finish_attempt": {
        "description": "Score an autoresearch attempt and keep or revert its change.",
        "schema": tool_schema({"tag": {"type": "string"}}, ["tag"]),
        "handler": lambda root, a: research_for_project(root).finish_attempt(a["tag"]),
    },
}


PROMPTS = {
    "autoexp/improve-script": "Read autoexp.md, inspect recent runs and the selected run source, improve script behavior, save a versioned script snapshot, run Autoexp, then summarize run_id, status, output files, logs, and report path.",
    "autoexp/debug-failed-run": "Read autoexp.md, inspect the failed run metadata, logs, source, and report bundle. Identify the likely failure cause and propose or create a versioned script fix.",
    "autoexp/write-report": "Read autoexp.md and the active project report instruction. Use runs/{run_id}/report/report_bundle.json to write report artifacts under that run's report directory.",
}


def call_tool(name, args):
    spec = TOOLS.get(name)
    if not spec:
        raise ValueError(f"unknown tool: {name}")
    return spec["handler"](project_root(), args or {})


# ======================================================================
#  Resources
# ======================================================================

# URI -> (display name, MIME type, producer).
RESOURCES = {
    "autoexp://workspace": ("workspace", "application/json", lambda root: json_text(workspace(root))),
    "autoexp://contract": ("contract", "text/markdown", lambda root: (root / "autoexp.md").read_text()),
    "autoexp://config": ("config", "application/json", lambda root: json_text(read_json(root / "autoexp.json"))),
    "autoexp://script/manifest": ("script manifest", "application/json", lambda root: json_text(script_manifest(root))),
    "autoexp://script/params": ("script params", "application/json", lambda root: json_text(read_script_params(root))),
    "autoexp://runs/latest": ("latest runs", "application/json", lambda root: json_text({"runs": list_runs(root=root)})),
    "autoexp://report-instruction": ("report instruction", "application/json", lambda root: json_text(report_generation_instruction(root))),
    "autoexp://research": ("autoresearch state", "application/json", lambda root: json_text(research_for_project(root).state())),
}


def resource_list():
    return [
        {"uri": uri, "name": name, "mimeType": mime_type}
        for uri, (name, mime_type, _) in RESOURCES.items()
    ]

# Per-run section -> producer(run_id, root). "" means the run row itself.
RUN_SECTIONS = {
    "": lambda run_id, root: get_run(run_id, root),
    "source": lambda run_id, root: run_source(run_id, root),
    "output": lambda run_id, root: read_output_files(run_id, root),
    "report": lambda run_id, root: run_report(run_id, root),
    "logs": lambda run_id, root: read_logs(run_id, root),
}


def read_resource(uri):
    root = project_root()
    if uri in RESOURCES:
        _, mime_type, produce = RESOURCES[uri]
        return mime_type, produce(root)

    prefix = "autoexp://runs/"
    if uri.startswith(prefix):
        parts = uri[len(prefix):].split("/")
        run_id = parts[0]
        section = parts[1] if len(parts) > 1 else ""
        if section in RUN_SECTIONS:
            return "application/json", json_text(RUN_SECTIONS[section](run_id, root))

    raise ValueError(f"unknown resource: {uri}")


# ======================================================================
#  JSON-RPC method dispatch
# ======================================================================

def handle(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "autoexp", "version": "0.1.0"},
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
        return {"tools": [
            {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
            for name, spec in TOOLS.items()
        ]}
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


# ======================================================================
#  Stdio transport
# ======================================================================

def write_message(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        message = None
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
            if isinstance(message, dict) and "id" in message:
                response["id"] = message["id"]
            write_message(response)


def main():
    serve()


if __name__ == "__main__":
    main()
