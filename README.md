# AI-Scratcher

A general-purpose AI assistant, built around deep iterative analysis of the
user–AI dialog and synthesis of its content into a **Synergy Context Gate (syngate)** — a DAG of
statements about the project of chat subject that then constructs the optimal context for the AI model,
per replica of the dialog.

A conversation with AI-Scratcher starts as an ordinary AI chat. Iteratively, the dialog is
transformed into a DAG: a sentence that survived the exchange becomes a node — a short
statement with the items it follows from as parents — and the chat goes on anchored at items rather
than at the tail of a transcript. The DAG is then the context engine: a replica anchored at an item
is answered from the item's ancestry alone — the statements from the root down to the anchor, each
once, in order — so the model receives exactly the reasoning that leads to the point under
discussion, not the whole history and not a lossy summary of it. The reply either continues the
dialog or condenses it back into the DAG, and the user keeps the last word by reviewing what the
DAG asserts.

Both directions of the loop are what the toolkit implements:

- **Dialog → DAG (analysis).** Items are plain YAML files, one sentence each, shaped only by
  `parents` (*Tree Model*). The local editor (`syngate ui`) shows the DAG as an outline and as one
  structured document, and the AI agent restructures it through *tree calls* — add, remove, move —
  never by editing files (*Dialog agent*).
- **DAG → context (synthesis).** The seed context of an item is the chain of its ancestors'
  statements, root first, then its own; multi-parent items enter once at their first occurrence
  (`SYNGATE-010`). That chain, and only that chain, is what a replica anchored at the item is
  answered from.
- **Gate.** Two optional disciplines turn a DAG into a gate: *review* stamps an item as approved by
  the user, so the DAG cannot drift from what was agreed, and *test* binds an item to executable
  routines, so the DAG cannot drift from what the code does (*Process Rules*). Both are switched on
  the root item and are off by default.

Product architecture definition through AI dialog analysis and synthesis is one use case of this loop:
with `test` and `review` on, the DAG of a software project is its architecture and contract tree,
every leaf definition bound to a test frozen on approval, and the ancestry chain is the context an AI coding
session is seeded from. Open Trader is such a project, and so is this repository — its own items
live under `syngate/` and its tests bind to them.

# Install and run

```
pipx install git+https://github.com/l2xl/ai-scratcher.git      # or: uv tool install git+…
cd <managed project>
syngate ui
```

The Python package is `ai_scratcher` (`syngatelib` library, `syngate` CLI, `syngate_ui` local
editor, `synthetic` agent, `check_self_approval`, `render_syngate_report`); any modern Python
(3.10+) with `pyyaml`, `jinja2` and `pytest` — pytest is a runtime dependency, not a dev extra:
`syngate test`/`review` run a managed project's pytest bindings with the toolkit's own interpreter.
Toolkit tests: `pip install -e .` then `pytest`.

**Managed project root**: `--root PATH` on any command, else the `SYNGATE_ROOT` environment
variable, else the nearest ancestor of the working directory holding a `syngate/` folder. Test
discovery scans `scripts/tests/` and `tests/` for pytest bindings and `test/` for Catch2 ones.

# Tree Model

- **Any `*.yml` file under any `syngate/` subfolder is one item.** The UID is the file stem
  (`INFRA-066.yml` → `INFRA-066`), globally unique. Folders are external sorting only — moving a
  file between folders changes nothing. Other extensions (`.md`, …) are ignored by tooling.
- **The tree shape lives inside the items** via `parents`. Multi-parent items form a DAG; exactly
  one item has empty `parents` (the root, `OPEN-TRADER`). No settings files anywhere.
  Use consideration to name items, which represents a whole feature, without a number (like top OPEN_TRADER),
  then its subbranches may be groupped by subfolder and have same name and numbered suffix (unless it again represent large feature)
- **Leaf vs branch is structural**: a branch has children; a `test` key binds tests to the item
  that carries it — every implemented leaf, and any branch that has tests of its own. A
  test-bearing branch passes only when its own bindings and all its children pass, and it is
  reviewed and frozen exactly as a leaf is. A childless item without `test` is simply not yet
  implemented — it rolls up as `not_implemented` like any leaf with no coverage.
