# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Tests for syngatelib.load_tree + validate_structure tree-shape semantics -- [INFRA-030]."""

import pytest
import yaml

import syngatelib


def _errors(syngate_dir):
    items, load_errors = syngatelib.load_tree(syngate_dir)
    return items, load_errors, syngatelib.validate_structure(items)


def _matching(errors, needle):
    return [e for e in errors if needle in e]


def _seed_minimal(syngate_dir, make_item):
    """One root branch with a single valid test-bearing leaf child."""
    make_item(syngate_dir, "ROOT-1", "the product shall exist", header="root")
    make_item(syngate_dir, "LEAF-1", "the leaf shall pass", parents=["ROOT-1"], tests=None)


def test_any_yaml_is_an_item_uid_is_stem_folders_meaningless(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall be here", header="root")
    make_item(syngate_dir / "deep" / "nested", "LEAF-1", "the leaf shall pass", parents=["ROOT-1"], tests=None)
    items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == []
    assert set(items) == {"ROOT-1", "LEAF-1"}
    assert items["LEAF-1"].uid == "LEAF-1"
    assert errors == []


def test_bare_feature_name_without_numbered_suffix_is_a_valid_uid(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall be here", header="root")
    make_item(syngate_dir, "EXCHANGE", "exchange integrations shall be grouped here", parents=["ROOT-1"])
    make_item(syngate_dir, "EXCHANGE-1", "the leaf shall pass", parents=["EXCHANGE"], tests=None)
    _items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == []
    assert errors == []


def test_duplicate_stem_across_folders_is_rejected(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir / "a", "ROOT-1", "root shall be one", header="root")
    make_item(syngate_dir / "b", "ROOT-1", "root shall be two", header="dup")
    _items, load_errors, _errs = _errors(syngate_dir)
    assert _matching(load_errors, "duplicate UID")


def test_unknown_field_is_rejected(syngate_tree):
    syngate_dir, make_item = syngate_tree
    _seed_minimal(syngate_dir, make_item)
    (syngate_dir / "LEAF-1.yml").write_text(
        yaml.safe_dump({"header": "x", "description": "the leaf shall pass", "parents": ["ROOT-1"], "tests": None, "bogus": 1}),
        encoding="utf-8",
    )
    _items, load_errors, _errs = _errors(syngate_dir)
    assert _matching(load_errors, "unknown field 'bogus'")


def test_unknown_parent_is_rejected(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root")
    make_item(syngate_dir, "LEAF-1", "the leaf shall pass", parents=["MISSING-9"], tests=None)
    _items, _load_errors, errors = _errors(syngate_dir)
    assert _matching(errors, "unknown parent 'MISSING-9'")


def test_exactly_one_root_required(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root a shall exist", header="a")
    make_item(syngate_dir, "ROOT-2", "root b shall exist", header="b")
    _items, _load_errors, errors = _errors(syngate_dir)
    assert _matching(errors, "expected exactly one root")


def test_multi_parent_dag_is_allowed(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root")
    make_item(syngate_dir, "A-1", "branch a shall exist", parents=["ROOT-1"])
    make_item(syngate_dir, "B-1", "branch b shall exist", parents=["ROOT-1"])
    make_item(syngate_dir, "C-1", "the leaf shall pass", parents=["A-1", "B-1"], tests=None)
    _items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == []
    assert errors == []


def test_cycle_is_rejected(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall pass", header="root", tests=None)
    make_item(syngate_dir, "X-1", "branch x shall exist", parents=["Y-1"])
    make_item(syngate_dir, "Y-1", "branch y shall exist", parents=["X-1"])
    _items, _load_errors, errors = _errors(syngate_dir)
    assert _matching(errors, "cycle")


@pytest.mark.syngate("SYNGATE-020")
def test_a_branch_passes_only_with_its_own_tests_and_its_children(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall pass", header="root", tests=None)
    make_item(syngate_dir, "LEAF-1", "the leaf shall pass", parents=["ROOT-1"], tests=None)
    items, load_errors, errors = _errors(syngate_dir)
    assert not load_errors and not errors
    passed = [{"passed": True, "name": "", "log": ""}]

    def test_axis(records):
        return syngatelib.compute_axes(items, records)["ROOT-1"]["test"]

    assert test_axis({("LEAF-1", None): passed}) == "unknown"  # the branch's own binding never ran
    assert test_axis({("ROOT-1", None): passed}) == "unknown"
    assert test_axis({("ROOT-1", None): passed, ("LEAF-1", None): passed}) == "test_passed"
    assert test_axis({("ROOT-1", None): [{"passed": False, "name": "", "log": ""}], ("LEAF-1", None): passed}) == "test_failed"
    assert syngatelib.compute_status(items, {("LEAF-1", None): passed})["ROOT-1"]["status"] == "partially_implemented"
    items["ROOT-1"].tests = {None: "a" * 64}
    items["ROOT-1"].reviewed = syngatelib.compute_stamp(items["ROOT-1"])
    assert syngatelib.compute_axes(items, {})["ROOT-1"]["review"] == "not_reviewed"  # LEAF-1 still is not
    problems = syngatelib.item_problems(items, {})  # the stamped routine is gone
    assert list(problems) == ["ROOT-1"]
    axes = syngatelib.compute_axes(items, {("ROOT-1", None): passed, ("LEAF-1", None): passed}, problems)["ROOT-1"]
    assert axes == {"test": "test_passed", "review": "review_violated"}  # a violated review never touches the test axis


def test_childless_item_without_tests_is_structurally_valid(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root")
    make_item(syngate_dir, "STUB-1", "not planned yet", parents=["ROOT-1"])
    _items, _load_errors, errors = _errors(syngate_dir)
    assert errors == []


def test_leaf_description_wording_is_unconstrained(syngate_tree):
    """No 'shall' wording rule: a wording nit is not a review-status or test
    failure, so it must not redden an item."""
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root")
    make_item(syngate_dir, "LEAF-1", "the leaf just passes, no matter the wording", parents=["ROOT-1"], tests=None)
    _items, _load_errors, errors = _errors(syngate_dir)
    assert errors == []


@pytest.mark.syngate("INFRA-071")
def test_malformed_reviewed_stamp_reads_as_absent(syngate_tree):
    syngate_dir, make_item = syngate_tree
    _seed_minimal(syngate_dir, make_item)
    make_item(syngate_dir, "ROOT-1", "the product shall exist", header="root", reviewed="not-a-sha")
    items, load_errors, _errs = _errors(syngate_dir)
    assert not _matching(load_errors, "review")
    assert items["ROOT-1"].reviewed is None


def test_reviewed_stamp_must_match_computed_content(syngate_tree):
    syngate_dir, make_item = syngate_tree
    _seed_minimal(syngate_dir, make_item)
    items, _load_errors = syngatelib.load_tree(syngate_dir)
    good = syngatelib.item_stamp(items, "ROOT-1")  # ROOT-1 has a child, and the child set is inside the stamp

    make_item(syngate_dir, "ROOT-1", "the product shall exist", header="root", reviewed=good)
    _items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == []
    assert _matching(errors, "reviewed") == []

    make_item(syngate_dir, "ROOT-1", "the product shall exist", header="root", reviewed="0" * 64)
    _items, _load_errors, errors = _errors(syngate_dir)
    assert _matching(errors, "reviewed stamp does not match")


def test_review_state_is_attributed_to_the_item_that_carries_it(syngate_tree):
    """A stale stamp is a defect of its own item: it is a review problem, never a
    layout one, so it cannot redden whatever item covers the layout."""
    syngate_dir, make_item = syngate_tree
    _seed_minimal(syngate_dir, make_item)
    make_item(syngate_dir, "ROOT-1", "the product shall exist", header="root", reviewed="0" * 64)
    items, load_errors = syngatelib.load_tree(syngate_dir)
    assert load_errors == []

    assert syngatelib.validate_layout(items) == []
    assert _matching(syngatelib.validate_structure(items), "reviewed stamp does not match")
    assert _matching(syngatelib.item_problems(items)["ROOT-1"], "reviewed stamp does not match")
    assert "LEAF-1" not in syngatelib.item_problems(items)


@pytest.mark.syngate("INFRA-030")
def test_live_syngate_tree_has_a_valid_layout():
    """The real syngate/ tree loads and satisfies the layout this item
    describes: UIDs are file stems and `parents` forms a DAG with one root.

    Review state is deliberately out of scope here. A stale stamp or a drifted
    frozen routine is the tree's data; the status rollup reddens the item that
    carries it. Detecting such a violation is this item working, so
    asserting the tree is clean here would redden INFRA for another item's fault.
    """
    items, load_errors = syngatelib.load_tree()
    assert load_errors == []
    assert syngatelib.validate_layout(items) == []


@pytest.mark.syngate("SYNGATE-010")
def test_walk_selects_ancestors_root_first_then_descendants_each_uid_once(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root", header="root")
    make_item(syngate_dir, "SIDE-B", "side b", parents=["ROOT-1"], order=20)
    make_item(syngate_dir, "SIDE-A", "side a", parents=["ROOT-1"], order=10)
    make_item(syngate_dir, "MID-1", "mid", parents=["SIDE-B", "SIDE-A"])
    make_item(syngate_dir, "LEAF-2", "the leaf shall two", parents=["MID-1"], order=20, tests=None)
    make_item(syngate_dir, "LEAF-1", "the leaf shall one", parents=["MID-1"], order=10, tests=None)
    items, _ = syngatelib.load_tree(syngate_dir)
    assert syngatelib.walk(items, "MID-1") == ["ROOT-1", "SIDE-B", "SIDE-A", "MID-1"]
    assert syngatelib.walk(items, "MID-1", descendants=True) == ["ROOT-1", "SIDE-B", "SIDE-A", "MID-1", "LEAF-1", "LEAF-2"]
    assert syngatelib.walk(items, "LEAF-2") == ["ROOT-1", "SIDE-B", "SIDE-A", "MID-1", "LEAF-2"]
    assert syngatelib.walk(items) == ["ROOT-1", "SIDE-A", "MID-1", "LEAF-1", "LEAF-2", "SIDE-B"]


@pytest.mark.syngate("SYNGATE-030")
def test_the_root_switches_the_test_and_review_features_both_off_when_absent(syngate_tree):
    syngate_dir, make_item = syngate_tree
    (syngate_dir / "ROOT-1.yml").write_text(yaml.safe_dump({"header": "root", "description": "root shall exist", "parents": []}), encoding="utf-8")
    make_item(syngate_dir, "LEAF-1", "the leaf shall pass", parents=["ROOT-1"], tests=None)
    make_item(syngate_dir, "STAMPED-1", "approved", parents=["ROOT-1"], reviewed="0" * 64)
    items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == [] and syngatelib.features(items) == {"test": False, "review": False}
    assert _matching(errors, "LEAF-1: test bindings while the tree's test feature is off")
    assert _matching(errors, "STAMPED-1: review stamp while the tree's review feature is off")
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root", features=("test",))
    items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == [] and syngatelib.features(items) == {"test": True, "review": False}
    assert _matching(errors, "feature is off") == ["STAMPED-1: review stamp while the tree's review feature is off (root item: 'review: enabled')"]
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root", tests=None, reviewed="0" * 64)  # the root's own binding and stamp switch both on
    items, load_errors, errors = _errors(syngate_dir)
    assert load_errors == [] and syngatelib.features(items) == {"test": True, "review": True} and not _matching(errors, "feature is off")
    make_item(syngate_dir, "LEAF-1", "the leaf shall pass", parents=["ROOT-1"], tests="enabled")
    _items, load_errors, _errs = _errors(syngate_dir)
    assert _matching(load_errors, "LEAF-1: 'test: enabled' switches the feature on the root item only")


@pytest.mark.syngate("SYNGATE-040")
def test_bindings_and_stamp_are_read_from_test_and_review_and_their_old_spellings_and_written_back_new(syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT-1", "root shall exist", header="root")
    sha, stamp = "a" * 64, "b" * 64
    (syngate_dir / "OLD-1.yml").write_text(yaml.safe_dump({"header": "old", "description": "old shall bind", "parents": ["ROOT-1"], "tests": {"a": sha}, "reviewed": stamp}), encoding="utf-8")
    (syngate_dir / "NEW-1.yml").write_text(yaml.safe_dump({"header": "new", "description": "new shall bind", "parents": ["ROOT-1"], "test": {"a": sha}, "review": stamp}), encoding="utf-8")
    (syngate_dir / "BOTH-1.yml").write_text(yaml.safe_dump({"header": "both", "description": "both shall not", "parents": ["ROOT-1"], "test": None, "tests": None}), encoding="utf-8")
    items, load_errors = syngatelib.load_tree(syngate_dir)
    assert load_errors == ["BOTH-1: both 'test' and its old spelling 'tests'"]
    assert (items["OLD-1"].tests, items["OLD-1"].reviewed) == ({"a": sha}, stamp) == (items["NEW-1"].tests, items["NEW-1"].reviewed)
    assert yaml.safe_load(syngatelib.dump_item(items["OLD-1"])) == {"header": "old", "description": "old shall bind\n", "parents": ["ROOT-1"], "test": {"a": sha}, "review": stamp}
    assert yaml.safe_load(syngatelib.dump_item(items["ROOT-1"])) == {"header": "root", "description": "root shall exist\n", "parents": [], "test": "enabled", "review": "enabled"}
    items["ROOT-1"].features["review"] = False
    assert yaml.safe_load(syngatelib.dump_item(items["ROOT-1"]))["review"] == "disabled"
