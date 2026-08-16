# opencode CLI + `--format json` event contract

Reverse-engineered from the `opencode run` implementation in the compiled binary
(`strings /opt/homebrew/bin/opencode`), verified against **opencode 1.18.15** on macOS.
This is the emitter's actual behaviour, not a sample of observed output.

## Contents

- [The JSON event stream](#the-json-event-stream)
- [`opencode run` flags](#opencode-run-flags)
- [Session semantics](#session-semantics)
- [Permission semantics](#permission-semantics)
- [Prompt input paths](#prompt-input-paths)
- [Environment variables](#environment-variables)
- [Agents](#agents)
- [Related commands](#related-commands)

## The JSON event stream

With `--format json`, opencode writes NDJSON to stdout. One emitter produces every event,
with a fixed envelope:

```json
{"type": "<type>", "timestamp": 1786900424176, "sessionID": "ses_…", ...payload}
```

`sessionID` is on **every** event, which makes it a reliable handle even before the run
finishes.

### Event types (exhaustive)

| `type` | payload | emitted when |
|---|---|---|
| `step_start` | `part` (`type: "step-start"`) | an assistant step begins |
| `text` | `part` with `text`, `time.start/end` | a text part **completes** (only if `time.end` is set) |
| `reasoning` | `part` with `text` | a reasoning part completes — **only when `--thinking` is passed** |
| `tool_use` | `part` with `tool`, `state` | a tool reaches `state.status` of `completed` **or** `error` |
| `step_finish` | `part` with `tokens`, `cost`, `reason` | a step ends |
| `error` | `error` | a `session.error` occurs; also sets `process.exitCode = 1` |

No other types exist. Notably there is **no** streaming-delta event — text arrives once,
complete. Tool *invocations* are not announced; only their outcomes are.

The stream ends when the session's status becomes `idle`.

### Shapes

`step_finish` carries the accounting:

```json
{"type":"step_finish","timestamp":1786900424176,"sessionID":"ses_…",
 "part":{"id":"prt_…","messageID":"msg_…","sessionID":"ses_…","type":"step-finish",
         "reason":"stop","cost":0,
         "tokens":{"total":8197,"input":8194,"output":3,"reasoning":0,
                   "cache":{"write":0,"read":0}}}}
```

Token counts are **per step**. Summing `input` across steps double-counts the context that
is resent each step; treat the sum as a cost proxy, not a distinct-token count.

`tool_use` nests everything under `part.state` — `status`, `input`, `output`, `title`,
`metadata`, and `error` when the tool failed.

`error` nests the human-readable message deeply; prefer `error.data.message`, fall back to
`error.name`:

```json
{"type":"error","timestamp":…,"sessionID":"ses_…",
 "error":{"name":"APIError","data":{"message":"No payment method…","statusCode":401}}}
```

### Exit codes

`0` on success. `1` when any session error occurred. `137` when SIGKILLed (what the
delegate watchdog produces on timeout or cancel).

## `opencode run` flags

```
opencode run [message..]
```

| Flag | Notes |
|---|---|
| `--format default\|json` | `json` gives the raw event stream. Incompatible with `--mini`. |
| `--auto` | Auto-approve permissions. See [permissions](#permission-semantics). |
| `--model <provider/model>` | Omit to use the config default. |
| `--agent <name>` | Must be a **primary**-mode agent. |
| `--variant <v>` | Provider reasoning effort: `high`, `max`, `minimal`, … |
| `--continue` / `-c` | Continue the last root session (one with no `parentID`). |
| `--session <id>` / `-s` | Continue a specific session. Errors out if not found. |
| `--fork` | Fork before continuing. Requires `--continue` or `--session`. |
| `--dir <path>` | `chdir`s before running; resolved against `$PWD`. |
| `--file <path>` / `-f` | Attach a file (repeatable). Must exist. Directories allowed locally. |
| `--title [str]` | Session title. Bare `--title` uses the prompt truncated to 50 chars. |
| `--thinking` | Emit `reasoning` events. Default off for `run`. |
| `--share` | Share the session. |
| `--pure` | Run without external plugins. |
| `--command <name>` | Run a slash command; the message becomes its arguments. |
| `--attach <url>` | Drive a remote opencode server; `--dir` becomes a remote path. |
| `--port`, `--username`/`-u`, `--password`/`-p` | Server / basic-auth plumbing. |
| `--print-logs`, `--log-level` | Diagnostics to stderr. |

Hidden aliases for `--auto`: `--yolo`, `--dangerously-skip-permissions`.

## Session semantics

`--session` looks the session up and **exits with an error** if it doesn't exist.
`--continue` picks the most recent session with no `parentID`. With `--fork`, either path
forks first and the new session id is what subsequent events carry.

Sessions created by `run` (non-interactive) are constructed with these permission
overrides baked in:

```json
[{"permission":"question",   "action":"deny", "pattern":"*"},
 {"permission":"plan_enter", "action":"deny", "pattern":"*"},
 {"permission":"plan_exit",  "action":"deny", "pattern":"*"}]
```

So a headless delegate **structurally cannot** ask clarifying questions or enter plan mode.
Briefs must be self-contained.

Sessions persist. `opencode session list` enumerates them, `opencode session delete <id>`
removes one, and `opencode export <id>` dumps the full transcript as JSON (`--sanitize`
redacts sensitive transcript and file data).

## Permission semantics

On a `permission.asked` event:

- with `--auto` (or `--yolo` / `--dangerously-skip-permissions`) → replies `once`, allowing it
- **without** → replies `reject`, and prints `permission requested: … ; auto-rejecting` to stderr

The run does **not** block waiting for a human either way. This is the important trap: a
headless run without `--auto` doesn't hang or fail loudly — it proceeds without tools and
produces a plausible answer that did nothing. Any autonomous delegation needs `--auto`.

`OPENCODE_PERMISSION` allows finer-grained control via config rather than the blanket flag.

## Prompt input paths

The message can arrive three ways, and they **merge**:

1. positional args (joined with spaces)
2. anything after `--`
3. **stdin**, when stdin is not a TTY

```js
let u = process.stdin.isTTY ? undefined : await Bun.stdin.text();
P = merge(P, u) ?? "";
if (P.trim().length === 0 && !command) error("You must provide a message or a command");
```

Because the merge happens *before* the empty-message check, a stdin-only prompt is valid.
Piping the brief avoids argv quoting entirely — the recommended path for anything long or
containing quotes, backticks or `$`.

## Environment variables

| Variable | Effect |
|---|---|
| `OPENCODE_CONFIG` | Path to the config file to use instead of the default. |
| `OPENCODE_CONFIG_CONTENT` | Inline config JSON, no file needed. |
| `OPENCODE_CONFIG_DIR` | Override the config directory. |
| `OPENCODE_PERMISSION` | Permission rules without `--auto`. |
| `OPENCODE_DISABLE_PROJECT_CONFIG` | Ignore project-level `opencode.json`. |
| `OPENCODE_DISABLE_MODELS_FETCH` | Skip the remote model-list fetch (faster cold start). |
| `OPENCODE_DISABLE_AUTOUPDATE` | Don't check for updates. |
| `OPENCODE_DISABLE_DEFAULT_PLUGINS` | Skip bundled plugins. |
| `OPENCODE_DISABLE_AUTOCOMPACT` | Don't auto-compact long sessions. |
| `OPENCODE_PURE` | Equivalent of `--pure`. |
| `OPENCODE_SERVER_USERNAME` / `_PASSWORD` | Defaults for `--attach` basic auth. |

A lean `OPENCODE_CONFIG` is the lever for the MCP context-bloat problem: MCP servers
declared in the user's config are loaded into every run, costing ~8.2k input tokens before
the prompt on a typical setup. Note that `--pure` alone does **not** shed them — it drops
external plugins, not MCP servers. Overriding the config changes which providers and models
resolve, so do it deliberately rather than by default.

## Agents

Agent definitions are markdown with YAML frontmatter, in `~/.config/opencode/agents/`
(global) or `.opencode/agent/` (project):

```markdown
---
name: code-reviewer
description: Expert code review specialist.
mode: primary        # all | primary | subagent
model: provider/model
---

System prompt body…
```

`opencode agent create` scaffolds one; `--permissions` / `--tools` accepts a comma-separated
list from: `bash, read, edit, glob, grep, webfetch, task, todowrite, websearch, lsp, skill`.

**`run --agent` only accepts primary-mode agents.** Given a `mode: subagent` definition it
prints a warning to stderr and silently falls back to the default agent — the run still
exits 0, so this failure is invisible unless you read stderr.

## Related commands

| Command | Purpose |
|---|---|
| `opencode models [provider]` | List available models as `provider/model`. |
| `opencode agent list` | List agents with their permissions. |
| `opencode session list` / `delete <id>` | Manage persisted sessions. |
| `opencode export [id]` | Full transcript as JSON. `--sanitize` to redact. |
| `opencode stats` | Token usage and cost. |
| `opencode serve` | Headless server, for `run --attach`. |
| `opencode acp` | Agent Client Protocol server. |
