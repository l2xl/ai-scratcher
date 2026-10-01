# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Synthetic agent library: prompt library, item seed context, AI connections and the tree call protocol of a reply."""

import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from . import syngatelib

CLAUDE_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT = "default"


def load_prompts(prompts_dir=PROMPTS_DIR):
    """The prompt library: name -> text of every markdown file under `prompts_dir`, the name being the file stem."""
    return {path.stem: path.read_text(encoding="utf-8") for path in sorted(prompts_dir.glob("*.md"))}


PROMPTS = load_prompts()
# The reply protocol: the tree changes only through tree calls, and a reply is either text or one
# block of calls. It is the default prompt heading every seed context, and every connection appends
# it to its system prompt as well.
TREE_CALLS = PROMPTS[DEFAULT_PROMPT]
CALL_BLOCK_RE = re.compile(r"```syngate[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
TOOLS = {"add": ("uid", "header", "description"), "edit": ("uid",), "remove": ("uid",), "move": ("uid", "to")}
QUERIES = {"query": ("uid",), "context": ("uid",)}
RUNS = {"test": ("uid",)}
CALLS = {**TOOLS, **QUERIES, **RUNS}
EDITABLE = ("header", "description")
QUERY_ROUNDS = 8
RESULTS_PREAMBLE = "Results of the calls in your last answer:\n"
# A message names an item the way a file is named: `@UID` brings the item's statement into the turn.
REFERENCE_RE = re.compile(r"@([A-Z][A-Z_]*(?:-[A-Z0-9_]+)*)\b")
HISTORY_PREAMBLE = "The exchange so far, as kept by the user:\n"

STRING = {"type": "string"}


def _call_schema(tool, **optional):
    return {"type": "object", "properties": {"tool": {"const": tool}, "uid": STRING, **optional},
            "required": ["tool", *CALLS[tool]], "additionalProperties": False}


# The structured answer: the tools of the Syngate API an answer may carry, as the JSON schema a
# connection hands its model, so the shape of a call is enforced by the model side rather than by the parser alone.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["text", "calls"]},
        "text": STRING,
        "calls": {"type": "array", "minItems": 1, "items": {"anyOf": [
            _call_schema("add", parent=STRING, header=STRING, description=STRING, kind={"enum": ["leaf", "branch"]}),
            _call_schema("edit", header=STRING, description=STRING),
            _call_schema("remove"),
            _call_schema("move", to=STRING, before=STRING, link={"type": "boolean"}, **{"from": STRING}),
            _call_schema("query"),
            _call_schema("context"),
            _call_schema("test", name=STRING),
        ]}},
    },
    "required": ["kind"],
    "additionalProperties": False,
}


def seed_context(items, uid, prompt=DEFAULT_PROMPT):
    """The named prompt of the library, then the descriptions of every ancestor, parents first, then the item's own;
    an item reached through several parents enters once."""
    if uid not in items:
        raise syngatelib.unknown_uid(f"unknown UID '{uid}'")
    return PROMPTS[prompt] + "\n" + "".join(items[selected].description for selected in syngatelib.walk(items, uid))


def referenced(items, text):
    """The statements of the items named `@UID` in `text`, each as one more section after the seed context."""
    found = [uid for uid in dict.fromkeys(REFERENCE_RE.findall(text)) if uid in items]
    return "".join(f"\n@{uid} {items[uid].header}\n{items[uid].description}" for uid in found)


def transcript(history, text):
    """The user's text headed by the kept messages of an exchange that has no session to resume."""
    if not history:
        return text
    lines = [f"{'user' if message.get('role') == 'user' else 'assistant'}: {message.get('text') or ''}" for message in history]
    return HISTORY_PREAMBLE + "\n".join(lines) + "\n\n" + text


def item_view(items, uid):
    """The item as the AI reads it back: its fields, its kind and the UIDs of its children in sibling order."""
    if uid not in items:
        raise syngatelib.unknown_uid(f"unknown UID '{uid}'")
    item = items[uid]
    return {"uid": uid, "header": item.header, "description": item.description, "parents": item.parents,
            "children": syngatelib.sorted_children(items, syngatelib.children_map(items), uid),
            "kind": "branch" if item.tests is None else "leaf"}


def is_query(call):
    return call["tool"] in QUERIES


def is_run(call):
    return call["tool"] in RUNS


