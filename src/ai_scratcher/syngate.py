#!/usr/bin/env python3
# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Synergy Context Gate (syngate) CLI: the genuine spelling of every tree call.

add / edit / move / delete are the tree mutations, each one line over the core
call in syngatelib; the editor and the AI wrapper reach those same functions.
`query` and `context` read an item back the way the AI does: its fields and
children, and the seed context an AI call anchored at it is formed from.
`review` and `clear` are user-only: the reviewed stamp is the record of the
user's approval. `test` runs the routines bound to items without stamping.
`validate` is the CI gate entry point; `report` computes the recursive status
rollup from coverage JSONL and renders the static HTML site.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import syngatelib, synthetic


def _load_or_die():
    items, errors = syngatelib.load_tree()
    if errors:
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)
    return items


def _mutate(run):
    """Perform one core tree call, reporting what it did or why it was refused."""
    items, _ = syngatelib.load_tree()
    try:
        print(run(items))
    except syngatelib.tree_error as err:
        print(err, file=sys.stderr)
        return 1
    return 0


def cmd_add(args):
    def run(items):
        item = syngatelib.add_item(items, args.uid, args.parent, header=args.header, description=args.description,
                                   kind="branch" if args.branch else "leaf", folder=args.dir, order=args.order,
                                   clear=args.clear_review)
        return f"created {item.path.relative_to(syngatelib.ROOT)}"
    return _mutate(run)


def cmd_edit(args):
    changes = {field: value for field, value in (("header", args.header), ("description", args.description), ("order", args.order)) if value is not None}
    if not changes:
        print("nothing to edit: pass --header, --description or --order", file=sys.stderr)
        return 1

    def run(items):
        syngatelib.edit_item(items, args.uid, changes, clear=args.clear_review)
        return f"{args.uid}: {', '.join(sorted(changes))} updated"
    return _mutate(run)


def cmd_move(args):
    def run(items):
        written = syngatelib.move_item(items, args.uid, args.to, before=args.before, source=args.source,
                                       link=args.link, clear=args.clear_review)
        return f"{args.uid}: under {', '.join(items[args.uid].parents)}" + (f" -- rewrote {', '.join(written)}" if written else " -- already placed")
    return _mutate(run)


def cmd_delete(args):
    def run(items):
        return f"deleted {syngatelib.delete_item(items, args.uid, clear=args.clear_review).relative_to(syngatelib.ROOT)}"
    return _mutate(run)


def cmd_query(args):
    return _mutate(lambda items: json.dumps(synthetic.item_view(items, args.uid), ensure_ascii=False, indent=1))


def cmd_context(args):
    return _mutate(lambda items: synthetic.seed_context(items, args.uid).rstrip("\n"))


def _require(items, feature):
    """False, with the root switch to set printed, when the tree keeps the feature off."""
    if syngatelib.features(items)[feature]:
        return True
    print(f"the tree's {feature} feature is off; switch it on with '{feature}: enabled' in the root item", file=sys.stderr)
    return False


def _addressable(items):
    """The items `review` and `test` address: those with own tests, or -- with the test feature off -- the childless ones."""
    if syngatelib.features(items)["test"]:
        return lambda item: item.is_leaf
    children = syngatelib.children_map(items)
    return lambda item: not children[item.uid]


def _bindings_of(item, discovered, tests_on):
    """The binding names a run addresses. With the test feature on those are the
    item's declared bindings; with it off nothing is declared, so whatever
    routines are tagged for the item are run -- the switch withholds enforcement
    and stamping, never the run itself."""
    if tests_on:
        return list(item.tests or {})
    return [name for uid, name in discovered if uid == item.uid]


def _resolve(item, discovered, names):
    """({binding: Location}, {binding: why it cannot run}) -- a binding runs only when exactly one routine carries its tag pair."""
    resolved, unrun = {}, {}
    for name in names:
        locations = discovered.get((item.uid, name), [])
        if len(locations) == 1:
            resolved[name] = locations[0]
        else:
            found = ", ".join(l.name for l in locations) or "none"
            unrun[name] = f"binding {syngatelib.binding_tag(item.uid, name)} must match exactly one routine, found: {found}"
            print(f"{item.uid}: {unrun[name]}", file=sys.stderr)
    return resolved, unrun


