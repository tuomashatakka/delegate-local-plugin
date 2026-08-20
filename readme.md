# delegate-local

A Claude Code **plugin** that wraps a locally installed [opencode](https://opencode.ai)
runtime as a background subagent. Send a self-contained task brief, get a handle back
immediately, and collect a compact structured result later. Multi-turn continuation and
parallel fan-out included.

The point is context, not convenience: the spawn/poll loop, the raw NDJSON event stream and
the full result JSON all stay inside the runner subagent. The main conversation sees a
verdict line and an answer.

## Install

```
/plugin marketplace add tuomashatakka/delegate-local-plugin
/plugin install delegate-local@tuomashatakka-tools
```

If the install summary says `Run /reload-plugins to activate.`, do that.

For local development, add the working copy as a marketplace:

```
/plugin marketplace add /path/to/delegate-local
```

Installed plugins are **copied** into a version-pinned cache
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), and
`/plugin marketplace update` does not refresh a copy that is already there. To pick up local
edits, either bump `version` in `plugin.json` or reinstall:

```
claude plugin uninstall delegate-local
claude plugin install delegate-local@tuomashatakka-tools
```

Requires `opencode`, `jq` and `python3` on `PATH`. Run `/delegate-local:delegations` (or
`delegate-local doctor`) to check the runtime.

### Permissions

The runner works in the background, where a permission prompt stalls the whole run. Allowlist
the shim once, in `~/.claude/settings.json` or the project's `.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(delegate-local:*)"] } }
```

Plugin subagents cannot set `permissionMode` themselves — this rule is the supported way to
grant it, and the `bin/` shim exists so that one stable pattern covers every subcommand.

## What it ships

| Component | Name | Role |
| --- | --- | --- |
| Agent | `delegate-local:runner` | Executes one brief: spawn, wait, report. Background, `haiku`, `Bash` only. |
| Skill | `delegate-local` | When delegating is worth it, how to write the brief, how to read the result. |
| Command | `/delegate-local:delegate` | Compose a brief for a task and hand it to the runner. |
| Command | `/delegate-local:delegations` | In-flight runs and runtime health. |
| Binary | `delegate-local` | The protocol CLI, on the Bash tool's `PATH`. |

The runner is deliberately a dumb pipe — it does not author briefs and does not retry on
failure. Both of those need context it doesn't have, so they stay with the caller.

## Usage

```
/delegate-local:delegate --dir src/api  add docstrings to every exported function
```

Or drive the CLI directly for management operations:

```bash
delegate-local list                  # recent runs, status, lineage
delegate-local logs --stderr <id>    # warnings that never reach the result
delegate-local cancel <id>
delegate-local doctor                # live round-trip against the configured default
```

Subcommands: `spawn`, `status`, `result`, `wait`, `send`, `list`, `cancel`, `logs`, `doctor`.

See [`skills/delegate-local/SKILL.md`](skills/delegate-local/SKILL.md) for the full protocol
and [`skills/delegate-local/references/opencode-cli.md`](skills/delegate-local/references/opencode-cli.md)
for the reverse-engineered `opencode run` flag and event-stream contract.

## Layout

```
.claude-plugin/
  plugin.json                 manifest
  marketplace.json            the repo is its own marketplace
agents/runner.md              → delegate-local:runner
commands/                     → /delegate-local:delegate, /delegate-local:delegations
bin/delegate-local            shim onto the Bash tool's PATH
skills/delegate-local/
  SKILL.md
  scripts/delegate.sh         the protocol, single entry point
  scripts/_events.py          NDJSON event stream -> structured result
  references/opencode-cli.md
```

## Development

```bash
claude plugin validate . --strict
bash -n skills/delegate-local/scripts/delegate.sh bin/delegate-local
python3 -m py_compile skills/delegate-local/scripts/_events.py
```

CI runs the same checks on every push. There is no release artifact — plugins install
straight from git.

### The public page

<https://tuomashatakka.github.io/delegate-local-plugin/> is generated from this file.
`scripts/build_page.py` turns the `#` heading and the prose above the first `##` into a
hero, each `##` section into a page section, and reads the title, version and blurb from
`plugin.json` so the manifest stays the single source of truth. Output is deterministic,
which is what lets CI commit it back only when it genuinely changed.

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt
.venv/bin/python scripts/build_page.py            # write public/index.html
.venv/bin/python scripts/build_page.py --check    # exit 1 if stale
```

Editing `public/index.html` by hand is pointless — the next push to `main` overwrites it.
Change `readme.md` instead.
