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


@pytest.mark.asyncio
async def test_native_count_key_runs_once_off_loop_for_record(tmp_path, monkeypatch):
    from tools import vision_tools, vision_tools_history_budget as budget

    path = tmp_path / "shot.png"
    path.write_bytes(PNG)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(vision_tools, "_resolve_repeat_cap", lambda: 0)
    loop_thread = threading.get_ident()
    calls = []
    real_count_key = budget._count_key

    def tracked(url):
        calls.append((threading.get_ident(), url))
        return real_count_key(url)

    monkeypatch.setattr(vision_tools, "_count_key", tracked)
    monkeypatch.setattr(budget, "_count_key", tracked)
    with budget._repeat_lock:
        budget._repeat_counts.clear()
    try:
        result = await vision_tools._vision_analyze_native(str(path), "what?")
        assert isinstance(result, dict) and result.get("_multimodal") is True
        assert len(calls) == 1
        assert calls[0][0] != loop_thread
    finally:
        with budget._repeat_lock:
            budget._repeat_counts.clear()


@pytest.mark.asyncio
async def test_native_reserve_and_release_reuse_same_off_loop_key_across_symlink_retarget(tmp_path, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from tools import vision_tools, vision_tools_history_budget as budget

    target_a = tmp_path / "a.png"
    target_b = tmp_path / "b.png"
    target_a.write_bytes(PNG)
    target_b.write_bytes(PNG)
    link = tmp_path / "current.png"
    link.symlink_to(target_a)
    monkeypatch.setenv("TERMINAL_ENV", "local")

    loop_thread = threading.get_ident()
    calls = []
    real_count_key = budget._count_key

    def tracked(url):
        calls.append((threading.get_ident(), url))
        return real_count_key(url)

    async def retarget_and_fail(url, *args, **kwargs):
        link.unlink()
        link.symlink_to(target_b)
        raise vision_tools._ImagePrepError("prep failed after retarget")

    with budget._repeat_lock:
        budget._repeat_counts.clear()
    try:
        with delegated_child_context("child-retarget"):
            key_a = real_count_key(str(target_a))
            key_b = real_count_key(str(target_b))
            assert budget.reserve_embed(key_b, 3) is None
            monkeypatch.setattr(vision_tools, "_count_key", tracked)
            monkeypatch.setattr(budget, "_count_key", tracked)
            monkeypatch.setattr(vision_tools, "_prepare_image", retarget_and_fail)

            raw = await vision_tools._vision_analyze_native(str(link), "what?")
            payload = json.loads(raw)
            assert payload["success"] is False
            assert len(calls) == 1
            assert calls[0][0] != loop_thread
            assert key_a not in budget._repeat_counts
            assert budget._repeat_counts.get(key_b) == 1
    finally:
        with budget._repeat_lock:
            budget._repeat_counts.clear()