def run_report(items, targets, records):
    """The outcome of a run as the AI reads it back: one line per addressed leaf binding -- `targets` being (uid, binding|None),
    None standing for every binding of the leaf -- with pass/fail, a binding without a record having failed, then the captured
    output of every failed routine."""
    lines, logs = [], []
    for uid, name in targets:
        declared = [name] if name else list(items[uid].tests) if uid in items and items[uid].is_leaf else []
        for binding in declared or [found for found_uid, found in records if found_uid == uid] or [None]:
            found = list({record["name"]: record for record in records.get((uid, binding), [])}.values())
            label = f"{uid}:{binding}" if binding else uid
            lines.append(f"- {label}: {'pass' if found and all(record['passed'] for record in found) else 'fail'}")
            logs += [f"{label} output:\n{record['log']}" for record in found if not record["passed"] and record["log"]]
    return "\n".join(lines + logs)


class connection_error(Exception):
    pass


class query_error(ValueError):
    pass


class protocol_error(ValueError):
    pass


def parse_reply(reply):
    """("chat", text) for a plain answer, ("calls", [call, ...]) for one batch of Syngate API calls: a structured answer
    shaped by `ANSWER_SCHEMA`, or text holding exactly one ```syngate block; anything mixed or malformed is refused."""
    if isinstance(reply, dict):
        calls = reply.get("calls")
        if reply.get("kind") == "text" and not calls:
            return "chat", reply.get("text") or ""
        if reply.get("kind") != "calls" or reply.get("text"):
            raise protocol_error("an answer is either text or one block of tree calls, never both")
    else:
        blocks = CALL_BLOCK_RE.findall(reply)
        if not blocks:
            return "chat", reply
        if len(blocks) > 1 or CALL_BLOCK_RE.sub("", reply, count=1).strip():
            raise protocol_error("an answer is either text or one block of tree calls, never both")
        try:
            calls = json.loads(blocks[0])
        except ValueError as exc:
            raise protocol_error(f"tree calls are not valid JSON: {exc}") from None
    if not isinstance(calls, list) or not calls or not all(isinstance(call, dict) for call in calls):
        raise protocol_error("tree calls must be a non-empty JSON list of objects")
    for call in calls:
        tool = call.get("tool")
        if tool not in CALLS:
            raise protocol_error(f"unknown tree call {tool!r}")
        missing = [name for name in CALLS[tool] if not (isinstance(call.get(name), str) and call[name].strip())]
        if missing:
            raise protocol_error(f"{tool}: missing {', '.join(missing)}")
        if tool == "edit" and not any(isinstance(call.get(field), str) and call[field].strip() for field in EDITABLE):
            raise protocol_error(f"edit: missing {' or '.join(EDITABLE)}")
    return "calls", calls


def apply_call(items, anchor, call, syngate_dir=None):
    """Perform one parsed Syngate API call against the loaded tree, `@` standing for the
    anchored item. -> the line reporting what it did, or what a query read; a refusal raises
    `syngatelib.tree_error`.

    No call passes `clear`: a stamp an AI answer moves is deliberately left
    standing over the changed content, so the user meets a violated review
    rather than a silently unreviewed item."""
    at = lambda value: anchor if value == "@" else value
    tool, uid = call["tool"], at(call["uid"])
    if tool in RUNS:
        raise syngatelib.tree_error(f"{tool} runs through the page's runner, not against the tree")
    if tool == "query":
        return f"- {uid}: {json.dumps(item_view(items, uid), ensure_ascii=False)}"
    if tool == "context":
        return f"- context of {uid}:\n{seed_context(items, uid)}"
    if tool == "add":
        parent = at(call.get("parent") or "@")
        family = syngatelib.sorted_children(items, syngatelib.children_map(items), parent) if parent in items else []
        last = items[family[-1]] if family else items.get(parent)
        syngatelib.add_item(items, uid, [parent], header=call["header"], description=call["description"],
                            kind=call.get("kind") or "leaf", folder=last.folder if last else None,
                            order=last.order + 10 if family else 0, syngate_dir=syngate_dir)
        return f"- added {uid} under {parent}"
    if tool == "edit":
        syngatelib.edit_item(items, uid, {field: call[field] for field in EDITABLE if field in call})
        return f"- edited {uid}"
    if tool == "remove":
        syngatelib.delete_item(items, uid)
        return f"- removed {uid}"
    source, target, link = at(call.get("from")), at(call["to"]), bool(call.get("link"))
    if source is None and uid in items and len(items[uid].parents) == 1:
        source = items[uid].parents[0]
    if source is None and uid in items and not link:
        raise syngatelib.tree_error(f"{uid} has several parents; name the one to re-point in 'from' or pass 'link'")
    syngatelib.move_item(items, uid, target, before=at(call.get("before")), source=source, link=link)
    return f"- moved {uid} under {target}"


