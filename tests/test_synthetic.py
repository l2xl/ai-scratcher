# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Synthetic agent library: item seed context and AI connectors."""

import json
import sys
import threading
import time

import pytest

import synthetic
import syngate_ui
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
def test_seed_context_opens_with_the_default_prompt_then_every_ancestor_once_parents_first(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    make_item(syngate_dir, "SIDE-A", "Side A.\n", parents=("ROOT",))
    make_item(syngate_dir, "SIDE-B", "Side B.\n", parents=("ROOT",))
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("SIDE-A", "SIDE-B"), tests=None)
    items, errors = syngatelib.load_tree(syngate_dir)
    assert not errors
    prompt = synthetic.PROMPTS["default"] + "\n"
    assert synthetic.seed_context(items, "ROOT") == prompt + "Root.\n"
    assert synthetic.seed_context(items, "SIDE-B") == prompt + "Root.\nSide B.\n"
    assert synthetic.seed_context(items, "LEAF-001") == prompt + "Root.\nSide A.\nSide B.\nIt shall leaf.\n"


def _library(folder):
    """A prompt library of two skills -- `rules` default, `brief` on request -- and one tool, written as front-matter markdown."""
    folder.mkdir(exist_ok=True)
    (folder / "rules.md").write_text("---\nkind: skill\ndescription: The rules\ndefault: true\n---\n# Rules\nFollow them.\n")
    (folder / "brief.md").write_text("---\nkind: skill\ndescription: Be brief\n---\n\nSay less.\n\n")
    (folder / "echo.md").write_text("---\nkind: tool\ndescription: Echo back\n---\n# Echo\n`{\"tool\": \"echo\"}` echoes.\n")
    (folder / "notes.txt").write_text("not a prompt")
    return synthetic.load_prompts(folder)


@pytest.mark.syngate("AI_CHAT-PROMPTS")
def test_the_prompt_library_is_read_from_front_matter_markdown_files_by_name_as_skills_and_tools(tmp_path, syngate_tree):
    library = _library(tmp_path / "prompts")
    assert library == {
        "brief": synthetic.prompt("brief", "skill", "Be brief", False, "Say less.\n"),
        "echo": synthetic.prompt("echo", "tool", "Echo back", False, "# Echo\n`{\"tool\": \"echo\"}` echoes.\n"),
        "rules": synthetic.prompt("rules", "skill", "The rules", True, "# Rules\nFollow them.\n")}
    (tmp_path / "prompts" / "bare.md").write_text("No front matter.\n")
    assert synthetic.load_prompts(tmp_path / "prompts")["bare"] == synthetic.prompt("bare", "skill", "", False, "No front matter.\n")
    assert synthetic.LIBRARY == synthetic.load_prompts() and set(synthetic.PROMPTS) == set(synthetic.LIBRARY) | {synthetic.DEFAULT_PROMPT}
    assert all(synthetic.PROMPTS[name] == entry.text for name, entry in synthetic.LIBRARY.items())
    assert synthetic.SKILLS == {name: entry for name, entry in synthetic.LIBRARY.items() if entry.kind == "skill"}
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    items, _ = syngatelib.load_tree(syngate_dir)
    assert synthetic.seed_context(items, "ROOT") == synthetic.PROMPTS[synthetic.DEFAULT_PROMPT] + "\nRoot.\n"


@pytest.mark.syngate("AI_CHAT-PROMPTS-DEFAULT")
def test_the_default_prompt_is_composed_of_the_default_skills_the_skills_on_request_and_every_tool(tmp_path):
    library = _library(tmp_path / "prompts")
    assert synthetic.default_prompt(library) == ("# Rules\nFollow them.\n\n# Skills on request\nLoad one with the `skill` tool:\n- `brief`: Be brief\n\n"
                                                 "# Echo\n`{\"tool\": \"echo\"}` echoes.\n")
    assert synthetic.default_prompt({"rules": library["rules"]}) == "# Rules\nFollow them.\n\n# Skills on request\nLoad one with the `skill` tool:\nnone\n"
    shipped = synthetic.PROMPTS[synthetic.DEFAULT_PROMPT]
    assert shipped == synthetic.default_prompt(synthetic.LIBRARY) and not list(synthetic.PROMPTS_DIR.glob("CLAUDE.md"))
    assert all(entry.text in shipped for entry in synthetic.LIBRARY.values() if entry.kind == "tool" or entry.default)
    assert all(f'"tool": "{tool}"' in shipped for tool in synthetic.CALLS)


