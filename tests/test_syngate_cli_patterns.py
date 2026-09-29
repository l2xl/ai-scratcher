# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""`syngate review` / `syngate clear` accept several UIDs and glob patterns
(`BUOY-00?`, `BUOY-*`), expanding patterns against the loaded tree."""

import argparse
import sys
from pathlib import Path

import pytest

import syngate
import syngatelib


def _leaf(uid, reviewed=None):
    return syngatelib.Item(uid=uid, path=Path(f"{uid}.yml"), header=uid, description="x shall y\n", parents=["PAT"], tests={None: None}, reviewed=reviewed)


def _tree(test=True, review=True):
    branch = syngatelib.Item(uid="PAT", path=Path("PAT.yml"), header="Pattern", description="branch\n", parents=[], features={"test": test, "review": review})
    items = {"PAT": branch}
    for n in range(1, 4):
        uid = f"PAT-00{n}"
        items[uid] = _leaf(uid)
    return items


@pytest.mark.syngate("INFRA-051", "leaves_only")
def test_pattern_expands_to_matching_leaves_only():
    items = _tree()
    uids, errors = syngate._expand_uids(items, ["PAT-00?"], lambda item: item.is_leaf, "leaf")
    assert uids == ["PAT-001", "PAT-002", "PAT-003"]
    assert errors == []


@pytest.mark.syngate("INFRA-051", "branch_skip")
def test_star_pattern_skips_non_selectable_branch():
    items = _tree()
    uids, errors = syngate._expand_uids(items, ["PAT*"], lambda item: item.is_leaf, "leaf")
    assert uids == ["PAT-001", "PAT-002", "PAT-003"]
    assert errors == []


@pytest.mark.syngate("INFRA-051", "literal_passthrough")
def test_literals_pass_through_for_downstream_diagnostics():
    items = _tree()
    uids, errors = syngate._expand_uids(items, ["PAT", "NOPE-001"], lambda item: item.is_leaf, "leaf")
    assert uids == ["PAT", "NOPE-001"]
    assert errors == []


@pytest.mark.syngate("INFRA-051", "no_match_error")
def test_unmatched_pattern_is_an_error():
    items = _tree()
    uids, errors = syngate._expand_uids(items, ["ZZZ-*"], lambda item: item.is_leaf, "leaf")
    assert uids == []
    assert errors == ["pattern 'ZZZ-*' matches no leaf item"]


@pytest.mark.syngate("INFRA-051", "dedup")
def test_mixed_literals_and_patterns_deduplicate_preserving_order():
    items = _tree()
    uids, errors = syngate._expand_uids(items, ["PAT-002", "PAT-00?"], lambda item: item.is_leaf, "leaf")
    assert uids == ["PAT-002", "PAT-001", "PAT-003"]
    assert errors == []


@pytest.mark.syngate("INFRA-051", "clear_batch")
def test_clear_with_pattern_touches_only_reviewed_items(monkeypatch):
    items = _tree()
    items["PAT-001"] = _leaf("PAT-001", reviewed="a" * 64)
    items["PAT-003"] = _leaf("PAT-003", reviewed="b" * 64)
    written = []
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "write_item", written.append)
    assert syngate.cmd_clear(argparse.Namespace(uid=["PAT-*"])) == 0
    assert [item.uid for item in written] == ["PAT-001", "PAT-003"]
    assert all(item.reviewed is None for item in written)


@pytest.mark.syngate("INFRA-051", "clear_unknown_literal")
def test_clear_with_unknown_literal_fails_after_processing_the_rest(monkeypatch):
    items = _tree()
    items["PAT-002"] = _leaf("PAT-002", reviewed="c" * 64)
    written = []
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "write_item", written.append)
    assert syngate.cmd_clear(argparse.Namespace(uid=["NOPE-001", "PAT-002"])) == 1
    assert [item.uid for item in written] == ["PAT-002"]


@pytest.mark.syngate("INFRA-052", "selection")
def test_bound_routines_are_selected_by_node_id_and_by_tag_pairs(monkeypatch, tmp_path):
    items = _tree()
    items["PAT-003"].tests = {"a": None, "b": None}
    located = {
        ("PAT-001", None): [syngatelib.Location("scripts/tests/test_pat.py", 1, "scripts/tests/test_pat.py::test_one", "")],
        ("PAT-002", None): [syngatelib.Location("test/pat/test_pat.cpp", 1, "test/pat/test_pat.cpp:1", "")],
        ("PAT-003", "a"): [syngatelib.Location("test/pat/test_pat.cpp", 9, "test/pat/test_pat.cpp:9", "")],
        ("PAT-003", "b"): [syngatelib.Location("test/pat/test_pat.cpp", 17, "test/pat/test_pat.cpp:17", "")],
    }
    (tmp_path / "test_pat").touch()
    runs, written = [], []
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "discover_bindings", lambda: located)
    monkeypatch.setattr(syngatelib, "write_item", written.append)
    monkeypatch.setattr(syngate.subprocess, "run", lambda argv, **kwargs: runs.append(argv) or argparse.Namespace(returncode=0))
    assert syngate.cmd_test(argparse.Namespace(uid=["PAT-00?"], build_dir=str(tmp_path), coverage_out=None)) == 0
    assert runs == [[sys.executable, "-m", "pytest", "scripts/tests/test_pat.py::test_one"], [str(tmp_path / "test_pat"), "[PAT-002],[PAT-003][a],[PAT-003][b]"]]
    assert written == []


@pytest.mark.syngate("INFRA-052", "unrun_record")
def test_a_binding_without_a_routine_is_recorded_as_failed(monkeypatch, tmp_path):
    items = _tree()
    coverage = tmp_path / "coverage.jsonl"
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "discover_bindings", lambda: {})
    assert syngate.cmd_test(argparse.Namespace(uid=["PAT-001"], build_dir=str(tmp_path), coverage_out=str(coverage))) == 1
    records, errors = syngatelib.load_coverage([coverage])
    assert errors == [] and list(records) == [("PAT-001", None)]
    assert records[("PAT-001", None)][0]["passed"] is False
    assert "must match exactly one routine" in records[("PAT-001", None)][0]["log"]


def _option_at_root(syngate_dir, option):
    """The feature switch read off a root file carrying `option` (a `key: value` line, or nothing)."""
    (syngate_dir / "PAT.yml").write_text(f"header: Pattern\ndescription: branch\nparents: []\n{option}", encoding="utf-8")
    return syngatelib.features(syngatelib.load_tree(syngate_dir)[0])


_load_tree = syngatelib.load_tree  # the tests below monkeypatch the module's own


def _rooted(syngate_tree, features, tests="absent"):
    """A file-backed tree: root PAT switching `features`, childless PAT-001 with `tests`."""
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "PAT", "branch\n", header="Pattern", features=features)
    make_item(syngate_dir, "PAT-001", "x shall y\n", parents=("PAT",), tests=tests)
    return _load_tree(syngate_dir)[0]


@pytest.mark.syngate("SYNGATE-TEST-OPT")
def test_the_test_option_at_the_root_withholds_bindings_and_stamping_alone(monkeypatch, capsys, tmp_path, syngate_tree):
    syngate_dir, _ = syngate_tree
    assert _option_at_root(syngate_dir, "test: enabled\n")["test"] is True
    assert _option_at_root(syngate_dir, "test: disabled\n")["test"] is False
    assert _option_at_root(syngate_dir, "")["test"] is False
    review = argparse.Namespace(uid=["PAT-001"], build_dir=str(tmp_path), coverage_out=None)
    items = _rooted(syngate_tree, features=("review",))
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "discover_bindings", lambda: {})
    assert syngate.cmd_test(argparse.Namespace(uid=["PAT-001"], build_dir=str(tmp_path), coverage_out=None)) == 0  # the run is never closed
    written = []
    monkeypatch.setattr(syngatelib, "write_item", written.append)
    assert syngate.cmd_review(review) == 0  # a childless item stamps without any routine
    assert [(item.uid, item.tests, item.reviewed == syngatelib.compute_stamp(item)) for item in written] == [("PAT-001", None, True)]
    assert "feature is off" not in capsys.readouterr().err
    items = _rooted(syngate_tree, features=("test", "review"))
    assert syngate.cmd_review(review) == 1  # with the option on, an item without a binding of its own is reviewed through its children only
    assert "nothing to stamp" in capsys.readouterr().err


@pytest.mark.syngate("SYNGATE-REVIEW-OPT")
def test_the_review_option_at_the_root_closes_the_stamping_commands(monkeypatch, capsys, tmp_path, syngate_tree):
    syngate_dir, _ = syngate_tree
    assert _option_at_root(syngate_dir, "review: enabled\n")["review"] is True
    assert _option_at_root(syngate_dir, "review: disabled\n")["review"] is False
    assert _option_at_root(syngate_dir, "")["review"] is False
    items = _rooted(syngate_tree, features=("test",), tests=None)
    monkeypatch.setattr(syngatelib, "load_tree", lambda: (items, []))
    monkeypatch.setattr(syngatelib, "discover_bindings", lambda: {})
    assert syngate.cmd_review(argparse.Namespace(uid=["PAT-001"], build_dir=str(tmp_path), coverage_out=None)) == 1
    assert syngate.cmd_clear(argparse.Namespace(uid=["PAT-001"])) == 1
    assert capsys.readouterr().err.count("the tree's review feature is off; switch it on with 'review: enabled' in the root item") == 2
    items = _rooted(syngate_tree, features=("test", "review"), tests=None)
    items["PAT-001"].reviewed = syngatelib.item_stamp(items, "PAT-001")
    written = []
    monkeypatch.setattr(syngatelib, "write_item", written.append)
    assert syngate.cmd_clear(argparse.Namespace(uid=["PAT-001"])) == 0  # with the option on, the stamp is dropped
    assert [(item.uid, item.reviewed) for item in written] == [("PAT-001", None)]
