#!/usr/bin/env python3
"""Turn an opencode `run --format json` NDJSON stream into one structured result.

Usage: _events.py [--stream] <run_dir>

Reads the run directory written by delegate.sh and prints a single JSON object.
Tolerates a truncated final line, which is normal when a run is killed by the
watchdog or cancelled mid-stream.
"""

import json
import os
import re
import sys
import time

RESULT_RE = re.compile(r"<result>(.*?)</result>", re.DOTALL | re.IGNORECASE)
TERMINAL = {"done", "error", "timeout", "cancelled"}
MAX_TOOL_INPUT = 400
STREAM_POLL_SECONDS = 0.1


def read_text(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def parse_events(path):
    """NDJSON -> list of dicts. A partial trailing line is dropped, not fatal."""
    events = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def truncate(value):
    try:
        blob = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(value)
    return blob if len(blob) <= MAX_TOOL_INPUT else blob[:MAX_TOOL_INPUT] + "…"


def error_message(err):
    """opencode nests the useful string at error.data.message; fall back gracefully."""
    if not isinstance(err, dict):
        return str(err)
    data = err.get("data")
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return str(err.get("name") or err)


def project_event(event):
    """Reduce a raw opencode event to the communication a supervisor needs.

    opencode emits completed parts rather than token deltas. Keeping this
    projection small lets a supervising subagent observe progress without
    absorbing metadata-heavy tool inputs and outputs into its context.
    """
    event_type = event.get("type")
    part = event.get("part") or {}
    projected = {"event": event_type}

    if event_type in {"text", "reasoning"}:
        text = (part.get("text") or "").strip()
        if not text:
            return None
        projected["text"] = text
    elif event_type == "tool_use":
        state = part.get("state") or {}
        projected.update(
            {
                "tool": part.get("tool"),
                "status": state.get("status"),
                "title": state.get("title"),
            }
        )
        if state.get("error"):
            projected["error"] = str(state["error"])
    elif event_type == "error":
        projected["message"] = error_message(event.get("error"))
    elif event_type == "step_finish":
        projected["reason"] = part.get("reason")
        projected["tokens"] = part.get("tokens") or {}
    else:
        return None

    if event.get("sessionID"):
        projected["session_id"] = event["sessionID"]
    return projected


def stream_events(run_dir):
    """Follow a live run and flush compact NDJSON events as they complete."""
    events_path = os.path.join(run_dir, "events.ndjson")
    while not os.path.exists(events_path):
        if read_text(os.path.join(run_dir, "status")) in TERMINAL:
            break
        time.sleep(STREAM_POLL_SECONDS)

    try:
        with open(events_path, errors="replace") as events_file:
            while True:
                line_start = events_file.tell()
                line = events_file.readline()
                if line:
                    status = read_text(os.path.join(run_dir, "status"))
                    if not line.endswith("\n") and status not in TERMINAL:
                        events_file.seek(line_start)
                        time.sleep(STREAM_POLL_SECONDS)
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    projected = project_event(event)
                    if projected:
                        print(json.dumps(projected, ensure_ascii=False), flush=True)
                    continue

                status = read_text(os.path.join(run_dir, "status"))
                if status in TERMINAL:
                    print(json.dumps({"event": "terminal", "status": status}), flush=True)
                    return 0
                time.sleep(STREAM_POLL_SECONDS)
    except OSError as exc:
        print(json.dumps({"event": "stream_error", "message": str(exc)}), flush=True)
        return 1


def collect(events):
    texts, tools, errors = [], [], []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "total": 0}
    cache = {"read": 0, "write": 0}
    cost = 0.0
    steps = 0
    session_id = None
    first_ts = last_ts = None

    for ev in events:
        ts = ev.get("timestamp")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        session_id = session_id or ev.get("sessionID")

        etype = ev.get("type")
        part = ev.get("part") or {}

        if etype == "text":
            text = (part.get("text") or "").strip()
            if text:
                texts.append(text)
        elif etype == "tool_use":
            state = part.get("state") or {}
            entry = {
                "tool": part.get("tool"),
                "status": state.get("status"),
                "title": state.get("title"),
            }
            if state.get("input") is not None:
                entry["input"] = truncate(state["input"])
            if state.get("error"):
                entry["error"] = str(state["error"])
            tools.append(entry)
        elif etype == "step_finish":
            steps += 1
            tk = part.get("tokens") or {}
            for key in ("input", "output", "reasoning", "total"):
                if isinstance(tk.get(key), (int, float)):
                    tokens[key] += tk[key]
            ck = tk.get("cache") or {}
            for key in ("read", "write"):
                if isinstance(ck.get(key), (int, float)):
                    cache[key] += ck[key]
            if isinstance(part.get("cost"), (int, float)):
                cost += part["cost"]
        elif etype == "error":
            errors.append(error_message(ev.get("error")))

    tokens["cache"] = cache
    return {
        "session_id": session_id,
        "texts": texts,
        "tool_calls": tools,
        "errors": errors,
        "tokens": tokens,
        "cost": cost,
        "steps": steps,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def extract_result(texts):
    """Small local models follow output contracts unreliably, so degrade in stages.

    1. an explicit <result> block anywhere (the fast path the brief asks for)
    2. otherwise the last completed text part (usually the final answer)
    3. otherwise nothing — text_all still preserves whatever was produced
    """
    for text in reversed(texts):
        match = RESULT_RE.search(text)
        if match:
            return match.group(1).strip(), "result_block"
    if texts:
        return texts[-1], "last_text"
    return None, "none"


def derive_status(run_dir, exit_code, has_errors):
    recorded = read_text(os.path.join(run_dir, "status"))
    if recorded in TERMINAL:
        return recorded
    runner_pid = read_text(os.path.join(run_dir, "runner.pid"))
    if runner_pid and pid_alive(runner_pid):
        return "running"
    if recorded == "running":
        # runner died without recording a terminal state
        return "error" if (exit_code not in (0, None) or has_errors) else "done"
    return recorded or "unknown"


def elapsed(run_dir, meta, agg, status):
    """Prefer the event span; fall back to the exit-code stamp so a run that
    produced no events at all (killed by the watchdog, say) still reports how
    long it actually ran rather than how long ago it was started."""
    if agg["first_ts"] and agg["last_ts"] and agg["last_ts"] > agg["first_ts"]:
        return round((agg["last_ts"] - agg["first_ts"]) / 1000.0, 1)
    started = meta.get("started_at_epoch")
    if not started:
        return None
    if status in TERMINAL:
        try:
            return round(os.path.getmtime(os.path.join(run_dir, "exit_code")) - started, 1)
        except OSError:
            pass
    return round(time.time() - started, 1)


def main():
    stream = len(sys.argv) == 3 and sys.argv[1] == "--stream"
    if len(sys.argv) != (3 if stream else 2):
        print("usage: _events.py [--stream] <run_dir>", file=sys.stderr)
        return 2
    run_dir = sys.argv[2] if stream else sys.argv[1]
    if not os.path.isdir(run_dir):
        print(json.dumps({"error": "run not found", "run_dir": run_dir}))
        return 1

    if stream:
        return stream_events(run_dir)

    meta = read_json(os.path.join(run_dir, "meta.json"), {}) or {}
    raw_exit = read_text(os.path.join(run_dir, "exit_code"))
    exit_code = int(raw_exit) if raw_exit.lstrip("-").isdigit() else None

    events = parse_events(os.path.join(run_dir, "events.ndjson"))
    agg = collect(events)
    status = derive_status(run_dir, exit_code, bool(agg["errors"]))
    result, source = extract_result(agg["texts"])

    duration = elapsed(run_dir, meta, agg, status)

    stderr_tail = read_text(os.path.join(run_dir, "stderr.log"))[-1000:]

    out = {
        "run_id": meta.get("run_id") or os.path.basename(run_dir),
        "session_id": agg["session_id"] or meta.get("session_id"),
        "parent_run_id": meta.get("parent_run_id"),
        "status": status,
        "exit_code": exit_code,
        "result": result,
        "result_source": source,
        "text_all": "\n\n".join(agg["texts"]) or None,
        "tool_calls": agg["tool_calls"],
        "tokens": agg["tokens"],
        "cost": agg["cost"],
        "steps": agg["steps"],
        "errors": agg["errors"],
        "duration_s": duration,
        "dir": meta.get("dir"),
        "title": meta.get("title"),
        "run_dir": run_dir,
    }
    if stderr_tail and status in {"error", "timeout"}:
        out["stderr_tail"] = stderr_tail

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
