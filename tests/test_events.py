import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVENTS_SCRIPT = ROOT / "skills/delegate-local/scripts/_events.py"
DELEGATE_SCRIPT = ROOT / "skills/delegate-local/scripts/delegate.sh"

SPEC = importlib.util.spec_from_file_location("delegate_events", EVENTS_SCRIPT)
EVENTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVENTS)


class EventProjectionTests(unittest.TestCase):
    def test_projects_only_supervisor_relevant_tool_fields(self):
        event = {
            "type": "tool_use",
            "sessionID": "ses_test",
            "part": {
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "title": "run tests",
                    "input": {"command": "large private input"},
                    "output": "large private output",
                },
            },
        }

        self.assertEqual(
            EVENTS.project_event(event),
            {
                "event": "tool_use",
                "tool": "bash",
                "status": "completed",
                "title": "run tests",
                "session_id": "ses_test",
            },
        )

    def test_ignores_step_start_noise(self):
        self.assertIsNone(EVENTS.project_event({"type": "step_start", "part": {}}))


class EventStreamingTests(unittest.TestCase):
    def test_flushes_an_event_before_the_run_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            events_path = run_dir / "events.ndjson"
            events_path.touch()
            (run_dir / "status").write_text("running\n")

            process = subprocess.Popen(
                [sys.executable, "-u", str(EVENTS_SCRIPT), "--stream", str(run_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())

            event = {
                "type": "text",
                "sessionID": "ses_live",
                "part": {"text": "working now"},
            }
            with events_path.open("a") as events_file:
                events_file.write(json.dumps(event) + "\n")
                events_file.flush()

            readable, _, _ = select.select([process.stdout], [], [], 2)
            self.assertTrue(readable, "stream did not flush while status was still running")
            self.assertEqual(
                json.loads(process.stdout.readline()),
                {"event": "text", "text": "working now", "session_id": "ses_live"},
            )
            self.assertIsNone(process.poll(), "stream exited before the run became terminal")

            (run_dir / "status").write_text("done\n")
            stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout), {"event": "terminal", "status": "done"})

    def test_waits_for_a_partial_ndjson_line_to_finish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            events_path = run_dir / "events.ndjson"
            events_path.touch()
            (run_dir / "status").write_text("running\n")

            process = subprocess.Popen(
                [sys.executable, "-u", str(EVENTS_SCRIPT), "--stream", str(run_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())

            with events_path.open("a") as events_file:
                events_file.write('{"type":"text","part":{"text":"half')
                events_file.flush()
                readable, _, _ = select.select([process.stdout], [], [], 0.25)
                self.assertFalse(readable, "stream emitted a partial NDJSON event")

                events_file.write(' done"}}\n')
                events_file.flush()

            readable, _, _ = select.select([process.stdout], [], [], 2)
            self.assertTrue(readable, "stream did not resume after the NDJSON line completed")
            self.assertEqual(
                json.loads(process.stdout.readline()),
                {"event": "text", "text": "half done"},
            )

            (run_dir / "status").write_text("done\n")
            process.communicate(timeout=2)


class RunnerFrontmatterTests(unittest.TestCase):
    def test_frontmatter_hook_contains_the_direct_opencode_command(self):
        runner = (ROOT / "agents/runner.md").read_text()
        frontmatter = runner.split("---", 2)[1]

        self.assertIn("hooks:", frontmatter)
        self.assertIn("UserPromptSubmit:", frontmatter)
        self.assertIn("opencode run --format json --auto --dir", frontmatter)


class DelegateCliIntegrationTests(unittest.TestCase):
    def test_spawn_stream_and_result_with_a_fake_opencode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            fake_opencode = fake_bin / "opencode"
            fake_opencode.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                "printf '%s\\n' '{\"type\":\"text\",\"sessionID\":\"ses_fake\",\"part\":{\"text\":\"<result>streamed</result>\"}}'\n"
                "sleep 0.1\n"
                "printf '%s\\n' '{\"type\":\"step_finish\",\"sessionID\":\"ses_fake\",\"part\":{\"reason\":\"stop\",\"tokens\":{\"total\":3}}}'\n"
            )
            fake_opencode.chmod(0o755)

            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "DELEGATE_LOCAL_HOME": str(temp_path / "state"),
                "DELEGATE_LOCAL_TIMEOUT": "3",
                "DELEGATE_LOCAL_POLL": "0.05",
            }
            spawn = subprocess.run(
                [str(DELEGATE_SCRIPT), "spawn", "--dir", str(ROOT), "test prompt"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            run_id = json.loads(spawn.stdout)["run_id"]

            streamed = subprocess.run(
                [str(DELEGATE_SCRIPT), "stream", run_id],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                timeout=3,
            )
            stream_events = [json.loads(line) for line in streamed.stdout.splitlines()]
            self.assertEqual(stream_events[0]["text"], "<result>streamed</result>")
            self.assertEqual(stream_events[-1], {"event": "terminal", "status": "done"})

            result = subprocess.run(
                [str(DELEGATE_SCRIPT), "result", run_id],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            parsed_result = json.loads(result.stdout)
            self.assertEqual(parsed_result["status"], "done")
            self.assertEqual(parsed_result["session_id"], "ses_fake")
            self.assertEqual(parsed_result["result"], "streamed")
            self.assertEqual(parsed_result["result_source"], "result_block")


if __name__ == "__main__":
    unittest.main()
