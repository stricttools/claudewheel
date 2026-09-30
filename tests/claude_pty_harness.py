"""A harness that runs a real, interactive Claude Code under a pty against a mock API.

Each run gets a throwaway Claude Code config directory with the settings the
test gives it, a working directory, and its own mock Anthropic API
(tests/mock_anthropic_api.py, a separate process on an ephemeral loopback
port) answering from the test's rules. Claude Code runs under a pty, so it is
an interactive session (CLAUDE_CODE_SESSION_ATTENDED=1 for its hooks), and a
thread drains its terminal so it never blocks on output. The test reads the
requests the mock logged, and so sees exactly what reached the model.

The binary is an installed Claude Code version, found where claudewheel
installs them: ~/.local/share/claude/versions/<version> in the account's real
home directory (the suite's own HOME is a throwaway). A version that is not
installed fails the test with the command that installs it.

Named ``claude_pty_harness`` (not ``test_*``) so pytest does not collect it.
"""

from __future__ import annotations

import json
import os
import pty
import pwd
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MOCK = HERE / "mock_anthropic_api.py"
FAKE_KEY = "sk-ant-fake-probe-0123456789abcdefghij"
MODEL = "claude-sonnet-4-5"


def client_binary(version: str) -> Path:
    """The installed Claude Code *version*, or AssertionError naming the install."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    path = home / ".local" / "share" / "claude" / "versions" / version
    if not path.is_file():
        raise AssertionError(
            f"Claude Code {version} is not installed at {path}; the probe delivery "
            f"is verified against it: install it with 'claudewheel install {version}'"
        )
    return path


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE", "ANTHROPIC", "AI_AGENT"))
    }
    env.update(extra)
    return env


class MockApi:
    """The mock API process for one run, and the requests it logged."""

    def __init__(self, case_dir: Path, rules: list[dict[str, Any]]) -> None:
        self.log = case_dir / "api.log"
        self.log.write_text("")
        rules_file = case_dir / "rules.json"
        rules_file.write_text(json.dumps(rules))
        port_file = case_dir / "api.port"
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK), str(port_file), str(self.log), str(rules_file)]
        )
        deadline = time.monotonic() + 20
        while not port_file.exists():
            if time.monotonic() > deadline or self.proc.poll() is not None:
                raise AssertionError("the mock API did not start")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port_file.read_text().strip()}"

    def requests(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float, what: str
    ) -> dict[str, Any]:
        """The first logged request *predicate* accepts, waiting up to *timeout*."""
        deadline = time.monotonic() + timeout
        while True:
            for request in self.requests():
                if predicate(request):
                    return request
            if time.monotonic() > deadline:
                lasts = [r["last"][-300:] for r in self.requests()]
                raise AssertionError(
                    f"no request {what} within {timeout}s; got: {lasts}"
                )
            time.sleep(0.1)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=20)


class ClaudeRun:
    """One Claude Code process of one case."""

    def __init__(
        self,
        case_dir: Path,
        version: str,
        settings: dict[str, Any],
        api: MockApi,
        env: dict[str, str],
        args: list[str],
    ) -> None:
        self.case_dir = case_dir
        self.config_dir = case_dir / "cfg"
        self.work = case_dir / "work"
        self.config_dir.mkdir(exist_ok=True)
        self.work.mkdir(exist_ok=True)
        (self.config_dir / "settings.json").write_text(
            json.dumps({**settings, "skipDangerousModePermissionPrompt": True})
        )
        claude_json = self.config_dir / ".claude.json"
        if not claude_json.exists():
            claude_json.write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "theme": "dark",
                        "customApiKeyResponses": {
                            "approved": [FAKE_KEY[-20:]],
                            "rejected": [],
                        },
                        "projects": {
                            str(self.work): {
                                "hasTrustDialogAccepted": True,
                                "hasCompletedProjectOnboarding": True,
                            }
                        },
                        "bypassPermissionsModeAccepted": True,
                    }
                )
            )
        self.env = _clean_env(
            {
                **env,
                "CLAUDE_CONFIG_DIR": str(self.config_dir),
                "ANTHROPIC_API_KEY": FAKE_KEY,
                "ANTHROPIC_BASE_URL": api.url,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "TERM": "xterm-256color",
            }
        )
        self.argv = [
            str(client_binary(version)),
            "--dangerously-skip-permissions",
            "--model",
            MODEL,
            *args,
        ]
        self.screen = bytearray()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: become Claude Code
            os.chdir(self.work)
            os.execve(self.argv[0], self.argv, self.env)
        self._alive = True
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while self._alive:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.screen.extend(data)

    def exited(self) -> bool:
        pid, _ = os.waitpid(self.pid, os.WNOHANG)
        return pid != 0

    def stop(self) -> None:
        """End the session the way a closed terminal does, then make sure it is gone."""
        if not self._alive:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self._alive = False
                    os.close(self.fd)
                    return
                time.sleep(0.1)
        self._alive = False
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        os.close(self.fd)


def print_mode(
    case_dir: Path,
    version: str,
    settings: dict[str, Any],
    api: MockApi,
    env: dict[str, str],
    prompt: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Claude Code in print mode (-p), with no terminal; raises on *timeout*."""
    run_dir = case_dir / "print"
    run_dir.mkdir(exist_ok=True)
    config_dir = run_dir / "cfg"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps(settings))
    work = run_dir / "work"
    work.mkdir(exist_ok=True)
    full_env = _clean_env(
        {
            **env,
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "ANTHROPIC_API_KEY": FAKE_KEY,
            "ANTHROPIC_BASE_URL": api.url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    return subprocess.run(
        [
            str(client_binary(version)),
            "-p",
            "--dangerously-skip-permissions",
            "--model",
            MODEL,
            prompt,
        ],
        cwd=work,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
