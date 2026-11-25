import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from agent import file_safety
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.tools.test_image_source import PNG
from tools import image_source


@pytest.mark.asyncio
async def test_local_guard_does_not_block_loop(tmp_path, monkeypatch):
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    await image_source.resolve_image_source(str(path), image_source.ResolveContext())
    monkeypatch.setattr(file_safety, "get_read_block_error", lambda path: time.sleep(0.5))
    done = asyncio.Event()
    gaps = []

    async def tick():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def load():
        try:
            result = await image_source.resolve_image_source(str(path), image_source.ResolveContext())
            assert result.data == PNG
        finally:
            done.set()

    await asyncio.gather(tick(), load())
    assert max(gaps) < 0.2


@pytest.mark.asyncio
async def test_worker_refuses_bound_profiles_credentials(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_bytes(PNG)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    token = set_hermes_home_override(tmp_path)
    try:
        with pytest.raises(image_source.SourceUnsafe) as denied:
            await image_source.resolve_image_source(str(path), image_source.ResolveContext())
    finally:
        reset_hermes_home_override(token)
    assert str(denied.value) == (
        f"Access denied: {path.resolve()} is a Hermes credential store and cannot be read directly. "
        "Provider tools consume these credentials through internal channels." + file_safety._DID_SUFFIX)


@pytest.mark.asyncio
async def test_native_duplicate_checks_local_identity_off_loop(tmp_path, monkeypatch):
    from tools.vision_tools import _vision_analyze_native
    from tools.vision_tools_history_budget import native_turn_images

    path = tmp_path / "shot.png"
    message = [{"type": "image_url"}, {"type": "text", "text": f"[Image attached at: {path}]"}]
    loop_thread = threading.get_ident()
    resolve = Path.resolve

    def off_loop(candidate, *args, **kwargs):
        if candidate == path:
            assert threading.get_ident() != loop_thread
        return resolve(candidate, *args, **kwargs)

    with native_turn_images(message):
        monkeypatch.setattr(Path, "resolve", off_loop)
        assert json.loads(await _vision_analyze_native(str(path), "what?"))["already_in_context"]