def _run_bound(resolved, build_dir):
    """Run the routines of {uid: {binding: Location}}: pytest routines are
    selected by node id in one process, Catch2 cases by an OR of their tag pairs
    in one process per binary. -> (all passed, {(uid, binding): why it did not run})."""
    node_ids, tags_by_binary, unrun = [], {}, {}
    for uid, bindings in resolved.items():
        for name, loc in bindings.items():
            if loc.path.endswith(".py"):
                node_ids.append(loc.name)
                continue
            binary = syngatelib.ROOT / build_dir / Path(loc.path).stem
            if binary.is_file():
                tags_by_binary.setdefault(binary, []).append(syngatelib.binding_tag(uid, name))
            else:
                unrun[(uid, name)] = f"test binary not built: {binary} (build target {binary.name} first)"
                print(f"{uid}: {unrun[(uid, name)]}", file=sys.stderr)
    passed = True
    if node_ids:
        print(f"running pytest: {' '.join(node_ids)}", flush=True)
        passed &= subprocess.run([sys.executable, "-m", "pytest", *node_ids], cwd=syngatelib.ROOT).returncode == 0
    for binary, tags in tags_by_binary.items():
        print(f"running {binary.name} \"{','.join(tags)}\"", flush=True)
        passed &= subprocess.run([str(binary), ",".join(tags)], cwd=syngatelib.ROOT).returncode == 0
    return passed, unrun