@pytest.mark.syngate("AI_CHAT-PROMPTS-SKILLS")
def test_every_skill_carries_a_description_and_only_the_skills_on_request_are_listed_for_loading(tmp_path):
    library = _library(tmp_path / "prompts")
    listed = synthetic.default_prompt(library).split(synthetic.SKILLS_ON_REQUEST, 1)[1]
    assert listed.startswith("- `brief`: Be brief\n") and "rules" not in listed and "echo" not in listed
    assert synthetic.SKILLS and all(entry.description for entry in synthetic.SKILLS.values())
    assert "skill" in synthetic.CALLS and synthetic.LIBRARY["skill"].kind == "tool"


@pytest.mark.syngate("AI_CHAT-SKILL-CALL-TOOL")
def test_the_tool_call_skill_is_a_default_skill_stating_the_answer_format_and_the_result_round_trip():
    skill = synthetic.SKILLS["call_tool"]
    assert skill.default and skill.text.startswith("# Tool calls\n")
    for phrase in ('{"kind": "text", "text": "…"}', '{"kind": "calls", "calls": [...]}', "```syngate", "never both",
                   "applied in order", "first refused one stops the batch", "next prompt"):
        assert phrase in skill.text


@pytest.mark.syngate("AI_CHAT-SKILL-SYNGATE")
def test_the_syngate_rules_skill_is_a_default_skill_stating_the_dag_rules():
    skill = synthetic.SKILLS["syngate"]
    assert skill.default and skill.text.startswith("# Syngate DAG rules\n")
    for phrase in ("directed acyclic graph", "testable statement", "split into children", "markdown files", "root-relative link",
                   "tags equal to the item UID", "numbered suffix"):
        assert phrase in skill.text


@pytest.mark.syngate("AI_CHAT-PROMPTS-TOOLS")
def test_every_tool_definition_is_in_the_default_prompt_and_every_call_is_defined_by_a_tool():
    tools = {name: entry for name, entry in synthetic.LIBRARY.items() if entry.kind == "tool"}
    assert set(tools) == {"skill", "syngate_api"} and all(entry.description for entry in tools.values())
    shipped = synthetic.PROMPTS[synthetic.DEFAULT_PROMPT]
    assert all(entry.text in shipped for entry in tools.values())
    assert all(any(f'"tool": "{call}"' in entry.text for entry in tools.values()) for call in synthetic.CALLS)
    assert all(f'"tool": "{call}"' in tools["syngate_api"].text for call in list(synthetic.TOOLS) + ["query", "context", "test"])


