---
kind: skill
description: How an answer asks the harness to run tools and how their results come back
default: true
---
# Tool calls
An answer is either plain text or exactly one list of tool calls and nothing else, never both. A structured answer is the object `{"kind": "text", "text": "…"}` or `{"kind": "calls", "calls": [...]}`; a plain one carries the list in a single fenced block tagged `syngate`:
```syngate
[{"tool": "query", "uid": "@"},
 {"tool": "skill", "name": "syngate"}]
```
Every call is an object whose `tool` names one of the tools defined below, its other keys being the fields of that tool. The calls are applied in order and the first refused one stops the batch; what was applied before the refusal is reported with it.
What a call read or ran is not your answer but your next prompt: the harness places the results of the batch into the same session as the next message and the exchange goes on with your next answer, for a bounded number of rounds.
Subagents and workflows you launch are awaited: the turn lasts until they have reported and you have answered again, and only that last answer counts. A shell command left running in the background is killed when the turn ends, so await it instead.
