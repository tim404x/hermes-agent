"""A DM admitted into a busy side window refreshes the tab stamp like every other DM turn (R2)."""
import time
from tests.tools.test_bot_mode_side_window_marker import (
    target, AUTHOR, KEY, _db, _window, _append, _ready_authority, _side_argv, _dm, _windows,
)
from tools import bot_mode_dm


def test_busy_window_admission_refreshes_the_tab_stamp(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, '_default_home', lambda: str(target.parent.parent))
    _window(target, 'w1', 'micro1')
    with _db(target) as store:
        store.sessions.mark_side_window('w1', by=AUTHOR['id'], at=1000, key=KEY)
        _append(store, 'w1', 'user', 'the first DM, still being answered', timestamp=time.time())
    authority = _ready_authority(monkeypatch)
    monkeypatch.setattr(bot_mode_dm, '_wait_live_dm', lambda *args, **kwargs: 0)
    code = bot_mode_dm._run_delivery(_side_argv('micro1'), _dm(tmp_path), stdin_file=False,
        profile_home=target, author=AUTHOR, side_window=True, window_key=KEY)
    assert code == 0 and len(authority.calls) == 1
    assert _windows(target)[0]['at'] > 1000, _windows(target)
    assert _windows(target)[0]['by'] == AUTHOR['id']
