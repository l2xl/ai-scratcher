# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Synthetic agent library: item seed context and AI connectors."""

import json
import sys

import pytest

import synthetic
import syngatelib

# Stub `claude`: answers the JSON a non-interactive turn prints, echoing its argv and the seed file it was handed.
STUB_CLAUDE = """
import json, sys
argv = sys.argv[1:]
if "rc-1" in argv[1]:
    print(json.dumps({"is_error": True, "result": "model overloaded"})); sys.exit(1)
seed = open(argv[argv.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
print(json.dumps({"result": json.dumps({"argv": argv, "seed": seed}), "session_id": "session-7", "is_error": False}))
"""


@pytest.mark.syngate("SYNTHETIC-010")
def test_seed_context_appends_every_ancestor_once_parents_first(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    make_item(syngate_dir, "SIDE-A", "Side A.\n", parents=("ROOT",))
    make_item(syngate_dir, "SIDE-B", "Side B.\n", parents=("ROOT",))
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("SIDE-A", "SIDE-B"), tests=None)
    items, errors = syngatelib.load_tree(syngate_dir)
    assert not errors
    assert synthetic.seed_context(items, "ROOT") == "Root.\n"
    assert synthetic.seed_context(items, "SIDE-B") == "Root.\nSide B.\n"
    assert synthetic.seed_context(items, "LEAF-001") == "Root.\nSide A.\nSide B.\nIt shall leaf.\n"


class EchoConnection:
    label, models, efforts, modes = "Echo", ("model-a", "model-b"), ("low", "high"), ("edit",)

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        return f"{context}|{text}|{model}|{effort}|{','.join(modes)}|{session}", "session-1"


@pytest.mark.syngate("SYNTHETIC-011")
def test_query_goes_through_the_selected_connection_model_and_effort(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("ROOT",), tests=None)
    items, _ = syngatelib.load_tree(syngate_dir)
    connectors = {"echo": EchoConnection(), "other": None}
    reply, session = synthetic.query(connectors, items, "LEAF-001", "What is missing?", "echo", "model-b", "high", modes=["edit"], session="session-0")
    assert (reply, session) == ("Root.\nIt shall leaf.\n|What is missing?|model-b|high|edit|session-0", "session-1")
    with pytest.raises(synthetic.query_error):
        synthetic.query(connectors, items, "LEAF-001", "hi", "echo", "model-a", "low", modes=["fly"])
    for uid, text, connector, model, effort in (("NOPE", "hi", "echo", "model-a", "low"), ("LEAF-001", "hi", "absent", "model-a", "low"),
                                                ("LEAF-001", "hi", "echo", "model-c", "low"), ("LEAF-001", "hi", "echo", "model-a", "max"),
                                                ("LEAF-001", "  ", "echo", "model-a", "low")):
        with pytest.raises(synthetic.query_error):
            synthetic.query(connectors, items, uid, text, connector, model, effort)


@pytest.mark.syngate("CLAUDE_CODE")
def test_claude_code_connector_dispatches_a_non_interactive_turn(tmp_path):
    stub = tmp_path / "claude_stub.py"
    stub.write_text(STUB_CLAUDE)
    connector = synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path)
    reply, session = connector.dispatch("Root.\nIt shall leaf.\n", "What is missing?", model="claude-haiku-4-5-20251001", effort="low", session="session-6")
    echoed = json.loads(reply)
    assert session == "session-7" and echoed["seed"] == "Root.\nIt shall leaf.\n"
    argv = echoed["argv"]
    assert argv[:4] == ["-p", "What is missing?", "--output-format", "json"]
    for flag, value in (("--model", "claude-haiku-4-5-20251001"), ("--effort", "low"), ("--resume", "session-6")):
        assert argv[argv.index(flag) + 1] == value
    assert "--model" not in json.loads(connector.dispatch("", "Defaults?")[0])["argv"]
    with pytest.raises(synthetic.connection_error, match="model overloaded"):
        connector.dispatch("", "rc-1")


