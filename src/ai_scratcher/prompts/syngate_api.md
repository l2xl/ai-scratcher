---
kind: tool
description: Read and change the syngate tree and run the tests bound to its items
---
# Syngate API
The item files under syngate/ are read-only for you: the tree is read and changed only through these calls.
```syngate
[{"tool": "query", "uid": "@"},
 {"tool": "context", "uid": "AREA-010"},
 {"tool": "add", "uid": "AREA-030", "parent": "@", "header": "…", "description": "…", "kind": "leaf"},
 {"tool": "edit", "uid": "@", "header": "…", "description": "…"},
 {"tool": "remove", "uid": "AREA-020"},
 {"tool": "move", "uid": "AREA-010", "to": "AREA", "before": "AREA-030"},
 {"tool": "test", "uid": "AREA-010", "name": "binding"}]
```
"@" stands for the anchored item; any other item is addressed by its UID, the stem of its file under syngate/.
- query: read an item back — header, description, parents, children in sibling order, kind. Query the parent before adding under it, to pick a free UID and the place among its children.
- context: read the seed context of an item, as an exchange anchored at it is seeded.
- add: a new item under `parent` (default "@") with `header` and `description`; `kind` "leaf" (default) carries a test binding, "branch" does not.
- edit: the `header` and/or `description` of an item.
- remove: a childless item.
- move: under parent `to`, in front of sibling `before` (default last). `from` names the parent link to re-point when the item has several; `link` true adds `to` as one more parent instead of re-pointing, and a `from` that is a parent while `to` already is another drops the `from` link.
- test: run the routines bound to the leaves under the item (a UID or a glob), `name` narrowing a leaf to one binding. Every test call of a batch joins one run, performed after the other calls; a run already live elsewhere refuses it. The outcome comes back as one line per leaf binding with pass/fail, then the output of every failed routine. Review stamping is the user's act: there is no review call.
A call of yours never clears a review stamp: every stamp it moves -- the item's own, and its parents' where their child set changed -- is left standing over content that no longer matches it, so the change shows up as a violated review for the user to settle. Only the user's own edit in the page clears a stamp.
