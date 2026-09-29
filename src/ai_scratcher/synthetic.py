# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Synthetic agent library: item seed context, AI connections and the tree call protocol of a reply."""

import json
import re
import subprocess
import tempfile

from . import syngatelib

CLAUDE_MODELS = ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The reply protocol every connection appends to its system prompt: the tree changes only through
# tree calls, and a reply is either text or one block of calls.
TREE_CALLS = """\
# Syngate tree calls
The seed context is the chain of syngate item descriptions from the root down to the anchored item, whose description comes last. The item files under syngate/ are read-only for you: the tree changes only through tree calls.
An answer is either plain text or exactly one call block and nothing else, never both:
```syngate
[{"tool": "add", "uid": "AREA-030", "parent": "@", "header": "…", "description": "…", "kind": "leaf"},
 {"tool": "edit", "uid": "@", "header": "…", "description": "…"},
 {"tool": "remove", "uid": "AREA-020"},
 {"tool": "move", "uid": "AREA-010", "to": "AREA", "before": "AREA-030"}]
```
"@" stands for the anchored item; any other item is addressed by its UID, the stem of its file under syngate/.
- add: a new item under `parent` (default "@") with `header` and `description`; `kind` "leaf" (default) carries a test binding, "branch" does not.
- edit: the `header` and/or `description` of an item.
- remove: a childless item.
- move: under parent `to`, in front of sibling `before` (default last). `from` names the parent link to re-point when the item has several; `link` true adds `to` as one more parent instead of re-pointing, and a `from` that is a parent while `to` already is another drops the `from` link.
A call of yours never clears a review stamp: every stamp it moves -- the item's own, and its parents' where their child set changed -- is left standing over content that no longer matches it, so the change shows up as a violated review for the user to settle. Only the user's own edit in the page clears a stamp.
"""
CALL_BLOCK_RE = re.compile(r"```syngate[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
TOOLS = {"add": ("uid", "header", "description"), "edit": ("uid",), "remove": ("uid",), "move": ("uid", "to")}
EDITABLE = ("header", "description")


def seed_context(items, uid):
    """Descriptions of every ancestor, parents first, then the item's own; an item reached through several parents enters once."""
    return "".join(items[selected].description for selected in syngatelib.walk(items, uid))


class connection_error(Exception):
    pass


class query_error(ValueError):
    pass


class protocol_error(ValueError):
    pass


def parse_reply(text):
    """("chat", text) for a plain answer, ("calls", [call, ...]) for exactly one ```syngate block of tree calls; anything mixed or malformed is refused."""
    blocks = CALL_BLOCK_RE.findall(text)
    if not blocks:
        return "chat", text
    if len(blocks) > 1 or CALL_BLOCK_RE.sub("", text, count=1).strip():
        raise protocol_error("an answer is either text or one block of tree calls, never both")
    try:
        calls = json.loads(blocks[0])
    except ValueError as exc:
        raise protocol_error(f"tree calls are not valid JSON: {exc}") from None
    if not isinstance(calls, list) or not calls or not all(isinstance(call, dict) for call in calls):
        raise protocol_error("tree calls must be a non-empty JSON list of objects")
    for call in calls:
        tool = call.get("tool")
        if tool not in TOOLS:
            raise protocol_error(f"unknown tree call {tool!r}")
        missing = [name for name in TOOLS[tool] if not (isinstance(call.get(name), str) and call[name].strip())]
        if missing:
            raise protocol_error(f"{tool}: missing {', '.join(missing)}")
        if tool == "edit" and not any(isinstance(call.get(field), str) and call[field].strip() for field in EDITABLE):
            raise protocol_error(f"edit: missing {' or '.join(EDITABLE)}")
    return "calls", calls


def apply_call(items, anchor, call, syngate_dir=None):
    """Perform one parsed tree call against the loaded tree, `@` standing for the
    anchored item. -> the line reporting what it did; a refusal raises
    `syngatelib.tree_error`.

    No call passes `clear`: a stamp an AI answer moves is deliberately left
    standing over the changed content, so the user meets a violated review
    rather than a silently unreviewed item."""
    at = lambda value: anchor if value == "@" else value
    tool, uid = call["tool"], at(call["uid"])
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


def query(connectors, items, uid, text, connector, model, effort, modes=(), session=None):
    """(reply text, session id) of one exchange anchored at `uid`, sent through the named connection with the exact model, effort and call modes."""
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
    return connection.dispatch(seed_context(items, uid), text, model=model, effort=effort, modes=modes, session=session)


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

    def settings(self):
        return {"label": self.label,
                "models": [{"id": model, "name": model.removeprefix("claude-")} for model in self.models],
                "efforts": list(self.efforts),
                "modes": [{"id": mode, "label": label, "hint": hint, "default": default} for mode, (label, hint, default, _, _) in self.modes.items()]}

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        """(reply text, session id) of one non-interactive `claude -p` turn; `session` continues an earlier one."""
        permission = next((self.modes[mode][3] for mode in modes if self.modes[mode][3]), None)
        tools = list(dict.fromkeys([*self.always_allowed, *(tool for mode in modes for tool in self.modes[mode][4])]))
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as seed:
            seed.write(context)
            seed.flush()
            argv = [*self.cli, "-p", text, "--output-format", "json", "--append-system-prompt-file", seed.name, "--append-system-prompt", TREE_CALLS,
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
            for flag, value in (("--model", model), ("--effort", effort), ("--resume", session), ("--permission-mode", permission),
                                ("--allowedTools", ",".join(tools)), ("--disallowedTools", ",".join(self.denied))):
                if value:
                    argv += [flag, value]
            done = subprocess.run(argv, cwd=self.cwd, capture_output=True, text=True)
        try:
            reply = json.loads(done.stdout)
        except ValueError:
            raise connection_error((done.stderr or done.stdout).strip() or f"claude exited with {done.returncode}") from None
        if done.returncode or reply.get("is_error"):
            raise connection_error(str(reply.get("result") or done.stderr.strip() or f"claude exited with {done.returncode}"))
        return reply["result"], reply["session_id"]


CONNECTORS = {"claude_code": claude_code_connector}