@pytest.mark.syngate("AI_CHAT-TOOL-SKILL")
def test_the_skill_call_brings_the_named_skill_back_as_the_next_prompt_and_an_unknown_name_is_refused(tmp_path, syngate_tree):
    assert synthetic.parse_reply({"kind": "calls", "calls": [{"tool": "skill", "name": "syngate"}]}) == ("calls", [{"tool": "skill", "name": "syngate"}])
    with pytest.raises(synthetic.protocol_error, match="missing name"):
        synthetic.parse_reply({"kind": "calls", "calls": [{"tool": "skill"}]})
    shape = next(shape for shape in synthetic.ANSWER_SCHEMA["properties"]["calls"]["items"]["anyOf"] if shape["properties"]["tool"]["const"] == "skill")
    assert shape["required"] == ["tool", "name"] and "uid" not in shape["properties"]
    assert synthetic.apply_call({}, "ROOT", {"tool": "skill", "name": "syngate"}) == "- skill syngate:\n" + synthetic.SKILLS["syngate"].text
    with pytest.raises(syngatelib.tree_error, match="unknown skill 'fly'"):
        synthetic.apply_call({}, "ROOT", {"tool": "skill", "name": "fly"})
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    app = syngate_ui.SyngateUIApp(root=tmp_path, syngate_dir=syngate_dir)
    calls = lambda *batch: {"kind": "calls", "calls": list(batch)}
    ask = lambda: app.chat({"uid": "ROOT", "connector": "scripted", "model": "model-a", "effort": "low", "text": "Which rules?"})
    app.connectors = {"scripted": (scripted := ScriptedConnection(calls({"tool": "skill", "name": "call_tool"}), {"kind": "text", "text": "Got it."}))}
    result = ask()
    assert scripted.prompts[1] == (synthetic.RESULTS_PREAMBLE + "- skill call_tool:\n" + synthetic.SKILLS["call_tool"].text, "session-1")
    assert result["reply"].endswith(synthetic.SKILLS["call_tool"].text + "\nGot it.") and result["session"] == "session-2"
    app.connectors = {"scripted": ScriptedConnection(calls({"tool": "skill", "name": "fly"}))}
    with pytest.raises(syngate_ui.ApiError, match="skill fly refused: unknown skill 'fly'") as refused:
        ask()
    assert refused.value.status == 400


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
    assert (reply, session) == (synthetic.PROMPTS[synthetic.DEFAULT_PROMPT] + "\nRoot.\nIt shall leaf.\n|What is missing?|model-b|high|edit|session-0", "session-1")
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
    assert {"id": "claude-opus-5-5", "name": "opus-5-5"} in settings["models"]
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
def test_the_seed_context_is_the_appended_system_prompt_and_tree_file_edits_are_denied_in_every_mode(tmp_path, syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    items, _ = syngatelib.load_tree(syngate_dir)
    stub = tmp_path / "claude_stub.py"
    stub.write_text(STUB_CLAUDE)
    connector = synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path)
    for modes in ([], ["edit"], ["edit", "internet", "workflows"]):
        echoed = json.loads(connector.dispatch(synthetic.seed_context(items, "ROOT"), "Go.", modes=modes)[0])
        argv = echoed["argv"]
        assert echoed["seed"] == synthetic.PROMPTS[synthetic.DEFAULT_PROMPT] + "\nRoot.\n" and "--append-system-prompt" not in argv
        assert argv[argv.index("--disallowedTools") + 1] == "Edit(syngate/**),Write(syngate/**)"
    assert synthetic.LIBRARY["syngate_api"].text in synthetic.PROMPTS[synthetic.DEFAULT_PROMPT]


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


# Stub `claude` answering through structured output, the reply text being something else entirely.
STUB_STRUCTURED = """
import json
print(json.dumps({"result": "the text the model also printed", "structured_output": {"kind": "calls", "calls": [{"tool": "query", "uid": "@"}]},
                  "session_id": "session-8", "is_error": False}))
"""


@pytest.mark.syngate("CLAUDE_CODE-060")
def test_the_answer_schema_carries_every_tool_call_and_the_structured_output_is_the_answer(tmp_path):
    stub = tmp_path / "claude_stub.py"
    stub.write_text(STUB_CLAUDE)
    argv = json.loads(synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path).dispatch("", "Go.")[0])["argv"]
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema == synthetic.ANSWER_SCHEMA and schema["properties"]["kind"]["enum"] == ["text", "calls"]
    shapes = {shape["properties"]["tool"]["const"]: shape["required"] for shape in schema["properties"]["calls"]["items"]["anyOf"]}
    assert shapes == {tool: ["tool", *fields] for tool, fields in synthetic.CALLS.items()}
    assert set(synthetic.QUERIES) == {"query", "context", "skill"} and set(synthetic.RUNS) == {"test"}
    assert set(synthetic.CALLS) == set(synthetic.TOOLS) | set(synthetic.QUERIES) | set(synthetic.RUNS)
    structured = tmp_path / "structured_stub.py"
    structured.write_text(STUB_STRUCTURED)
    answer, session = synthetic.claude_code_connector(cli=(sys.executable, str(structured)), cwd=tmp_path).dispatch("", "Go.")
    assert (answer, session) == ({"kind": "calls", "calls": [{"tool": "query", "uid": "@"}]}, "session-8")
    assert synthetic.parse_reply(answer) == ("calls", [{"tool": "query", "uid": "@"}])
    assert synthetic.parse_reply({"kind": "text", "text": "Talk."}) == ("chat", "Talk.")
    for mixed in ({"kind": "text", "text": "Both.", "calls": [{"tool": "remove", "uid": "X"}]}, {"kind": "calls", "text": "Both.", "calls": [{"tool": "remove", "uid": "X"}]}):
        with pytest.raises(synthetic.protocol_error, match="never both"):
            synthetic.parse_reply(mixed)
    with pytest.raises(synthetic.protocol_error, match="missing"):
        synthetic.parse_reply({"kind": "calls", "calls": [{"tool": "context"}]})


