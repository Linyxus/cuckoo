"""Hook monitor tests: run the actual hook scripts as subprocesses against a
fabricated journal."""

import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).parents[2] / "hooks"


def run_hook(script: str, state_dir: Path, stdin: dict) -> dict | None:
    result = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(stdin),
        capture_output=True,
        text=True,
        timeout=15,
        env={"CUCKOO_STATE_DIR": str(state_dir), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout.strip()
    return json.loads(output) if output else None


def write_journal(state_dir: Path, records: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "journal.jsonl").open("w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def event(id, kind="message", agent="codex-1", text="hello", data=None):
    return {
        "type": "event", "id": id, "ts": 0.0,
        "agent": agent, "kind": kind, "text": text, "data": data or {},
    }


class TestStopGate:
    def test_no_journal_is_silent(self, tmp_path):
        assert run_hook("stop_gate.py", tmp_path / "none", {"cwd": "/x"}) is None

    def test_pending_items_block_stop(self, tmp_path):
        write_journal(
            tmp_path,
            [
                event(1, text="progress update"),
                event(
                    2,
                    kind="question",
                    text="which db?",
                    data={"question_id": "q-abc123"},
                ),
            ],
        )
        output = run_hook("stop_gate.py", tmp_path, {"cwd": "/x"})
        assert output["decision"] == "block"
        assert "2 update(s)" in output["reason"]
        assert "1 BLOCKED on a question" in output["reason"]
        assert "q-abc123" in output["reason"]
        # watermark advanced: same items do not nag twice
        assert (tmp_path / "notified.cursor").read_text() == "2"
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_delivered_items_do_not_block(self, tmp_path):
        write_journal(
            tmp_path,
            [event(1), {"type": "delivered", "ids": [1]}],
        )
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_new_items_after_watermark_block_again(self, tmp_path):
        write_journal(tmp_path, [event(1)])
        (tmp_path / "notified.cursor").write_text("1")
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None
        write_journal(tmp_path, [event(1), event(2, text="fresh news")])
        output = run_hook("stop_gate.py", tmp_path, {"cwd": "/x"})
        assert output["decision"] == "block"
        assert "fresh news" in output["reason"]


class TestStopGateRunningAgents:
    def write_status(self, state_dir: Path, busy: bool, pid: int | None = None):
        state_dir.mkdir(parents=True, exist_ok=True)
        import os

        (state_dir / "status.json").write_text(
            json.dumps(
                {
                    "pid": pid if pid is not None else os.getpid(),
                    "ts": 0,
                    "agents": [{"agent": "codex-1", "state": "running", "busy": busy}],
                    "pending_questions": 0,
                }
            )
        )

    def test_busy_agents_block_first_stop(self, tmp_path):
        write_journal(tmp_path, [])
        self.write_status(tmp_path, busy=True)
        output = run_hook("stop_gate.py", tmp_path, {"cwd": "/x"})
        assert output["decision"] == "block"
        assert "still working: codex-1" in output["reason"]

    def test_busy_agents_do_not_loop_on_stop_hook_active(self, tmp_path):
        write_journal(tmp_path, [])
        self.write_status(tmp_path, busy=True)
        output = run_hook(
            "stop_gate.py", tmp_path, {"cwd": "/x", "stop_hook_active": True}
        )
        assert output is None

    def test_idle_agents_allow_stop(self, tmp_path):
        write_journal(tmp_path, [])
        self.write_status(tmp_path, busy=False)
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_dead_server_pid_is_ignored(self, tmp_path):
        write_journal(tmp_path, [])
        self.write_status(tmp_path, busy=True, pid=99999999)
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_pending_items_take_precedence_over_busy(self, tmp_path):
        write_journal(tmp_path, [event(1, text="news")])
        self.write_status(tmp_path, busy=True)
        output = run_hook("stop_gate.py", tmp_path, {"cwd": "/x"})
        assert "news" in output["reason"]


class TestPostToolUse:
    def test_injects_new_activity(self, tmp_path):
        write_journal(tmp_path, [event(1, kind="message", text="found it")])
        output = run_hook("post_tool_use.py", tmp_path, {"cwd": "/x"})
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "PostToolUse"
        assert "found it" in specific["additionalContext"]
        # marks notified: silent on the next tool call and at stop time
        assert run_hook("post_tool_use.py", tmp_path, {"cwd": "/x"}) is None
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_silent_without_activity(self, tmp_path):
        write_journal(tmp_path, [event(1), {"type": "delivered", "ids": [1]}])
        assert run_hook("post_tool_use.py", tmp_path, {"cwd": "/x"}) is None

    def test_blocking_question_is_flagged(self, tmp_path):
        write_journal(
            tmp_path,
            [event(1, kind="question", text="a or b?", data={"question_id": "q-z"})],
        )
        output = run_hook("post_tool_use.py", tmp_path, {"cwd": "/x"})
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "BLOCKED" in context and "q-z" in context


class TestPromptContext:
    def test_injects_additional_context(self, tmp_path):
        write_journal(tmp_path, [event(1, kind="completed", text="task finished")])
        output = run_hook("prompt_context.py", tmp_path, {"cwd": "/x"})
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "UserPromptSubmit"
        assert "task finished" in specific["additionalContext"]
        # marks notified so the stop gate stays quiet afterwards
        assert run_hook("stop_gate.py", tmp_path, {"cwd": "/x"}) is None

    def test_silent_when_nothing_pending(self, tmp_path):
        write_journal(tmp_path, [event(1), {"type": "delivered", "ids": [1]}])
        assert run_hook("prompt_context.py", tmp_path, {"cwd": "/x"}) is None
