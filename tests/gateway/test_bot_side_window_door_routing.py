"""A DM into a busy side window runs in the session ``chat -c <title>`` resolves (R1), and the door
admits only DM side windows, never an open-tab stamp (R5)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.gateway.test_peer_dm_bot_chat_authority import peer, AUTHOR
from tests.gateway.test_peer_dm_bot_chat_headless import _local_owner
from gateway.session_local import create_local_session
from gateway.session_local_title import title_new_session, resolve_titled_session
from persistence.conversation.messages import NewMessage


@pytest.mark.asyncio
async def test_busy_window_admits_into_the_numbered_chat_the_transport_resolves(peer, monkeypatch):
    from tools import bot_mode_dm, bot_live_delivery
    from hermes_cli import gateway_runtime
    authority = peer.authority
    home = authority.persistence.scope.profile_dir
    monkeypatch.setenv('HERMES_HOME', str(home))
    connection = _local_owner(authority)
    authority._native_legacy_transports[connection.actor.transport_id] = connection.actor
    refs = []
    for i, title in enumerate(('Vendor', 'Vendor #2')):
        ref = create_local_session(authority, connection.actor, {'request_id': f'review-{i}',
            'source': 'gui', 'cwd': str(home), 'model': 'fixture', 'toolsets': []})
        title_new_session(authority, ref, title)
        authority.persistence.sessions.mark_side_window(ref.session_id, by=AUTHOR['id'], at=1000+i,
                                                       key='cto>default|topic:vendor')
        authority.persistence.messages.append(ref.session_id, NewMessage(role='user', content='working'))
        refs.append(ref)
    expected = resolve_titled_session(authority, connection.actor, 'Vendor')
    assert expected.session_id == refs[1].session_id
    loop = asyncio.get_running_loop()
    def deliver(home, params):
        from gateway.session_bot import deliver as admit
        return asyncio.run_coroutine_threadsafe(admit(connection, params), loop).result(timeout=20)
    monkeypatch.setattr(bot_live_delivery, 'authority_delivery', deliver)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', lambda home, timeout=5:
        SimpleNamespace(state='ready', endpoint=SimpleNamespace(instance_id=authority.instance_id)))
    monkeypatch.setattr(bot_mode_dm, '_default_home', lambda: str(home))
    dm_file = home / 'review-dm.txt'
    dm_file.write_text('Message from bot: please finish the vendor review.')
    argv = ['hermes', '-p', 'default', 'chat', '--in', '~', '-c', 'Vendor', '--create-if-missing', '-Q']
    code = await asyncio.to_thread(bot_mode_dm._run_delivery, argv, str(dm_file), stdin_file=False,
        profile_home=home, author=AUTHOR, side_window=True)
    assert code == 0
    assert peer.executed[-1][0] == expected.session_id, (peer.executed, [r.session_id for r in refs])


def test_door_refuses_an_open_tab_stamp_but_accepts_a_dm_side_window(peer, monkeypatch):
    from tools.bot_live_delivery import door_accepts_title
    authority = peer.authority
    home = authority.persistence.scope.profile_dir
    monkeypatch.setenv('HERMES_HOME', str(home))
    connection = _local_owner(authority)
    refs = {}
    for i, title in enumerate(('Opened by hand', 'Keyed window')):
        ref = create_local_session(authority, connection.actor, {'request_id': f'door-{i}',
            'source': 'gui', 'cwd': str(home), 'model': 'fixture', 'toolsets': []})
        title_new_session(authority, ref, title)
        refs[title] = ref
    # ``hermes sessions open-tab --by cto``: a tab, never a door target.
    authority.persistence.sessions.mark_side_window(refs['Opened by hand'].session_id, by='bot:cto')
    authority.persistence.sessions.mark_side_window(refs['Keyed window'].session_id, by=AUTHOR['id'],
                                                   key='cto>default|topic:keyed')
    assert not door_accepts_title(home, 'Opened by hand')
    assert door_accepts_title(home, 'Keyed window')
    from tools.bot_live_delivery import door_title_kind
    assert door_title_kind(home, 'Keyed window') == 'side_window'
    assert door_title_kind(home, 'Bot Chat') == 'bot_chat'
