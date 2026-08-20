---
name: delegate-local
description: Delegate scoped tasks to a local opencode runtime as background subagents, using a spawn/poll/collect protocol over `opencode run --format json`. Use this whenever the user wants to offload, delegate, hand off, farm out, or background a task; run something with opencode or a local model (Ollama, Apple Foundation Model, LM Studio); fan work out across several files or directories in parallel; keep bulk grunt work out of the main context window; or asks "can another agent do this", "run this in the background", "spin up a local agent", or "use opencode for this". Also use when the user wants to check on, resume, or collect results from delegations already in flight. Covers when delegating is worth it, how to write the brief, and how to read the result; the `delegate-local:runner` agent is what actually executes one.
---

# Delegating to a local opencode runtime

`opencode` is a coding agent that runs on the user's machine. This skill wraps it as a
**background subagent**: you send a self-contained task brief, get a handle back
immediately, and collect a structured result later — the same shape as spawning a native
subagent, except the work happens in a separate process against whatever model the user's
opencode is configured with.

## Two ways in

**To run a delegation, spawn the `delegate-local:runner` agent** with a finished brief and a
`--dir`. It does the spawn, the blocking wait and the result projection inside its own
context, then reports back a verdict line plus the delegate's answer — the poll loop and the
raw event stream never reach you. It runs in the background, so the handle comes back
immediately. Fan out by spawning one runner per directory.

**Drive the CLI directly** for what the runner deliberately doesn't do: seeing what's in
flight, cancelling, and debugging. It is on `PATH` as `delegate-local`, and every subcommand
prints exactly one JSON document on stdout.

```
delegate-local <spawn|status|result|wait|send|list|cancel|logs|doctor>
```

The protocol below is what the runner executes. Read it when you're driving by hand, or
working out why a run behaved the way it did.

## Decide whether to delegate at all

This is the judgment call that matters most, and it cuts both ways.

**Delegation is worth it when** the work is bulky and mechanical (annotate 40 files, write
docstrings across a package), when it parallelises cleanly across independent directories,
when it would otherwise flood your context with output you only need a summary of, or when
the user explicitly asks for it.

**Delegation is a bad trade when** you could just do the thing. A delegated run carries
real fixed cost: process startup, a fresh agent reading its way into a codebase it knows
nothing about, and — if it's pointed at a small local model — genuinely slow inference.
Measured on this machine, `ollama/qwen3:4b` blew past 240 s on a task as trivial as listing
a directory. If the task is one Read and one Edit, doing it yourself is faster than
describing it to someone else. Delegating trivia is the main way this skill gets misused.

The delegate also **cannot ask you anything**. opencode creates `run` sessions with the
`question`, `plan_enter` and `plan_exit` permissions denied outright, so a brief that
depends on a clarification it can't request will simply fail or guess. Tasks needing
back-and-forth judgment belong with you.

## The protocol

```bash
# 1. spawn — returns instantly with a handle
delegate-local spawn --dir src/api "…task brief…"
# → {"run_id":"oc_20260816T180911_6745","status":"running","dir":"…","run_dir":"…"}

# 2. poll — cheap, no parsing of the event stream
delegate-local status oc_20260816T180911_6745
# → {"run_id":"…","status":"running","terminal":false,"session_id":"ses_…"}

# 3. collect — blocks until terminal, then prints the structured result
delegate-local wait oc_20260816T180911_6745
```

Run ids are timestamped and any unambiguous prefix works, so `delegate-local status oc_2026081
6T1809` resolves fine when you're poking at things by hand.

`wait` is the join primitive and takes several ids at once, which is how fan-out works:

```bash
a=$(delegate-local spawn --dir src/api "…" | jq -r .run_id)
b=$(delegate-local spawn --dir src/web "…" | jq -r .run_id)
c=$(delegate-local spawn --dir src/cli "…" | jq -r .run_id)
delegate-local wait "$a" "$b" "$c"      # → JSON array of three results
```

Give each parallel delegate its **own `--dir`** when you can. They run concurrently with
`--auto`, so two agents editing the same tree will happily clobber each other.

`delegate-local wait --all` joins everything currently in flight, and `delegate-local list` shows
recent runs with their status and lineage.

### Continuing a conversation

`send` posts a follow-up turn into the *same* opencode session — the analogue of messaging
a subagent that's already loaded the context:

```bash
delegate-local send <run_id> "That missed the error path in parse(). Handle it and re-report."
```

It reuses the parent's `--dir`, `--agent` and `--model`, records `parent_run_id`, and gets
its own run id and event log. Use it instead of a fresh `spawn` whenever the follow-up
depends on what the delegate already read — re-explaining a codebase from scratch is
exactly the cost you're trying to avoid. `--fork` branches the session instead, leaving the
original untouched.

## Writing the task brief