- **The root item switches the tree's features.** `test: enabled` turns test binding on and
  `review: enabled` turns review stamping on; both are **off when absent**, so a project that wants
  neither the TDD gate nor approval stamps carries plain items only. A root's own binding or stamp
  switches its feature on as well. With a feature off, an item carrying its key is a layout problem,
  the commands of that feature refuse to run and the page shows none of its affordances; with the
  test feature off, `review` stamps childless items as approved text. AI-Scratcher's own root
  (`AI-SCRATCHER`) and Open Trader's (`OPEN-TRADER`) switch both on.

# Item Schema

```yaml
header: CI build on push
description: |
  The CI pipeline shall build the project on every push to the repository.
parents: [INFRA-065]
order: 10
test: ~
review: <sha256 hex, present only after user review>
```

- `header` — a minimal noun phrase naming the behaviour (`Per-subscription condition`), no mechanism and no
  restated assertion; the description carries the contract.
- `description` — exactly one "shall" on test-bearing leaves.
- `order` — optional, presentation-only sibling sort key; siblings sort by `(order, UID)`.
  Excluded from the reviewed stamp: reordering a report never triggers re-review.
- `test` forms: `~` → single default test bound by the `[UID]` tag alone (becomes
  `test: <routine-sha256>` once reviewed); mapping `name: sha|~` → each binding bound by the
  `[UID][name]` tag pair. Binding names: `[a-z0-9_]+`, unique per item. On the root item,
  `enabled` / `disabled` is the tree's test feature switch instead.
- `review` — transparent stamp: sha256 hex over the canonical JSON
  `{"children":[…],"description":…,"header":…,"parents":[…],"tests":{name:sha}|null}` (`sort_keys`,
  compact separators, UTF-8; default binding name `""`, unstamped shas `""`). `children` holds the
  immediate child UIDs and is present only for an item that has any, so a childless item's stamp is
  unchanged and adding or removing a child moves its parent's. Recompute with
  `syngatelib.item_stamp(items, uid)`, or `compute_stamp` plus the child set. Stamping is **user-only** — the stamp is the
  record of user approval. On the root item, `enabled` / `disabled` is the tree's review feature
  switch instead.
- A call that moves stamped content settles the stamps it touched one of two ways. With
  `--clear-review` — the page always passes it, and it is yours to pass from the CLI — the stamp is
  dropped and the item reads as *not reviewed*. Without it, as when a call is proxied from an AI
  answer or a file is edited behind the tooling, the stamp is left standing over content that no
  longer matches it and the item reads as *review violated*, so an unattributed change is loud
  rather than quietly unapproved.
- The old spellings `tests` and `reviewed` are read as the same keys, so an existing tree keeps
  loading; the canonical writer emits `test` and `review`, so items migrate as they are edited.

# Test Binding

Binding identity is the tag pair; the routine's location is *discovered* from tags at check time,
never declared in items — tests move freely between files without touching `syngate/`.

- **Pytest** (`scripts/tests/`, `tests/`): `@pytest.mark.syngate("INFRA-066")` for the default binding,
  `@pytest.mark.syngate("INFRA-066", "binding_name")` for a named one. Discovered by an `ast` scan.
- **Catch2** (`test/`): `TEST_CASE("…", "[INFRA-066]")` or `…[INFRA-066][binding_name]`.
  Discovered by a source scan for test macros; the binding name is the tag immediately following
  the UID tag, so place item tags last.
- Each binding must resolve to **exactly one** routine; 0 or >1 is a gate error on reviewed items.
- **Routine hash**: sha256 of the routine's raw source span (pytest: decorators through the end of
  the function; Catch2: the TEST_CASE line to the next test macro or EOF). Shared-fixture changes
  outside the span are consciously not tracked.

# Coverage JSONL

Test executions self-report which bindings ran: one JSON line
`{"tags": ["INFRA-066", "binding_name"], "passed": true, "name": …, "log": …}` appended to the
file named by `SYNGATE_COVERAGE_FILE` (no emission when unset).

- Emitters live in the managed project: a pytest `conftest.py` hook (see `tests/conftest.py` here)
  and, for Catch2, a listener linked into every test executable (Open Trader's
  `test/syngate_coverage_listener.cpp`).
- The gate joins records against `test:` in both directions: every reviewed leaf binding needs ≥1
  executed record, and every record's UID must match a known item.

# CLI (`syngate`)