class ScriptedConnection:
    """Answers the scripted structured answers in turn, keeping every prompt it was handed."""
    label, models, efforts, modes = "Scripted", ("model-a",), ("low",), ()

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        self.prompts.append((text, session))
        return self.answers[min(len(self.prompts), len(self.answers)) - 1], f"session-{len(self.prompts)}"


@pytest.mark.syngate("SYNTHETIC-021")
def test_what_a_batch_queried_goes_back_into_the_session_until_an_answer_carries_no_query(tmp_path, syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("ROOT",), tests=None)
    app = syngate_ui.SyngateUIApp(root=tmp_path, syngate_dir=syngate_dir)
    calls = lambda *batch: {"kind": "calls", "calls": list(batch)}
    query, add = {"tool": "query", "uid": "@"}, {"tool": "add", "uid": "LEAF-002", "header": "Second", "description": "It shall follow."}
    ask = lambda connection: app.chat({"uid": "ROOT", "connector": "scripted", "model": "model-a", "effort": "low", "text": "Add a second leaf."})

    app.connectors = {"scripted": (scripted := ScriptedConnection(calls(query), calls(add, query), {"kind": "text", "text": "Done."}))}
    result = ask(scripted)
    assert [session for _, session in scripted.prompts] == [None, "session-1", "session-2"]
    assert scripted.prompts[0][0] == "Add a second leaf."
    for prompt, children in ((scripted.prompts[1][0], ["LEAF-001"]), (scripted.prompts[2][0], ["LEAF-001", "LEAF-002"])):
        assert prompt.startswith(synthetic.RESULTS_PREAMBLE) and json.loads(prompt.split("- ROOT: ", 1)[1])["children"] == children
    assert scripted.prompts[2][0].split("\n")[1] == "- added LEAF-002 under ROOT"
    assert result["session"] == "session-3" and result["calls"] == [query, add, query] and result["reply"].endswith("- added LEAF-002 under ROOT\n- ROOT: " + json.dumps(synthetic.item_view(app._load(), "ROOT"), ensure_ascii=False) + "\nDone.")

    app.connectors = {"scripted": (looping := ScriptedConnection(calls(query)))}
    with pytest.raises(syngate_ui.ApiError, match="kept querying") as refused:
        ask(looping)
    assert len(looping.prompts) == synthetic.QUERY_ROUNDS and refused.value.extra["session"] == f"session-{synthetic.QUERY_ROUNDS}"

    app.connectors = {"scripted": (unknown := ScriptedConnection(calls(query, {"tool": "query", "uid": "LEAF-009"})))}
    with pytest.raises(syngate_ui.ApiError, match="query LEAF-009 refused") as refused:
        ask(unknown)
    assert refused.value.status == 404 and str(refused.value).startswith("- ROOT: ") and len(unknown.prompts) == 1


# Stub `syngate test`: records one coverage line per addressed binding -- `two` failing with a log -- and keeps the argv it was run with.
STUB_RUNNER = """
import json, sys
argv = sys.argv[1:]
open(sys.argv[0] + ".argv", "a").write(" ".join(argv) + "\\n")
with open(argv[argv.index("--coverage-out") + 1], "a") as out:
    for leaf in argv[1:argv.index("--build-dir")]:
        uid, _, name = leaf.partition(":")
        for binding in ([name] if name else {"LEAF-001": ["one", "two"], "LEAF-002": [""]}[uid]):
            failed = binding == "two"
            out.write(json.dumps({"tags": [uid] + ([binding] if binding else []), "passed": not failed, "name": "", "log": "boom" if failed else ""}) + "\\n")
"""


