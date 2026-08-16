#!/usr/bin/env bash
# delegate.sh — spawn/poll/collect protocol over a local `opencode` runtime.
#
# Every subcommand prints exactly one JSON document to stdout so callers can
# parse it deterministically. Diagnostics go to stderr.
#
#   spawn   start a detached delegation, print a handle, return immediately
#   status  cheap terminal-or-not check
#   result  structured result JSON
#   wait    poll until terminal, then print result(s)  (the join primitive)
#   send    follow-up turn on the same opencode session
#   list    recent runs
#   cancel  stop a running delegation
#   logs    raw event / stderr streams for debugging
#   doctor  preflight the local runtime

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/$(basename "${BASH_SOURCE[0]}")"
EVENTS_PY="$SELF_DIR/_events.py"

STORE="${DELEGATE_LOCAL_HOME:-$HOME/.local/state/opencode-delegate}"
RUNS="$STORE/runs"
DEFAULT_TIMEOUT="${DELEGATE_LOCAL_TIMEOUT:-900}"
POLL_INTERVAL="${DELEGATE_LOCAL_POLL:-3}"

die() { printf 'delegate: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

require_deps() {
  have opencode || die "opencode not found on PATH (brew install sst/tap/opencode)"
  have jq       || die "jq not found on PATH"
  have python3  || die "python3 not found on PATH"
}

new_run_id() {
  printf 'oc_%s_%s' \
    "$(date -u +%Y%m%dT%H%M%S)" \
    "$(od -An -tx1 -N2 /dev/urandom | tr -d ' \n')"
}

# Accept a full run id or an unambiguous prefix — handy when typing by hand.
resolve_run() {
  local id="$1" dir matches
  dir="$RUNS/$id"
  [ -d "$dir" ] && { printf '%s' "$dir"; return 0; }
  matches=$(find "$RUNS" -maxdepth 1 -type d -name "${id}*" 2>/dev/null)
  case "$(printf '%s' "$matches" | grep -c .)" in
    1) printf '%s' "$matches" ;;
    0) die "no such run: $id" ;;
    *) die "ambiguous run prefix: $id" ;;
  esac
}

run_status() {
  local dir="$1" st pid
  st=$(cat "$dir/status" 2>/dev/null || echo unknown)
  case "$st" in
    done|error|timeout|cancelled) printf '%s' "$st"; return ;;
  esac
  pid=$(cat "$dir/runner.pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    printf 'running'; return
  fi
  # Runner vanished without recording a terminal state — fall back to exit code.
  if [ "$(cat "$dir/exit_code" 2>/dev/null || echo 1)" -eq 0 ] 2>/dev/null \
     && ! grep -q '"type":"error"' "$dir/events.ndjson" 2>/dev/null; then
    printf 'done'
  else
    printf 'error'
  fi
}

is_terminal() {
  case "$1" in done|error|timeout|cancelled) return 0 ;; *) return 1 ;; esac
}

session_of() {
  # Session id is on every event; read it live so `send` works mid-run too.
  local dir="$1" sid
  sid=$(jq -r 'select(.sessionID) | .sessionID' "$dir/events.ndjson" 2>/dev/null | head -1)
  [ -n "$sid" ] && [ "$sid" != "null" ] && { printf '%s' "$sid"; return 0; }
  jq -r '.session_id // empty' "$dir/meta.json" 2>/dev/null
}

emit_result() { python3 "$EVENTS_PY" "$1"; }

# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

usage_spawn() {
  cat >&2 <<'EOF'
usage: delegate.sh spawn [options] [prompt...]

  --dir <path>          working directory for the delegate (default: cwd)
  --title <str>         session title (shows up in `opencode session list`)
  --agent <name>        opencode agent; must be a PRIMARY-mode agent
  --model <prov/model>  override the model; omitted entirely unless you pass it
  --variant <v>         provider reasoning effort (high, max, minimal, ...)
  --file <path>         attach a file (repeatable)
  --timeout <sec>       watchdog timeout (default: 900)
  --thinking            include reasoning events in the stream
  --pure                run opencode without external plugins
  --prompt-file <path>  read the task brief from a file
  --session <ses_id>    continue an existing opencode session
  --fork                fork the session (requires --session)
  --parent <run_id>     record lineage (set automatically by `send`)

The prompt may be given as positional args, via --prompt-file, or on stdin.
EOF
  exit 2
}