class _Recording:
    """The coverage emitters of the tests run inside write to a scratch file
    that is folded into `coverage_out` on exit. A binding the run could not
    execute is recorded as failed: a test that cannot be found is a red test."""

    def __init__(self, coverage_out):
        self.coverage_out = coverage_out

    def __enter__(self):
        if self.coverage_out:
            self.scratch = tempfile.TemporaryDirectory()
            self.fresh = Path(self.scratch.name) / "coverage.jsonl"
            self.outer = os.environ.get("SYNGATE_COVERAGE_FILE")
            os.environ["SYNGATE_COVERAGE_FILE"] = str(self.fresh)
        return self

    def unrun(self, uid, name, why):
        if self.coverage_out:
            with open(self.fresh, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tags": [uid] + ([name] if name else []), "passed": False, "name": "", "log": why}) + "\n")

    def __exit__(self, *exc):
        if self.coverage_out:
            if self.outer is None:
                del os.environ["SYNGATE_COVERAGE_FILE"]
            else:
                os.environ["SYNGATE_COVERAGE_FILE"] = self.outer
            syngatelib.merge_coverage(self.coverage_out, self.fresh)
            self.scratch.cleanup()


def _expand_uids(items, uid_args, selectable, kind):
    """Expand literal UIDs and glob patterns (fnmatch: * ? [seq]) against the tree.

    Literals pass through untouched so per-command diagnostics stay precise;
    patterns select only items satisfying `selectable`, and matching nothing is
    an error. Order follows the sorted tree; duplicates collapse.
    """
    selected, errors = [], []
    for arg in uid_args:
        if any(ch in arg for ch in "*?["):
            matched = [uid for uid in sorted(items) if fnmatch.fnmatchcase(uid, arg) and selectable(items[uid])]
            if not matched:
                errors.append(f"pattern '{arg}' matches no {kind} item")
            selected.extend(matched)
        else:
            selected.append(arg)
    seen = set()
    return [uid for uid in selected if not (uid in seen or seen.add(uid))], errors


def _review_one(items, structural, discovered, uid, build_dir, recording, stampable):
    own = [e for e in structural if e.startswith(f"{uid}:") and "reviewed stamp" not in e and "no stamped routine sha" not in e]
    if own:
        for line in own:
            print(f"  {line}", file=sys.stderr)
        return False
    item = items.get(uid)
    if item is None:
        print(f"unknown UID '{uid}'", file=sys.stderr)
        return False
    if not stampable(item):
        print(f"{uid}: branch items are reviewed through their children; nothing to stamp", file=sys.stderr)
        return False
    resolved, unresolved = _resolve(item, discovered, item.tests) if item.is_leaf else ({}, {})
    passed, unrun = _run_bound({uid: resolved}, build_dir) if not unresolved else (False, {})
    for name, why in [*unresolved.items(), *((name, why) for (_, name), why in unrun.items())]:
        recording.unrun(uid, name, why)
    if unresolved or unrun:
        print(f"{uid}: bound test could not be run; not stamping", file=sys.stderr)
        return False
    # Test-first TDD: the routine is frozen by hash as soon as it runs and resolves
    # unambiguously, whether it currently passes or fails. A stamped-but-failing leaf
    # rolls up as test_failed until the covering implementation lands and turns it green.
    if item.is_leaf:
        item.tests = {name: syngatelib.routine_sha(loc) for name, loc in resolved.items()}
    item.reviewed = syngatelib.item_stamp(items, uid)
    syngatelib.write_item(item)
    state = "passing" if passed else "FAILING -- red, pending implementation"
    print(f"{uid}: reviewed ({item.reviewed}) -- bound test currently {state}")
    return True


def cmd_review(args):
    items = _load_or_die()
    if not _require(items, "review"):
        return 1
    stampable = _addressable(items)
    uids, errors = _expand_uids(items, args.uid, stampable, "leaf")
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    structural = syngatelib.validate_structure(items)
    discovered = syngatelib.discover_bindings()
    with _Recording(args.coverage_out) as recording:
        failed = [uid for uid in uids if not _review_one(items, structural, discovered, uid, args.build_dir, recording, stampable)]
    if failed:
        print(f"review: {len(failed)}/{len(uids)} item(s) not stamped: {' '.join(failed)}", file=sys.stderr)
        return 1
    if len(uids) > 1:
        print(f"review: stamped {len(uids)} item(s)")
    return 0


def cmd_test(args):
    """Runs whichever way the test switch sits: off only means the tree does not
    enforce that its items carry tests, so the routines tagged for them still
    run and still record coverage. Stamping is `review`'s and stays gated."""
    items = _load_or_die()
    tests_on = syngatelib.features(items)["test"]
    narrowed, errors = {}, []
    for arg in args.uid:
        pattern, _, name = arg.partition(":")
        matched, bad = _expand_uids(items, [pattern], _addressable(items), "leaf")
        errors += bad
        for uid in matched:
            narrowed.setdefault(uid, [])
            if name:
                narrowed[uid].append(name)
    uids = list(narrowed)
    discovered = syngatelib.discover_bindings()
    resolved, unresolved = {}, {}
    for uid in uids:
        item = items.get(uid)
        if item is None:
            errors.append(f"unknown UID '{uid}'")
            continue
        names = _bindings_of(item, discovered, tests_on)
        unknown = [name for name in narrowed[uid] if name not in names]
        if unknown:
            errors.append(f"{uid}: no binding named {', '.join(unknown)}")
            continue
        names = narrowed[uid] or names
        if not names:
            # With the switch off the tree never claimed the item has a test, so
            # having none is nothing to report; with it on, it binds none by mistake.
            if tests_on:
                errors.append(f"{uid}: a branch binds no tests of its own")
            continue
        resolved[uid], missing = _resolve(item, discovered, names)
        unresolved.update({(uid, name): why for name, why in missing.items()})
    with _Recording(args.coverage_out) as recording:
        passed, unrun = _run_bound(resolved, args.build_dir)
        unrun.update(unresolved)
        for (uid, name), why in unrun.items():
            recording.unrun(uid, name, why)
    for line in errors:
        print(line, file=sys.stderr)
    broken = {uid for uid, _ in unrun}
    print(f"test: {len(resolved) - len(broken)}/{len(uids)} item(s) run -- {'passed' if passed and not unrun else 'FAILED'}")
    return 0 if passed and not errors and not unrun else 1


def cmd_clear(args):
    items = _load_or_die()
    if not _require(items, "review"):
        return 1
    uids, errors = _expand_uids(items, args.uid, lambda item: bool(item.reviewed), "reviewed")
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    ret = 0
    for uid in uids:
        item = items.get(uid)
        if item is None:
            print(f"unknown UID '{uid}'", file=sys.stderr)
            ret = 1
            continue
        if not item.reviewed:
            print(f"{uid}: not reviewed")
            continue
        syngatelib.clear_review(item)
        syngatelib.write_item(item)
        print(f"{uid}: review stamp cleared")
    return ret


def cmd_validate(args):
    items, errors = syngatelib.load_tree()
    errors.extend(syngatelib.validate_structure(items))
    discovered = syngatelib.discover_bindings()
    errors.extend(syngatelib.check_frozen(items, discovered))
    if args.coverage:
        records, coverage_errors = syngatelib.load_coverage(args.coverage)
        errors.extend(coverage_errors)
        errors.extend(syngatelib.check_coverage(items, records))
    if args.strict:
        errors.extend(f"{uid}: not reviewed (strict mode)" for uid, item in sorted(items.items()) if not item.reviewed)
    pending = syngatelib.check_bindings_exist(items, discovered)
    if pending:
        print(f"validate: {len(pending)} unreviewed binding(s) without a tagged routine yet (pending, non-fatal)")
    if errors:
        print(f"validate: {len(errors)} error(s):", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"validate: OK ({len(items)} items)")
    return 0


def cmd_report(args):
    items = _load_or_die()
    records, coverage_errors = syngatelib.load_coverage(args.coverage)
    for line in coverage_errors:
        print(f"warning: {line}", file=sys.stderr)
    # Validation problems land on the items that own them, not on whichever
    # item's tooling found them.
    problems = syngatelib.item_problems(items, syngatelib.discover_bindings())
    report = syngatelib.compute_status(items, records, problems)
    out = Path(args.out)
    import json
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    counts = {}
    for entry in report.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(f"wrote {out} -- {counts}")
    if args.html:
        import render_syngate_report
        render_syngate_report.run(out, Path(args.html))
        print(f"rendered site to {args.html}")
    return 0


def cmd_ui(args):
    from . import syngate_ui
    return syngate_ui.serve(port=args.port, coverage=args.coverage, build_dir=args.build_dir, open_browser=not args.no_browser, root=syngatelib.ROOT)


def main():
    parser = argparse.ArgumentParser(prog="syngate", description=__doc__)
    parser.add_argument("--root", help="managed project root (default: SYNGATE_ROOT, else the nearest ancestor of the working directory holding syngate/)")
    sub = parser.add_subparsers(dest="command", required=True)

    def mutation(name, help):
        """A tree call: `--clear-review` is the user standing behind the change, dropping
        the stamps it moves. Omitted -- as when proxying an AI answer -- those stamps are
        left over changed content and the review reads as violated."""
        p = sub.add_parser(name, help=help)
        p.add_argument("uid")
        p.add_argument("--clear-review", action="store_true",
                       help="drop the review stamp of every item this call moves, instead of leaving it violated")
        return p

    p = mutation("add", "scaffold a syngate item")
    p.add_argument("--parent", action="append", required=True)
    p.add_argument("--dir", help="folder under the repo root, e.g. syngate/infra")
    p.add_argument("--order", type=int, default=0)
    p.add_argument("--header")
    p.add_argument("--description")
    p.add_argument("--branch", action="store_true", help="carry no test binding of its own")
    p.set_defaults(func=cmd_add)

    p = mutation("edit", "change an item's header, description or sibling order")
    p.add_argument("--header")
    p.add_argument("--description")
    p.add_argument("--order", type=int)
    p.set_defaults(func=cmd_edit)

    p = mutation("move", "add and remove an item's parent links, and place it among its siblings")
    p.add_argument("--to", required=True, help="the parent to place it under")
    p.add_argument("--before", help="sibling to land in front of (default: last)")
    p.add_argument("--from", dest="source", help="the parent link to re-point, or -- with --to another parent -- the link to drop")
    p.add_argument("--link", action="store_true", help="add --to as one more parent instead of re-pointing")
    p.set_defaults(func=cmd_move)

    p = mutation("delete", "remove a childless item")
    p.set_defaults(func=cmd_delete)

    for name, help, func in (("query", "print an item as JSON: header, description, parents, children and kind", cmd_query),
                             ("context", "print the seed AI-context of an item as an AI call anchored at it is formed", cmd_context)):
        p = sub.add_parser(name, help=help)
        p.add_argument("uid")
        p.set_defaults(func=func)

    p = sub.add_parser("review", help="user-only: run bound tests, stamp routine shas + reviewed (stamps even on a failing test -- TDD red state)")
    p.add_argument("uid", nargs="+", help="UID(s) or glob pattern(s) like 'BUOY-00?' / 'BUOY-*' (quote patterns for the shell); patterns select leaves only")
    p.add_argument("--build-dir", default="cmake-build-debug-clang")
    p.add_argument("--coverage-out", help="coverage JSONL to fold the run's records into; a re-run binding supersedes its previous records")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("test", help="run the routines bound to leaf items, without stamping")
    p.add_argument("uid", nargs="+", help="UID(s) or glob pattern(s); patterns select leaves only")
    p.add_argument("--build-dir", default="cmake-build-debug-clang")
    p.add_argument("--coverage-out", help="coverage JSONL to fold the run's records into; a re-run binding supersedes its previous records")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("clear", help="user-only: drop the reviewed stamp")
    p.add_argument("uid", nargs="+", help="UID(s) or glob pattern(s); patterns select reviewed items only")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("validate", help="structural + frozen + coverage checks (CI gate)")
    p.add_argument("--coverage", action="append", default=[], help="syngate_coverage.jsonl file(s); repeatable")
    p.add_argument("--strict", action="store_true", help="require every item reviewed")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("report", help="recursive status rollup + optional HTML site")
    p.add_argument("--coverage", action="append", default=[], help="syngate_coverage.jsonl file(s); repeatable")
    p.add_argument("--out", default="syngate_status.json")
    p.add_argument("--html", help="output directory for the static site")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ui", help="serve the local tree editor in the browser (loopback + session token)")
    p.add_argument("--port", type=int, default=8712, help="listen port on 127.0.0.1 (default 8712, 0 = ephemeral)")
    p.add_argument("--coverage", action="append", default=[], help="syngate_coverage.jsonl file(s) to color statuses; repeatable (default: well-known local files)")
    p.add_argument("--build-dir", default="cmake-build-debug-clang", help="build tree with the Catch2 test binaries for review runs")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    syngatelib.set_root(args.root)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