The brief is the entire interface. The delegate gets your text, a working directory, and
nothing else — no conversation history, no knowledge of what the user actually wants.
Since it can't come back with questions, everything it needs has to be in the brief.

This structure works well:

```markdown
## Objective
One sentence. What does "done" look like?

## Context
Where things live, what the code does, constraints a newcomer wouldn't infer.

## Constraints
- Work only inside <dir>
- Don't touch <thing>
- Don't commit or push

## Deliverable
What to report back, and in what shape.
Wrap the final answer in <result></result>.
```

That last line is worth including every time. The result parser looks for a `<result>`
block first and falls back to the last text part when there isn't one — so the tag isn't
required, it just makes extraction exact instead of approximate. When the answer is a
short verdict, a filename, or a list, the difference is real.

Pass long briefs by file or stdin rather than as an argument — no quoting to get wrong:

```bash
delegate-local spawn --dir src --prompt-file /tmp/brief.md
cat brief.md | delegate-local spawn --dir src
```

## Reading the result

```json
{
  "run_id": "oc_…", "session_id": "ses_…", "status": "done", "exit_code": 0,
  "result": "the delegate's final answer",
  "result_source": "result_block",
  "text_all": "every text part, joined",
  "tool_calls": [{"tool": "read", "status": "completed", "title": "…"}],
  "tokens": {"input": 8194, "output": 8, "total": 8202, "cache": {…}},
  "steps": 1, "errors": [], "duration_s": 12.3
}
```

`status` is one of `running`, `done`, `error`, `timeout`, `cancelled`.

Two fields deserve a glance before you trust the output. `result_source` tells you how the
answer was extracted — `result_block` means the delegate followed the contract, `last_text`
means it didn't and you're reading its final paragraph, which may be chatter rather than a
result. And `tool_calls` tells you whether it actually *did* anything: an empty array on a
task that was supposed to edit files means it talked about the work instead of doing it.
That failure is common with small local models and it looks like success from the outside.

## Choosing the model and agent

**Don't pass `--model` unless the user named one.** The user's opencode config already
specifies a default, and silently overriding it substitutes your judgment for theirs.

Pass `--model` when the user asks for a specific model, or when you're deliberately routing
cheap bulk work to a small local one (`ollama/…`) to save cost. `delegate-local doctor` lists
what's reachable.

`--agent` works the same way — forward it only when the user names an agent. One sharp
edge: opencode's `run` requires a **primary**-mode agent. Point `--agent` at a
`mode: subagent` definition and opencode prints a warning to stderr, falls back to the
default agent, and otherwise behaves normally — so a run that quietly ignored your agent
still exits 0 and looks fine. If the agent mattered, check `logs --stderr`.

## Gotchas

Real behaviours of this runtime, verified against opencode 1.18.15.

**Every run is `--auto`.** Without it opencode auto-*rejects* permission requests — it
doesn't block, it just proceeds toolless and produces a confident, useless answer. So the
delegate has full read/write/bash within its `--dir`. Scope `--dir` deliberately, and don't
delegate anything you wouldn't let an unsupervised agent do.

**MCP servers inflate every run.** All MCP servers in the user's opencode config are loaded
into each delegated run. On this machine that's ~8.2k input tokens before the brief is even
read. With a small local model that can consume most of the context window, and the
observed failure is subtle: the model emits *fake* tool calls as plain text
(`{"name": "read", "arguments": {…}}` appearing in `result`) instead of calling anything.
If you see that, the model is context-starved — use `--pure`, or a larger model.

**Timeouts.** The default watchdog is 900 s; `--timeout` overrides it. On expiry the run is
SIGTERMed, then SIGKILLed 5 s later, and lands in `status: "timeout"`. macOS has no
`timeout` binary, so this is implemented in-script.

**Runs survive their parent shell.** `spawn` detaches via `nohup`, so a delegation keeps
going after the command that started it returns. `cancel` stops one properly, escalating to
SIGKILL — opencode ignores SIGTERM while blocked on a model request, and killing only the
wrapper leaves an orphan reparented to init.

**Errors.** `errors[]` carries anything opencode reported as a session error, and `status`
is `error` whenever the exit code is non-zero or an error event appeared. Note that some
models on the hosted `opencode/*` provider are billing-gated and fail with a 401 that shows
up here — that's a per-model condition, not a broken install. `delegate-local doctor` does a
live round-trip against the configured default to tell you which situation you're in.

**Debugging.** `logs --events <id>` is the raw NDJSON, `logs --stderr <id>` catches
warnings that never reach the result (fallback notices, permission rejections), and
`logs --prompt <id>` shows the brief exactly as the delegate received it.

## Reference

`references/opencode-cli.md` documents the `opencode run` flags and the complete
`--format json` event contract, reverse-engineered from the binary. Read it if you need to
parse the event stream yourself, tune session or permission behaviour, or work out why a
run behaved unexpectedly.