cmd_spawn() {
  require_deps
  local dir="" title="" agent="" model="" variant="" timeout="$DEFAULT_TIMEOUT"
  local prompt_file="" session="" parent="" thinking=0 pure=0 fork=0
  local files=() prompt_parts=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --dir)         dir="${2:-}"; shift 2 ;;
      --title)       title="${2:-}"; shift 2 ;;
      --agent)       agent="${2:-}"; shift 2 ;;
      --model)       model="${2:-}"; shift 2 ;;
      --variant)     variant="${2:-}"; shift 2 ;;
      --file)        files+=("${2:-}"); shift 2 ;;
      --timeout)     timeout="${2:-}"; shift 2 ;;
      --prompt-file) prompt_file="${2:-}"; shift 2 ;;
      --session)     session="${2:-}"; shift 2 ;;
      --parent)      parent="${2:-}"; shift 2 ;;
      --thinking)    thinking=1; shift ;;
      --pure)        pure=1; shift ;;
      --fork)        fork=1; shift ;;
      -h|--help)     usage_spawn ;;
      --)            shift; prompt_parts+=("$@"); break ;;
      -*)            die "unknown spawn flag: $1" ;;
      *)             prompt_parts+=("$1"); shift ;;
    esac
  done

  [ "$fork" -eq 1 ] && [ -z "$session" ] && die "--fork requires --session"

  local run_id run_dir
  run_id="$(new_run_id)"
  run_dir="$RUNS/$run_id"
  mkdir -p "$run_dir" || die "cannot create run dir: $run_dir"

  # Task brief: file > positional args > stdin. Fed to opencode via stdin, which
  # merges it into the prompt — that sidesteps argv quoting entirely.
  if [ -n "$prompt_file" ]; then
    [ -f "$prompt_file" ] || die "prompt file not found: $prompt_file"
    cat "$prompt_file" > "$run_dir/prompt.md"
  elif [ ${#prompt_parts[@]} -gt 0 ]; then
    printf '%s\n' "${prompt_parts[*]}" > "$run_dir/prompt.md"
  elif [ ! -t 0 ]; then
    cat > "$run_dir/prompt.md"
  fi
  [ -s "$run_dir/prompt.md" ] || { rm -rf "$run_dir"; die "empty prompt"; }

  dir="${dir:-$PWD}"
  [ -d "$dir" ] || die "no such directory: $dir"
  dir="$(cd "$dir" && pwd)"

  # Assemble argv. Only flags explicitly supplied are forwarded — opencode's own
  # config stays the source of truth for model/agent selection.
  local argv=(opencode run --format json --auto --dir "$dir")
  [ -n "$title" ]   && argv+=(--title "$title")
  [ -n "$agent" ]   && argv+=(--agent "$agent")
  [ -n "$model" ]   && argv+=(--model "$model")
  [ -n "$variant" ] && argv+=(--variant "$variant")
  [ -n "$session" ] && argv+=(--session "$session")
  [ "$fork" -eq 1 ]     && argv+=(--fork)
  [ "$thinking" -eq 1 ] && argv+=(--thinking)
  [ "$pure" -eq 1 ]     && argv+=(--pure)
  local f
  for f in "${files[@]:-}"; do [ -n "$f" ] && argv+=(--file "$f"); done

  # jq's --args still parses `--`-prefixed values as its own options, so argv is
  # serialised NUL-delimited through python3 instead.
  local argv_json
  argv_json=$(printf '%s\0' "${argv[@]}" | python3 -c \
    'import json,sys; print(json.dumps([p.decode() for p in sys.stdin.buffer.read().split(b"\0")[:-1]]))')

  jq -n \
    --arg run_id "$run_id" --arg dir "$dir" --arg title "$title" \
    --arg agent "$agent" --arg model "$model" --arg variant "$variant" \
    --arg session "$session" --arg parent "$parent" \
    --arg started_at "$(now_iso)" --argjson started_epoch "$(date +%s)" \
    --argjson timeout "$timeout" --argjson oc_argv "$argv_json" \
    '{run_id:$run_id, dir:$dir, timeout:$timeout,
      title:(if $title=="" then null else $title end),
      agent:(if $agent=="" then null else $agent end),
      model:(if $model=="" then null else $model end),
      variant:(if $variant=="" then null else $variant end),
      session_id:(if $session=="" then null else $session end),
      parent_run_id:(if $parent=="" then null else $parent end),
      started_at:$started_at, started_at_epoch:$started_epoch,
      oc_argv:$oc_argv}' > "$run_dir/meta.json" || die "failed to write meta.json"

  echo running > "$run_dir/status"
  : > "$run_dir/events.ndjson"

  nohup "$SELF" __exec "$run_dir" >/dev/null 2>&1 &
  disown 2>/dev/null || true

  jq -n --arg run_id "$run_id" --arg dir "$dir" --arg run_dir "$run_dir" \
        --arg started_at "$(now_iso)" \
    '{run_id:$run_id, status:"running", dir:$dir, run_dir:$run_dir, started_at:$started_at}'
}

