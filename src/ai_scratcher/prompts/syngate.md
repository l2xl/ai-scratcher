---
kind: skill
description: The rules a syngate DAG and its items follow
default: true
---
# Syngate DAG rules
The syngate items form a tree or, in the general case, a directed acyclic graph (DAG): a condensed record of the analysis and synthesis of the project, which sets the abstraction level and the scope of the AI context, the test coverage and the sustainability a human-controlled review provides. The seed context of an exchange anchored at an item is the chain of its ancestors' statements, root first, then its own. For the process to stay effective the items follow these rules:
- Every leaf is one simple testable statement about the project, read as a requirement. A statement that is not testable as a whole is split into children, each testable or split again.
- Complex behavioural design descriptions are kept out of the items, in the project's text or markdown files, and the item refers to such a file by a root-relative link.
- The project's tests bind to exact items by tags equal to the item UID. No item is created just to refer to a design document or to describe the tests of its parent.
- A header is a minimal noun phrase naming the behaviour; the description carries the contract, one "shall" on a test-bearing leaf.
- A whole feature is named without a number; its sub-items carry the feature's name with a numbered suffix, unless one of them is a large feature again.