The tree calls (`add` / `edit` / `move` / `delete`) are one line each over the core functions in
`syngatelib`; the browser editor and the AI wrapper drive those same functions, so the command line
is their genuine spelling rather than a second implementation.

- `syngate add <UID> --parent <UID> [--dir syngate/<folder>] [--order N] [--header H]
  [--description D] [--branch]` — scaffold an item (with a `test: ~` binding while the tree's test
  feature is on, which `--branch` withholds).
- `syngate edit <UID> [--header H] [--description D] [--order N]` — change an item in place; with
  `--clear-review` an edit of anything under the stamp (header, description, parents, bindings)
  drops its `review`, without it the stamp is left standing and the review reads as violated.
- `syngate move <UID> --to <UID> [--before <UID>] [--from <UID>] [--link]` — add and remove parent
  links and place the item among its siblings: `--from` names the link to re-point when the item
  has several, `--link` adds `--to` as one more parent instead of re-pointing, and a `--from` that
  is a parent while `--to` already is another drops the `--from` link.
- `syngate delete <UID>` — remove a childless item's file.
- `syngate query <UID>` — print the item as the AI reads it back: header, description, parents, children
  in sibling order and kind, as JSON.
- `syngate context <UID>` — print the seed context an AI exchange anchored at the item is formed from.
- `syngate test <UID…> [--build-dir DIR] [--coverage-out FILE]` — run the routines bound to leaf
  items (literals or glob patterns) without stamping: pytest routines by node id in one process,
  Catch2 cases by an OR of their tag pairs in one run per test binary. With `--coverage-out` the
  run's records are folded into FILE, **replacing** the previous records of every re-run binding —
  with "any failed record reddens the leaf", an appended re-run could never turn a leaf green again.
  Runs whichever way the test feature sits: off only means the tree does not *await* tests of its
  items, so the routines tagged for them still run and still record coverage — with the switch off
  the bindings come from the discovered tags, since nothing is declared. Stamping stays `review`'s.
