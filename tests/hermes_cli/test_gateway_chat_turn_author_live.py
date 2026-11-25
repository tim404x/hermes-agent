"""A self-tab brief (message_agent new_tab) through ``chat -c <title> --create-if-missing -Q --query-file``
against an ordinary daemon: the runner's HERMES_TURN_AUTHOR carries ``tab_depth``, and the turn must run.

Live failure (24 Nov 2026, 23:16 UTC, proc_e6b11c847cc0): ``Session local-... found but has no messages.
Starting fresh.`` then ``Error: invalid_params``, exit 1, nothing delivered. The prompt.submit wire contract
refused the author's ``tab_depth`` (see tests/tui_gateway/contracts/test_turn_author_chain_fields.py).
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.gateway.test_normal_runtime_boot import control


@pytest.mark.platforms("linux")
def test_a_self_tab_author_with_chain_fields_reaches_the_turn(tmp_path, model_peer):
    home = tmp_path / "state"
    home.mkdir(mode=0o700)
    user = tmp_path / "user"
    user.mkdir()
    root = Path(__file__).resolve().parents[2]
    model_url = f"http://127.0.0.1:{model_peer.server_port}/v1"
    (home / "config.yaml").write_text(json.dumps({
        "gateway": {"multiplex_profiles": False},
        "model": {"provider": "custom", "default": "local-wire-stub", "base_url": model_url},
        "auxiliary": {"title_generation": {"enabled": False}},
    }), encoding="utf-8")
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED="1", OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=model_url)
    query_file = tmp_path / "dm.txt"

    def chat(title, author):
        query_file.write_text(f"WS_SHARED brief for {title}", encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "chat", "--in", str(user), "-c", title, "--create-if-missing",
             "-Q", "--query-file", str(query_file)],
            env={**env, "HERMES_TURN_AUTHOR": json.dumps(author, separators=(",", ":"))}, cwd=root,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, check=False)

    log_path = tmp_path / "gateway.log"
    with log_path.open("w") as log:
        daemon = subprocess.Popen([sys.executable, "-m", "gateway.run"], cwd=root, env=env,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            descriptor = {}
            while daemon.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, "identify")
                    if descriptor.get("state") == "ready":
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            assert descriptor.get("state") == "ready", log_path.read_text(encoding="utf-8")

            # The exact author tools/bot_mode_dm.py::_open_self_tab stamps on a self tab.
            tab = chat("Gateway loop-stall root fix",
                       {"id": "bot:default", "name": "hermes", "is_bot": True, "tab_depth": 1})
            assert tab.returncode == 0 and "invalid_params" not in tab.stderr, tab
            assert "LOCAL_ACK_WS_SHARED" in tab.stdout, tab
            # A bot-to-bot reply further down a chain carries ``hops``.
            hop = chat("Hub Eye v2 flow: Kepler build status",
                       {"id": "bot:kepler", "name": "Kepler", "is_bot": True, "hops": 2})
            assert hop.returncode == 0 and "invalid_params" not in hop.stderr, hop
            assert len(model_peer.requests) == 2
        finally:
            if daemon.poll() is None:
                daemon.send_signal(signal.SIGINT)
                try:
                    daemon.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
