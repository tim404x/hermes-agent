"""Terminal child envs route GUI launches to the running Bot Desktop (#125830)."""

import os
import shutil
import socket
import tempfile

import pytest

from tools.environments.local import _make_run_env

SCREEN_BUS = "unix:path=/tmp/dbus-botscreen"


def _publish(monkeypatch, value):
    monkeypatch.setattr("tools.bot_desktop.runtime.published_env", lambda: value)


def _user_bus(monkeypatch, listening=True):
    """A real systemd-style runtime dir (0700, owned) holding a ``bus`` socket. Short /tmp path: a pytest
    tmp_path overflows AF_UNIX's 108-byte limit."""
    rt = tempfile.mkdtemp(prefix="rt-", dir="/tmp")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(os.path.join(rt, "bus"))
    if listening:
        sock.listen(1)
    monkeypatch.setenv("XDG_RUNTIME_DIR", rt)
    return rt, sock


def _headless(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def test_running_bot_desktop_display_rides_along(monkeypatch):
    _publish(
        monkeypatch,
        {
            "DISPLAY": ":20",
            "XAUTHORITY": "/run/hermes/bot-desktop/xauth",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/hermes/bot-desktop/bus",
        },
    )
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = _make_run_env({})
    assert env["DISPLAY"] == ":20"
    assert env["XAUTHORITY"] == "/run/hermes/bot-desktop/xauth"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/hermes/bot-desktop/bus"
    # X11 desktop: a leaked Wayland socket flips GTK/Chromium backends
    assert "WAYLAND_DISPLAY" not in env


def test_running_bot_desktop_beats_the_session_snapshot(monkeypatch):
    _publish(monkeypatch, {"DISPLAY": ":20"})
    monkeypatch.setenv("DISPLAY", ":0")
    env = _make_run_env({"DISPLAY": ":0"})  # login snapshot captured the seat display
    assert env["DISPLAY"] == ":20"


def test_stopped_bot_desktop_leaves_the_seat_env_alone(monkeypatch):
    _publish(monkeypatch, {})
    monkeypatch.setenv("DISPLAY", ":0")
    env = _make_run_env({})
    assert env["DISPLAY"] == ":0"
    assert "XAUTHORITY" not in env or env.get("XAUTHORITY") == ""


def test_routing_does_not_stamp_bot_desktop_activity(monkeypatch):
    stamps = []
    _publish(monkeypatch, {"DISPLAY": ":20"})
    monkeypatch.setattr(
        "tools.bot_desktop.runtime.touch_activity", lambda: stamps.append(1)
    )
    _make_run_env({})
    # A plain terminal command is not screen use; idle_stop_minutes must stay effective.
    assert stamps == []


@pytest.mark.platforms("linux")
def test_screen_bus_never_hides_the_systemd_user_bus(monkeypatch):
    # 23 Nov 2026: with the screen up, every `systemctl --user` / `systemd-run --user` from a terminal failed
    # ("Process org.freedesktop.systemd1 exited with status 1"): the screen's private bus cannot activate systemd.
    _headless(monkeypatch)
    rt, sock = _user_bus(monkeypatch)
    try:
        _publish(monkeypatch, {"DISPLAY": ":20", "DBUS_SESSION_BUS_ADDRESS": SCREEN_BUS})
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", SCREEN_BUS)  # a stray bus in the base env loses too
        env = _make_run_env({})
    finally:
        sock.close()
        shutil.rmtree(rt)
    assert env["DISPLAY"] == ":20"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={rt}/bus"
    assert env["XDG_RUNTIME_DIR"] == rt


def test_screen_xdg_dirs_stay_with_the_screen(monkeypatch):
    # The screen's config/cache/data homes hid every CLI's own config in a terminal (gh logged out, op, rclone).
    _publish(monkeypatch, {
        "DISPLAY": ":20",
        "XDG_CONFIG_HOME": "/srv/bot-desktop/xdg",
        "XDG_CACHE_HOME": "/srv/bot-desktop/xdg/.cache",
        "XDG_DATA_HOME": "/srv/bot-desktop/xdg/.local-share",
    })
    _headless(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/u/.config")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    env = _make_run_env({})
    assert env["DISPLAY"] == ":20"
    assert env["XDG_CONFIG_HOME"] == "/home/u/.config"
    assert "XDG_CACHE_HOME" not in env
    assert "XDG_DATA_HOME" not in env


@pytest.mark.platforms("linux")
def test_stale_user_bus_socket_keeps_the_screen_bus(monkeypatch):
    # No user manager behind the socket (nothing listening): the screen's bus is the only working one.
    _headless(monkeypatch)
    rt, sock = _user_bus(monkeypatch, listening=False)
    try:
        _publish(monkeypatch, {"DISPLAY": ":20", "DBUS_SESSION_BUS_ADDRESS": SCREEN_BUS})
        env = _make_run_env({})
    finally:
        sock.close()
        shutil.rmtree(rt)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == SCREEN_BUS