@pytest.mark.syngate("CLAUDE_CODE-010")
def test_claude_code_settings_list_models_by_display_name_efforts_and_modes():
    settings = synthetic.claude_code_connector().settings()
    assert settings["label"] == "Claude Code"
    assert {"id": "claude-opus-5", "name": "opus-5"} in settings["models"]
    assert [model["id"] for model in settings["models"]] == list(synthetic.CLAUDE_MODELS)
    assert not [model for model in settings["models"] if model["name"].startswith("claude")]
    assert settings["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert [(mode["id"], mode["label"], mode["hint"], mode["default"]) for mode in settings["modes"]] == [
        ("edit", "Edit", "project files", True), ("internet", "Internet", "access", True), ("workflows", "Workflows", "run subagents", False)]


def _mode_argv(tmp_path, modes):
    stub = tmp_path / "claude_stub.py"
    stub.write_text(STUB_CLAUDE)
    connector = synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path)
    return json.loads(connector.dispatch("", "Go.", modes=modes)[0])["argv"]


@pytest.mark.syngate("CLAUDE_CODE-020")
def test_edit_mode_accepts_project_file_edits(tmp_path):
    argv = _mode_argv(tmp_path, ["edit"])
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits" and argv[argv.index("--allowedTools") + 1] == "Agent"
    assert "--permission-mode" not in _mode_argv(tmp_path, [])


@pytest.mark.syngate("CLAUDE_CODE-030")
def test_internet_mode_allows_the_web_tools(tmp_path):
    argv = _mode_argv(tmp_path, ["internet"])
    assert argv[argv.index("--allowedTools") + 1] == "Agent,WebSearch,WebFetch" and "--permission-mode" not in argv
    bare = _mode_argv(tmp_path, [])
    assert bare[bare.index("--allowedTools") + 1] == "Agent"


@pytest.mark.syngate("CLAUDE_CODE-040")
def test_workflows_mode_allows_the_workflow_tool(tmp_path):
    argv = _mode_argv(tmp_path, ["workflows"])
    assert argv[argv.index("--allowedTools") + 1] == "Agent,Workflow"
    combined = _mode_argv(tmp_path, ["edit", "internet", "workflows"])
    assert combined[combined.index("--allowedTools") + 1] == "Agent,WebSearch,WebFetch,Workflow"
    assert combined[combined.index("--permission-mode") + 1] == "acceptEdits"


@pytest.mark.syngate("CLAUDE_CODE-050")
def test_the_tree_call_protocol_is_appended_and_tree_file_edits_are_denied_in_every_mode(tmp_path):
    for modes in ([], ["edit"], ["edit", "internet", "workflows"]):
        argv = _mode_argv(tmp_path, modes)
        assert argv[argv.index("--append-system-prompt") + 1] == synthetic.TREE_CALLS
        assert argv[argv.index("--disallowedTools") + 1] == "Edit(syngate/**),Write(syngate/**)"
    assert synthetic.TREE_CALLS.startswith("# Syngate tree calls") and "```syngate" in synthetic.TREE_CALLS


@pytest.mark.syngate("SYNTHETIC-020")
def test_an_answer_is_text_or_one_block_of_tree_calls_never_both():
    calls = [{"tool": "add", "uid": "AREA-030", "parent": "@", "header": "H", "description": "D"}, {"tool": "remove", "uid": "AREA-020"},
             {"tool": "move", "uid": "AREA-010", "to": "AREA", "before": "AREA-030"}]
    block = "```syngate\n" + json.dumps(calls) + "\n```"
    prose = "Plain prose, even with ```json\n[]\n``` inside."
    assert synthetic.parse_reply(prose) == ("chat", prose)
    assert synthetic.parse_reply(f"\n{block}\n\n") == ("calls", calls)
    for mixed in (f"Here you go:\n{block}", f"{block}\nDone.", f"{block}\n{block}"):
        with pytest.raises(synthetic.protocol_error, match="never both"):
            synthetic.parse_reply(mixed)
    for malformed in ("[{", "[]", '{"tool": "add"}', '[{"tool": "rename", "uid": "X"}]', '[{"tool": "add", "uid": "X", "header": "H"}]', '[{"tool": "move", "uid": "X"}]'):
        with pytest.raises(synthetic.protocol_error):
            synthetic.parse_reply(f"```syngate\n{malformed}\n```")