- `syngate review <UID>` — **user-only**: validates the item, discovers its bindings, runs the bound
  tests, and stamps routine shas + `review` once every binding resolves to exactly one runnable
  routine — whether that routine currently passes or fails. A failing routine still freezes and the
  leaf simply rolls up as `test_failed` (TDD red state) until the implementation lands; only a
  binding that can't be run at all (ambiguous, unresolved, or not built) blocks stamping. `syngate clear
  <UID>` removes the stamp (and reverts shas to `~`). Takes `--coverage-out` like `test`. Both are
  refused while the tree's review feature is off; with the test feature off, `review` stamps
  childless items without running anything.
- `syngate validate [--coverage FILE …] [--strict]` — structural validation + frozen-routine checks
  (+ coverage join when given files; `--strict` requires every item reviewed). CI entry:
  Open Trader's `ci/gate.sh` (bootstraps `.venv-syngate` with this package; `GATE_STRICT=1` adds `--strict`).
- `syngate report [--coverage FILE …] [--out syngate_status.json] [--html <dir>]` — recursive status
  rollup + static HTML site.
- `syngate ui [--port N] [--coverage FILE …] [--build-dir DIR] [--no-browser]` — serve the local tree
  editor (`syngate_ui.py` + `syngate_ui.html`, stdlib-only) at `http://127.0.0.1:8712`; `--port 0`
  picks an ephemeral port and the URL line is flushed for hosts reading it from a pipe.
  - **The outline tree carries every structured field of an item**: the status icon (a square
    filled by the test status wrapping the review eyes), UID, the header (edited in place, F2) and — by the row's position — `parents`
    and `order`. It doubles as the TOC of the document on the right. A childless row's `T` opens
    the bindings popover (the `test` key, its binding names, the recorded runs). A feature the root
    item keeps off has no affordances on the page: no test axis or `T` without test, no review
    mark, review or clear run without review; a test run stays offered whichever way the switches
    sit, since the test switch only says the tree awaits no tests.
  - **The document shows the whole tree as one structured text**: every item is a titled frame —
    the UID sits on the top border, the description inside, validation problems inline. The label on
    the bottom border carries both status axes of *Status Rollup* — `test passed │ ⚠ review violated`
    — a leaf's own, a branch's rolled up from below. Hovering a leaf's label shows each binding's
    result and where its routine lives; a branch's, the tally of its leaves.
  - **Clicking the label opens the run menu**: *run test* (`syngate test`), *mark reviewed*
    (`syngate review`) and, on a reviewed leaf, *clear review*; on a branch the first two address
    every leaf below it (review asks for confirmation). Runs stream into the console and fold their
    records into `syngate_coverage.jsonl` next to the tree, which the page always reads, so a run
    recolors the statuses without a CI round trip.
  - **One selection drives both panes.** Picking a tree row scrolls the document to that block and
    tints the block's whole subtree blue; clicking a block unfolds the tree down to its row. The
    selected block itself is filled. Up / Down / PageUp / PageDown / Home / End move the selection
    in the pane that holds the focus: the tree walks the folded outline, the document every item.
  - **Double click or Enter turns the selected block into its description editor** (Esc leaves).
  - **Descriptions are markdown** — paragraphs, lists, headings, tables, code, emphasis, links;
    never raw HTML (the text is escaped first, so `data_model<Entity>` stays literal). `[UID]` of a
    known item jumps to its block. **A link to a project file** — root-relative in items,
    `[datahub guide](src/datahub/README.md#pipeline)`, since item files move freely between folders —
    opens that file as a panel right below the block holding the link: rendered, `✕` on its top
    border, double click edits it with the same autosave / keep-mine-take-theirs as item fields.
    Links inside a panel resolve against that file's folder (as on GitHub) and nest below it.
    Only git-tracked files and markdown are served, and only markdown is written: keys and
    databases sit untracked next to the tree.
  - **Row tools**: `+` scaffolds a child inline (UID suggested from the siblings), `✕` deletes a
    childless item, `⊖` drops one parent link of a multi-parent item.
  - **Drag a row by its UID** (keyboard twin: Alt+Shift+arrows): drop on a row's edge to reorder, on
    its middle to re-parent, Ctrl+drop to add one more parent. `order` is set automatically — a free
    integer between the neighbours' keys (one file written), else the family is renumbered in steps
    of 10. `order` is one key per item, so a multi-parent item carries the same key under each parent.
  - **Autosave**: typing is stored per field after ~1 s idle and on leaving the field or the tab.
    Every write is a compare-and-swap against the value the page loaded, so an edit made on disk
    meanwhile (agent, git, IDE) is never silently overwritten — the page offers keep mine / take
    theirs. Reviewed items stay read-only until unlocked through their `✓` badge, since any substance
    edit makes the stamp stale (typing the text back restores it).
  - Test / review / clear shell out to the `syngate.py` code path, so stamping semantics (user-only,
    test-gated) are identical to the terminal. Loopback-bound; every request needs the per-session
    token from the printed URL (Jupyter-style defense for localhost tools that execute commands);
    the page runs under a nonce-only `Content-Security-Policy`, the second fence behind the
    escape-first markdown renderer.

# Dialog tool

The dialog runs inside the DAG (`synthetic.py`). `✦ AI chat` on an item's panel opens an exchange
anchored at that item; every replica is one non-interactive turn of the chosen connector:

- **Context.** The seed context — the default prompt of the prompt library, then the ancestry
  chain of the anchor, root first, then the anchor's own statement — is the system prompt of the
  turn; the user's text is the prompt, and every `@UID` it names brings that item's statement
  into the context as one more section. Nothing else of the conversation travels except the
  connector's own session, which the next replica resumes; a turn without a session (a message
  was dropped from or restored to the exchange) carries the kept messages as a transcript heading
  the text. The Claude Code connector runs the local
  `claude` CLI with the call modes (edit, internet, workflows) as its permission mode and allowed
  tools; its models and efforts are the ones the page offers.
- **Prompt library.** The markdown files under the package's `prompts/` folder, by file stem
  (`synthetic.LIBRARY`), each opened by a front matter: `kind` — `skill` or `tool` — a
  `description` and, for a skill, `default`. A *tool* defines the format of a structured answer
  the harness recognises and turns into a call whose result reaches the model with the next turn;
  a *skill* is additional description only — the rules of how to call a tool are themselves a
  skill (`call_tool.md`), and so are the rules of the DAG (`syngate.md`). The default prompt
  (`synthetic.PROMPTS["default"]`) — deliberately not a `CLAUDE.md` — is composed from the
  library: the default skills, the list of the skills on request by name and description, then
  every tool definition; it heads every seed context. `{"tool": "skill", "name"}` brings a skill on
  request back as the next prompt, the way a query's result comes back.