@pytest.mark.syngate("SYNGATE-API-AI", "run")
def test_a_test_call_runs_the_bound_routines_through_the_page_runner_and_reports_the_outcome(tmp_path, syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root.\n", parents=())
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("ROOT",), tests={"one": None, "two": None})
    make_item(syngate_dir, "LEAF-002", "It shall follow.\n", parents=("ROOT",), order=10, tests=None)
    stub = tmp_path / "runner_stub.py"
    stub.write_text(STUB_RUNNER)
    app = syngate_ui.SyngateUIApp(root=tmp_path, syngate_dir=syngate_dir, cli_prefix=[sys.executable, str(stub)])
    calls = lambda *batch: {"kind": "calls", "calls": list(batch)}
    everything, narrowed = {"tool": "test", "uid": "@"}, {"tool": "test", "uid": "LEAF-001", "name": "two"}
    ask = lambda: app.chat({"uid": "ROOT", "connector": "scripted", "model": "model-a", "effort": "low", "text": "Run the tests."})

    app.connectors = {"scripted": (scripted := ScriptedConnection(calls(everything), calls(narrowed), {"kind": "text", "text": "Done."}))}
    result = ask()
    runs = (tmp_path / "runner_stub.py.argv").read_text().splitlines()
    assert [run.split(" --build-dir")[0] for run in runs] == ["test LEAF-001 LEAF-002", "test LEAF-001:two"]
    assert all(run.endswith(f"--coverage-out {app.run_coverage}") for run in runs)
    outcome, again = scripted.prompts[1][0], scripted.prompts[2][0]
    assert outcome == synthetic.RESULTS_PREAMBLE + "- LEAF-001:one: pass\n- LEAF-001:two: fail\n- LEAF-002: pass\nLEAF-001:two output:\nboom"
    assert again == synthetic.RESULTS_PREAMBLE + "- LEAF-001:two: fail\nLEAF-001:two output:\nboom"
    assert result["calls"] == [everything, narrowed] and result["reply"].endswith("boom\nDone.") and result["session"] == "session-3"
    axes = syngatelib.compute_axes(app._load(), syngatelib.load_coverage([app.run_coverage])[0])
    assert (axes["LEAF-001"]["test"], axes["LEAF-002"]["test"], axes["ROOT"]["test"]) == (syngatelib.TEST_FAILED, syngatelib.TEST_PASSED, syngatelib.TEST_FAILED)

    app.connectors = {"scripted": ScriptedConnection(calls(everything))}
    app.jobs.start([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    try:
        with pytest.raises(syngate_ui.ApiError, match="test ROOT refused: a run is already in progress") as refused:
            ask()
        assert refused.value.status == 409 and len((tmp_path / "runner_stub.py.argv").read_text().splitlines()) == 2
    finally:
        app.jobs.cancel()


STUB_AWAITED = """
import json, sys, time
open("started", "w").write("the first answer, given before the subagent reported")
time.sleep(0.3)
print(json.dumps({"result": "the agent reported", "structured_output": {"kind": "text", "text": "the agent reported"}, "session_id": "session-9", "is_error": False}))
"""


@pytest.mark.syngate("CLAUDE_CODE-070")
def test_a_turn_lasts_until_the_process_exits_and_its_answer_is_the_last_one(tmp_path):
    stub = tmp_path / "awaited_stub.py"
    stub.write_text(STUB_AWAITED)
    connector = synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path)
    started = time.monotonic()
    answer, session = connector.dispatch("", "Launch an agent.")
    assert time.monotonic() - started >= 0.3 and (tmp_path / "started").exists()
    assert (answer, session) == ({"kind": "text", "text": "the agent reported"}, "session-9") and connector.live == {}


STUB_STUCK = "import time; time.sleep(30)"


@pytest.mark.syngate("CLAUDE_CODE-080")
def test_an_interrupted_turn_kills_its_process_and_fails_as_a_connection_error(tmp_path):
    stub = tmp_path / "stuck_stub.py"
    stub.write_text(STUB_STUCK)
    connector = synthetic.claude_code_connector(cli=(sys.executable, str(stub)), cwd=tmp_path)
    failures = []

    def turn():
        try:
            connector.dispatch("", "Never answers.")
        except synthetic.connection_error as err:
            failures.append(str(err))

    thread = threading.Thread(target=turn)
    thread.start()
    deadline = time.monotonic() + 5
    while thread.ident not in connector.live and time.monotonic() < deadline:
        time.sleep(0.02)
    connector.interrupt(thread.ident)
    thread.join(5)
    assert not thread.is_alive() and failures == ["claude exited with -9"] and connector.live == {}
