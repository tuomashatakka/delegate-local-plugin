---
description: Show in-flight delegations and check the local opencode runtime
allowed-tools: Bash(delegate-local:*)
---

Report on the local delegation runtime.

- Recent and in-flight runs: !`delegate-local list`
- Runtime health: !`delegate-local doctor`

Summarise briefly: which runs are still `running` and how long they have been going, which
finished and how, and whether `doctor` shows a reachable model. Flag anything stuck.

To collect a finished run, spawn `delegate-local:runner` — do not paste raw result JSON here.