- **Several chats at once.** Every item has its own chat window, and any number are open at the
  same time, each running its own exchange in parallel with the others. A chat keeps its history
  and its connector session while closed, and reopening `✦ AI chat` on that item continues it; the
  histories survive a reload of the page (per browser tab).
- **Two kinds of reply, never mixed.** A reply is **either** plain text — the dialog goes on — **or**
  exactly one list of Syngate API calls — the dialog is condensed into the DAG — and nothing else.
  The Syngate API tools are handed to the turn as the JSON schema of its structured answer
  (`synthetic.ANSWER_SCHEMA`: `{"kind": "text", "text"}` or `{"kind": "calls", "calls": [...]}`; the
  Claude Code connector passes it as `--json-schema` and reads the turn's `structured_output`); a
  connector without structured output carries the same list in one fenced ```` ```syngate ```` block.
  A reply that carries both, or a malformed call, is refused by the harness (HTTP 422, shown as an
  error in the chat; the session is kept, so the next replica can ask for a clean answer). The
  protocol reaches every turn through the default prompt heading its seed context.
- **Tree calls are the only way the agent touches the DAG.** The files under `syngate/` are never
  edited by the model — the Claude Code connector denies `Edit`/`Write` under `syngate/**` in every
  mode. `"@"` stands for the anchored item, every other item is addressed by its UID:
  `{"tool": "add", "uid", "parent" (default "@"), "header", "description", "kind": "leaf"|"branch"}`
  adds a statement under its parent, last among the siblings, in the folder of its last sibling
  (else the parent's); `{"tool": "edit", "uid", "header", "description"}` changes one;
  `{"tool": "remove", "uid"}` drops a childless statement;
  `{"tool": "move", "uid", "to", "before" (default last), "from"}` re-points the parent link and
  places the item, `from` naming the link when the item has several parents.
- **Queries read the DAG back the same way.** `{"tool": "query", "uid"}` returns the item's header,
  description, parents, children in sibling order and kind; `{"tool": "context", "uid"}` returns
  its seed context; `{"tool": "skill", "name"}` the named skill of the prompt library. The harness
  sends what a batch read back into the same session as the next prompt and continues the exchange
  with the next answer, for a bounded number of rounds (`synthetic.QUERY_ROUNDS`), so the agent can
  look at an item's children before placing new ones.
- **Test runs go through the page's runner.** `{"tool": "test", "uid", "name"}` runs the routines bound
  to the leaves under the item — every `test` call of a batch joins one run, after the batch's other
  calls, and `name` narrows a leaf to one binding (the CLI spelling `syngate test UID:binding`). It is
  the same single-flight runner as ▶ run: the records land in the run coverage, the statuses recolor,
  and a run live elsewhere refuses the call. The harness awaits the run and hands its outcome back
  like a query's result — one line per leaf binding with pass/fail, then the output of every failed
  routine. Review stamping stays the user's act: there is no review call.
- **A turn lasts as long as its subagents.** The headless `claude` CLI awaits every subagent and
  workflow a turn launches and answers again on their reports, so the process exits only then and
  the turn's answer is the last one given (a shell command left in the background, by contrast, is
  killed when the turn ends). The harness therefore runs every turn as a server-side job
  (`POST /api/turn` starts it, `GET /api/turn/<id>?wait=<s>` awaits it; `POST /api/chat` is the same
  exchange awaited in one request): the page polls it and re-attaches to it after a reload, and while
  it runs the chat's close mark is a `■ stop` button (`POST /api/turn/<id>/stop`) that kills the
  connector's process — the calls applied before the stop stay applied, and the chat shows the turn
  as stopped.
- **Applied through the editor's own mutations.** The harness runs the calls in order through
  `add_item`, `edit_item`, `delete_item` and `move_item`, stops at the first refused one and shows
  the applied list in the history; the DAG re-renders through the fingerprint poll, and the user
  reviews the result exactly as any other edit.

The chat's place in the item panel, its ways of starting, the message tools and the history are
specified in [AI_CHAT.md](AI_CHAT.md).

# Status Rollup

Two independent axes per item (`compute_axes`; what the UI shows):

- **Test**: `unknown` | `test_passed` | `test_failed`. Leaf: any failed record → failed; any binding
  without a record → unknown; else passed. Branch: the worst of its children and its own bindings,
  if it carries any — a failed item fails every ancestor, else one unknown item leaves them
  unknown. The axis reflects the run records alone — a violated review never reddens it. A run that cannot execute a binding (no or several tagged
  routines, test binary not built) records it as failed — a test that cannot be found is red.
- **Review**: `not_reviewed` | `reviewed` | `review_violated`. An item's own validation problem
  (stale stamp, drifted frozen routine, malformed item) is a violated review; a branch without
  own tests is reviewed only through its children; violated outranks not reviewed, which outranks reviewed.

The single-status rollup below (`compute_status`) is what `syngate report`, the CI summary and the
check run still publish; its frozen tests (INFRA-043/044/070) define it, so retiring it in favour of
the axes is a user decision.

Leaf: no executed records → `not_implemented`; any failed record → `test_failed`; all bindings
covered and passing → `test_passed`; some covered → `partially_implemented`. A childless item
without a `tests` key has no bindings to cover, so it rolls up as `not_implemented` the same way.
Branch: aggregate of children (any failed → failed; all not_implemented → not_implemented; all
passed → passed; else partial).

**Validation problems redden the item they name, never the item that found them.** A stale review
stamp, a drifted frozen routine or a malformed item is a defect of *that* item: `item_problems`
attributes it by UID, `compute_status` marks the item `test_failed`, and it rolls up through that
item's own parents only. An item whose bound test detects such a violation is working, so it
stays green — the tooling's own tests assert tooling behaviour against fixtures, and where they read
the live tree (`INFRA-030`) they assert its *layout* (`validate_layout`), not its review state.
Coverage gaps are deliberately not item problems: an unrun binding already rolls up as
`not_implemented`, and one missing coverage file would otherwise redden every reviewed leaf at once.

# Process Rules (TDD gate)

- **Test-first, then freeze.** The covering routine is tagged with the leaf's UID before
  implementation; the user approves via `syngate review`, which freezes the routine by hash while it is
  still red — approval fixes *which* routine and *what it says*, not whether it already passes.
- **Frozen routines are immutable.** Editing a reviewed leaf's bound routine reddens the gate until
  a user-approved `syngate clear` + re-review.
- **Two-commit re-approval** (`check_self_approval.py`, Open Trader CI `approval` job): a commit that
  changes an item's approval may neither change the item's substance nor touch any file under the
  test trees.
- **CI flow** (Open Trader `.github/workflows/validate.yml`): build → ctest (emits
  `build-ci/syngate_coverage.jsonl`, uploaded as `syngate-coverage-cpp`) → syngate job: `gate.sh`,
  pytest (emits `pytest-coverage.jsonl`), coverage join via `gate.sh --coverage …`, `syngate report`,
  job summary + `Syngate Status` check run.
- **A red gate never costs the report.** The gate's verdict decides the job's colour, not whether
  the run is reported: every report step runs on `!cancelled()`, and `gate.sh`'s output (`gate: OK`
  / `gate: FAILED`) is captured into `syngate_validate.log` and folded into the top of both the job
  summary and the check run, so the reason for the red is read off the report itself.

# Contributing

The toolkit manages its own tree: `syngate/` in this repository holds AI-Scratcher's requirement
items, bound to `tests/` through `@pytest.mark.syngate`. Develop against an editable install, so the
UI and CLI run the working copy rather than an installed snapshot:

```
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/syngate ui                  # http://127.0.0.1:8712, tree = this checkout
```

`syngate` takes the managed project from `--root`, else `SYNGATE_ROOT`, else the nearest ancestor of
the working directory holding `syngate/` — started from this checkout it edits this tree, started
from a managed project's checkout the same install edits that project's tree. The rest of the loop:

```
.venv/bin/syngate validate            # this tree: structural + frozen-routine checks
.venv/bin/syngate test 'SYNGATE_UI-*' # bound routines of matching leaves, without stamping
.venv/bin/pytest                      # the toolkit's whole test suite (tests/)
```

`review`/`clear` are user-only here as in any managed project. A managed project's own provisioning
(e.g. Open Trader's `<build dir>/ai-scratcher-venv`) also carries the `syngate` entry point, so
`<that venv>/bin/syngate ui` run from this checkout works too — against the fetched snapshot of the
package, not the working copy.