# ---------------------------------------------------------------------------
# __exec — internal. Runs detached under nohup; never called directly.
# ---------------------------------------------------------------------------

cmd_exec() {
  local run_dir="${1:-}"
  [ -d "$run_dir" ] || exit 1
  echo "$$" > "$run_dir/runner.pid"

  # NUL-delimited so arguments containing newlines survive the round trip.
  local argv=() timeout
  while IFS= read -r -d '' arg; do argv+=("$arg"); done \
    < <(jq -j '.oc_argv[] + "\u0000"' "$run_dir/meta.json")
  timeout=$(jq -r '.timeout // 900' "$run_dir/meta.json")

  "${argv[@]}" < "$run_dir/prompt.md" > "$run_dir/events.ndjson" 2> "$run_dir/stderr.log" &
  local oc_pid=$!
  echo "$oc_pid" > "$run_dir/oc.pid"

  # macOS ships no `timeout`/`gtimeout`, so the watchdog is hand-rolled.
  (
    sleep "$timeout"
    if kill -0 "$oc_pid" 2>/dev/null; then
      : > "$run_dir/.timedout"
      kill -TERM "$oc_pid" 2>/dev/null
      sleep 5
      kill -KILL "$oc_pid" 2>/dev/null
    fi
  ) &
  local wd_pid=$!

  wait "$oc_pid"
  local rc=$?
  kill "$wd_pid" 2>/dev/null

  echo "$rc" > "$run_dir/exit_code"

  local sid final
  sid=$(session_of "$run_dir")
  if [ -n "$sid" ]; then
    jq --arg sid "$sid" '.session_id = $sid' "$run_dir/meta.json" > "$run_dir/meta.json.tmp" \
      && mv "$run_dir/meta.json.tmp" "$run_dir/meta.json"
  fi

  if [ -f "$run_dir/.timedout" ]; then
    final=timeout
  elif [ -f "$run_dir/.cancelled" ]; then
    final=cancelled
  elif [ "$rc" -ne 0 ] || grep -q '"type":"error"' "$run_dir/events.ndjson" 2>/dev/null; then
    final=error
  else
    final=done
  fi
  echo "$final" > "$run_dir/status"
}

# ---------------------------------------------------------------------------
# status / result / wait / list / cancel / logs / send
# ---------------------------------------------------------------------------

