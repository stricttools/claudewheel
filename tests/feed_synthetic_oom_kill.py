"""Feed the probe runner one synthetic OOM-kill journal entry for a Claude Code session.

Usage: feed_synthetic_oom_kill.py STORE_ROOT CLAUDE_PID

The entry names the session scope claudewheel would have started the Claude
Code process CLAUDE_PID in (claudewheel-session-<pid>-<time>.scope, launched an
hour ago), shaped like the entries systemd's user manager writes, and goes
through the runner's own processing, so the kill is attributed through the
lifecycle store and recorded exactly as a real one would be. No process is
killed. Run by a Bash call of the integration test's session, right before
that call kills itself with SIGKILL.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from claudewheel import probe_runner
from claudewheel.workspace import Workspace

root, pid = Path(sys.argv[1]), int(sys.argv[2])
now_us = time.time_ns() // 1000
unit = f"claudewheel-session-{pid}-{int(time.time()) - 3600}.scope"
kill = probe_runner.process_entry(
    Workspace.open(root),
    {
        "MESSAGE_ID": probe_runner.OOM_KILL_MESSAGE_ID,
        "USER_UNIT": unit,
        "__REALTIME_TIMESTAMP": str(now_us),
        "__CURSOR": f"synthetic-{now_us}",
        "MESSAGE": f"{unit}: The kernel OOM killer killed some processes in this unit.",
    },
    describe=lambda _unit: None,
)
assert kill is not None and kill["session"] is not None, kill
print(f"recorded synthetic kill {kill['id']} for session {kill['session']}")