def query(connectors, items, uid, text, connector, model, effort, modes=(), session=None, history=()):
    """(reply text, session id) of one exchange anchored at `uid`, sent through the named connection with the exact model, effort and call modes.
    The seed context grows by the statements the text references; without a session to resume, the kept `history` heads the text."""
    connection = connectors.get(connector)
    if uid not in items:
        raise query_error(f"unknown UID '{uid}'")
    if connection is None:
        raise query_error(f"unknown connector '{connector}'")
    if model not in connection.models or effort not in connection.efforts:
        raise query_error(f"unknown model '{model}' or effort '{effort}'")
    if not set(modes) <= set(connection.modes):
        raise query_error(f"unknown mode in {sorted(modes)}")
    if not text.strip():
        raise query_error("empty input")
    context = seed_context(items, uid) + referenced(items, text)
    return connection.dispatch(context, text if session else transcript(history, text), model=model, effort=effort, modes=modes, session=session)


class claude_code_connector:
    label = "Claude Code"
    models = CLAUDE_MODELS
    efforts = CLAUDE_EFFORTS
    always_allowed = ("Agent",)
    # Direct edits of the tree files are denied in every mode; the tree changes only through tree calls.
    denied = ("Edit(syngate/**)", "Write(syngate/**)")
    # Call modes: what a turn may do without prompting, as (label, hint, on by default, permission mode, allowed tools).
    modes = {
        "edit": ("Edit", "project files", True, "acceptEdits", ()),
        "internet": ("Internet", "access", True, None, ("WebSearch", "WebFetch")),
        "workflows": ("Workflows", "run subagents", False, None, ("Workflow",)),
    }

    def __init__(self, cli=("claude",), cwd=None):
        self.cli = list(cli)
        self.cwd = cwd
        self.live = {}

    def interrupt(self, ident):
        """Kill the `claude` process of the turn the thread `ident` is running, if one is live."""
        proc = self.live.get(ident)
        if proc:
            proc.kill()

    def settings(self):
        return {"label": self.label,
                "models": [{"id": model, "name": model.removeprefix("claude-")} for model in self.models],
                "efforts": list(self.efforts),
                "modes": [{"id": mode, "label": label, "hint": hint, "default": default} for mode, (label, hint, default, _, _) in self.modes.items()]}

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        """(answer, session id) of one non-interactive `claude -p` turn, the answer being the structured output shaped by
        `ANSWER_SCHEMA`, else the reply text; `session` continues an earlier one. The turn lasts until the process exits:
        a subagent or workflow launched in it is awaited by the CLI, which answers again on its report, and the answer of the
        turn is that last one."""
        permission = next((self.modes[mode][3] for mode in modes if self.modes[mode][3]), None)
        tools = list(dict.fromkeys([*self.always_allowed, *(tool for mode in modes for tool in self.modes[mode][4])]))
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as seed:
            seed.write(context)
            seed.flush()
            argv = [*self.cli, "-p", text, "--output-format", "json", "--json-schema", json.dumps(ANSWER_SCHEMA),
                    "--append-system-prompt-file", seed.name, "--append-system-prompt", TREE_CALLS,
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
            for flag, value in (("--model", model), ("--effort", effort), ("--resume", session), ("--permission-mode", permission),
                                ("--allowedTools", ",".join(tools)), ("--disallowedTools", ",".join(self.denied))):
                if value:
                    argv += [flag, value]
            ident = threading.get_ident()
            self.live[ident] = done = subprocess.Popen(argv, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = done.communicate()
            finally:
                self.live.pop(ident, None)
        try:
            reply = json.loads(stdout)
        except ValueError:
            raise connection_error((stderr or stdout).strip() or f"claude exited with {done.returncode}") from None
        if done.returncode or reply.get("is_error"):
            raise connection_error(str(reply.get("result") or stderr.strip() or f"claude exited with {done.returncode}"))
        answer = reply.get("structured_output")
        return (reply["result"] if answer is None else answer), reply["session_id"]


CONNECTORS = {"claude_code": claude_code_connector}
