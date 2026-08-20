---
description: Write a delegation brief for a task and run it on the local opencode runtime
argument-hint: "[--dir <path>] <what you want done>"
---

The user wants this delegated to the local opencode runtime:

$ARGUMENTS

Do the judgment work the runner agent cannot do, then hand it off.

1. **Sanity-check the trade.** A delegated run costs process startup plus a fresh agent
   reading its way into a codebase it knows nothing about. If this is one Read and one Edit,
   say so and just do it yourself.
2. **Scope a `--dir`.** The delegate runs with full read/write/bash inside it. Pick the
   narrowest directory that still contains the work. If the user named one, use theirs.
3. **Write the brief.** Use the structure in the `delegate-local` skill — Objective, Context,
   Constraints, Deliverable — and close with "Wrap the final answer in `<result></result>`".
   The delegate gets your text and a working directory and nothing else: no conversation
   history, no ability to ask you a question. Everything it needs goes in the brief.
4. **Spawn `delegate-local:runner`** with the finished brief and the `--dir`. It runs in the
   background; you will get a compact report back. If the work splits cleanly across
   independent directories, spawn one runner per directory — never two runners on one tree,
   they run concurrently and will clobber each other.