cmd_status() {
  local ids=() all=0
  while [ $# -gt 0 ]; do
    case "$1" in --all) all=1; shift ;; *) ids+=("$1"); shift ;; esac
  done
  if [ "$all" -eq 1 ]; then
    while IFS= read -r d; do ids+=("$(basename "$d")"); done \
      < <(find "$RUNS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)
  fi
  [ ${#ids[@]} -gt 0 ] || die "usage: delegate.sh status <run_id...> | --all"

  local out="[]" id dir st sid
  for id in "${ids[@]}"; do
    dir="$(resolve_run "$id")"
    st="$(run_status "$dir")"
    sid="$(session_of "$dir")"
    out=$(jq -n --argjson acc "$out" \
      --arg run_id "$(basename "$dir")" --arg status "$st" --arg sid "$sid" \
      --argjson terminal "$(is_terminal "$st" && echo true || echo false)" \
      '$acc + [{run_id:$run_id, status:$status, terminal:$terminal,
                session_id:(if $sid=="" then null else $sid end)}]')
  done
  if [ "${#ids[@]}" -eq 1 ]; then jq -r '.[0]' <<<"$out"; else printf '%s\n' "$out"; fi
}

cmd_result() {
  [ $# -ge 1 ] || die "usage: delegate.sh result <run_id...>"
  if [ $# -eq 1 ]; then emit_result "$(resolve_run "$1")"; return; fi
  local acc="[]" id
  for id in "$@"; do
    acc=$(jq -n --argjson a "$acc" --argjson r "$(emit_result "$(resolve_run "$id")")" '$a + [$r]')
  done
  printf '%s\n' "$acc"
}

cmd_wait() {
  local ids=() all=0 deadline=0 wait_timeout="${DELEGATE_LOCAL_WAIT_TIMEOUT:-0}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --all)     all=1; shift ;;
      --timeout) wait_timeout="${2:-0}"; shift 2 ;;
      *)         ids+=("$1"); shift ;;
    esac
  done
  if [ "$all" -eq 1 ]; then
    while IFS= read -r d; do
      [ "$(run_status "$d")" = running ] && ids+=("$(basename "$d")")
    done < <(find "$RUNS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)
  fi
  [ ${#ids[@]} -gt 0 ] || die "usage: delegate.sh wait <run_id...> | --all"

  [ "$wait_timeout" -gt 0 ] 2>/dev/null && deadline=$(( $(date +%s) + wait_timeout ))

  local pending=1 id dir
  while [ "$pending" -eq 1 ]; do
    pending=0
    for id in "${ids[@]}"; do
      dir="$(resolve_run "$id")"
      is_terminal "$(run_status "$dir")" || pending=1
    done
    [ "$pending" -eq 0 ] && break
    if [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ]; then
      printf 'delegate: wait timed out with runs still in flight\n' >&2
      break
    fi
    sleep "$POLL_INTERVAL"
  done
  cmd_result "${ids[@]}"
}

cmd_list() {
  local limit="${1:-20}" acc="[]" dir st sid meta
  while IFS= read -r dir; do
    [ -d "$dir" ] || continue
    st="$(run_status "$dir")"
    sid="$(session_of "$dir")"
    meta="$dir/meta.json"
    acc=$(jq -n --argjson a "$acc" --argjson m "$(cat "$meta" 2>/dev/null || echo '{}')" \
      --arg status "$st" --arg sid "$sid" --arg rid "$(basename "$dir")" \
      '$a + [{run_id:$rid, status:$status,
              session_id:(if $sid=="" then null else $sid end),
              title:$m.title, dir:$m.dir, agent:$m.agent, model:$m.model,
              parent_run_id:$m.parent_run_id, started_at:$m.started_at}]')
  done < <(find "$RUNS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -r | head -n "$limit")
  printf '%s\n' "$acc"
}

cmd_cancel() {
  [ $# -ge 1 ] || die "usage: delegate.sh cancel <run_id...>"
  local acc="[]" id dir pid
  for id in "$@"; do
    dir="$(resolve_run "$id")"
    : > "$dir/.cancelled"
    # Kill only opencode, not the runner — the runner's `wait` then returns and
    # it records the terminal state itself. opencode ignores SIGTERM while
    # blocked on a model request, so escalate to SIGKILL rather than leaking a
    # process reparented to init.
    pid=$(cat "$dir/oc.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ]; then
      kill -TERM "$pid" 2>/dev/null
      local n=0
      while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 6 ]; do sleep 1; n=$((n+1)); done
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    fi
    echo cancelled > "$dir/status"
    acc=$(jq -n --argjson a "$acc" --arg rid "$(basename "$dir")" \
      '$a + [{run_id:$rid, status:"cancelled"}]')
  done
  if [ $# -eq 1 ]; then jq -r '.[0]' <<<"$acc"; else printf '%s\n' "$acc"; fi
}

cmd_logs() {
  local which=events id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --stderr) which=stderr; shift ;;
      --events) which=events; shift ;;
      --prompt) which=prompt; shift ;;
      *)        id="$1"; shift ;;
    esac
  done
  [ -n "$id" ] || die "usage: delegate.sh logs [--events|--stderr|--prompt] <run_id>"
  local dir; dir="$(resolve_run "$id")"
  case "$which" in
    events) cat "$dir/events.ndjson" 2>/dev/null ;;
    stderr) cat "$dir/stderr.log" 2>/dev/null ;;
    prompt) cat "$dir/prompt.md" 2>/dev/null ;;
  esac
}

cmd_send() {
  local id="" fork=0 rest=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --fork) fork=1; shift ;;
      *) if [ -z "$id" ]; then id="$1"; else rest+=("$1"); fi; shift ;;
    esac
  done
  [ -n "$id" ] || die "usage: delegate.sh send [--fork] <run_id> [prompt...]"

  local dir sid parent_meta
  dir="$(resolve_run "$id")"
  sid="$(session_of "$dir")"
  [ -n "$sid" ] || die "no session id recorded for $id (did the run produce any events?)"
  parent_meta="$dir/meta.json"

  local args=(--session "$sid" --parent "$(basename "$dir")")
  [ "$fork" -eq 1 ] && args+=(--fork)
  local pdir pagent pmodel ptimeout
  pdir=$(jq -r '.dir // empty' "$parent_meta")
  pagent=$(jq -r '.agent // empty' "$parent_meta")
  pmodel=$(jq -r '.model // empty' "$parent_meta")
  ptimeout=$(jq -r '.timeout // empty' "$parent_meta")
  [ -n "$pdir" ]     && args+=(--dir "$pdir")
  [ -n "$pagent" ]   && args+=(--agent "$pagent")
  [ -n "$pmodel" ]   && args+=(--model "$pmodel")
  [ -n "$ptimeout" ] && args+=(--timeout "$ptimeout")

  if [ ${#rest[@]} -gt 0 ]; then
    cmd_spawn "${args[@]}" -- "${rest[@]}"
  else
    cmd_spawn "${args[@]}"   # prompt arrives on stdin
  fi
}

# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

cmd_doctor() {
  local checks="[]" ok pass
  add() { # name ok detail
    checks=$(jq -n --argjson a "$checks" --arg n "$1" --argjson o "$2" --arg d "$3" \
      '$a + [{check:$n, ok:$o, detail:$d}]')
  }

  if have opencode; then
    add opencode_installed true "$(opencode --version 2>/dev/null | head -1) at $(command -v opencode)"
  else
    add opencode_installed false "not on PATH"
  fi
  have jq      && add jq true "$(command -v jq)"           || add jq false "missing"
  have python3 && add python3 true "$(command -v python3)" || add python3 false "missing"

  # The configured default model is what delegation uses when --model is omitted.
  local cfg="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}" default_model=""
  if [ -f "$cfg" ]; then
    default_model=$(jq -r '.model // empty' "$cfg" 2>/dev/null)
    add config_found true "$cfg (default model: ${default_model:-<unset>})"
  else
    add config_found false "no config at $cfg"
  fi

  # A live round-trip is the only honest check that the default model works.
  if have opencode; then
    local probe_dir probe_out
    probe_dir="$(mktemp -d)"
    probe_out=$(cd "$probe_dir" && printf 'Reply with exactly: ok' \
      | opencode run --format json --auto 2>&1 | head -c 4000)
    rm -rf "$probe_dir"
    if printf '%s' "$probe_out" | grep -q '"type":"error"'; then
      add default_model_usable false \
        "$(printf '%s' "$probe_out" | jq -r 'select(.type=="error") | .error.data.message // .error.name' 2>/dev/null | head -1)"
    elif printf '%s' "$probe_out" | grep -q '"type":"text"'; then
      add default_model_usable true "round-trip succeeded (${default_model:-config default})"
    else
      add default_model_usable false "no text event returned; see: delegate.sh doctor output"
    fi
  fi

  local url
  for url in "http://127.0.0.1:11434/api/tags|ollama" "http://127.0.0.1:11436/v1/models|apple_foundation"; do
    local u="${url%%|*}" name="${url##*|}"
    if curl -s --max-time 3 "$u" >/dev/null 2>&1; then
      add "local_server_$name" true "reachable at ${u%%/api*}"
    else
      add "local_server_$name" false "not reachable at $u"
    fi
  done

  add run_store true "$RUNS"

  pass=$(jq '[.[] | select(.ok == false)] | length == 0' <<<"$checks")
  jq -n --argjson checks "$checks" --argjson ok "$pass" '{ok:$ok, checks:$checks}'
}

# ---------------------------------------------------------------------------

usage() {
  sed -n '2,20p' "$SELF" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

mkdir -p "$RUNS" 2>/dev/null

case "${1:-}" in
  spawn)   shift; cmd_spawn "$@" ;;
  status)  shift; cmd_status "$@" ;;
  result)  shift; cmd_result "$@" ;;
  wait)    shift; cmd_wait "$@" ;;
  send)    shift; cmd_send "$@" ;;
  list)    shift; cmd_list "$@" ;;
  cancel)  shift; cmd_cancel "$@" ;;
  logs)    shift; cmd_logs "$@" ;;
  doctor)  shift; cmd_doctor "$@" ;;
  __exec)  shift; cmd_exec "$@" ;;
  ""|-h|--help|help) usage ;;
  *) die "unknown subcommand: $1 (try --help)" ;;
esac
