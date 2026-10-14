"""The Bot door admits a ``bot-chat`` cron delivery into the profile's dedicated cron chat.

``cron.bot_chat_title`` (e.g. "Crons") or a job's own ``bot_chat_title`` names the chat; the door
mints it on first delivery, reuses it after, keeps the Bot Chat untouched, and refuses any title
the operator did not configure, so a producer can never steer a turn into an arbitrary chat.

Real: SessionAuthority, ``gateway.session_bot.deliver`` (the door), the FIFO and the receipt
mailbox. Seam: ``execute_finite_admission`` answers ``pong`` (fixture ``bot``).
"""
import json

import pytest

from tests.waiting import async_wait_until

from hermes_state_runtime import RuntimeStoreError, list_session_admissions

pytest_plugins = ['tests.gateway.test_session_bot_retry']


@pytest.fixture
def door(bot, monkeypatch):
    """``bot`` whose config loader also takes the profile path: a chat the door mints follows
    its profile (``follow_profile_config``), so its creation and first turn read the profile's
    config.yaml, which must name a model."""
    from gateway import run
    config = {'platform_toolsets': {'cli': []}, 'model': {
        'provider': 'custom', 'base_url': 'http://127.0.0.1:1/v1', 'default': 'fixture', 'api_key': 'fixture'}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args, **kwargs: config)
    return bot


def _config(home, title):
    (home / 'config.yaml').write_text(f'cron:\n  bot_chat_title: {title!r}\n', encoding='utf-8')


def _admitted(bot, session_id):
    return sorted(r['request_id'] for r in list_session_admissions(bot.authority.db, session_id=session_id,
                                                                   pending_only=False))


async def _settled(bot, key):
    from tools.bot_live_delivery import read_delivery_result
    await async_wait_until(lambda: (read_delivery_result(bot.home, key) or {}).get('status') == 'settled',
                           timeout=10, what='the cron delivery to settle')


@pytest.mark.asyncio
async def test_cron_delivery_lands_in_the_configured_chat_not_the_bot_chat(door):
    bot = door
    from gateway.session_bot import deliver
    _config(bot.home, 'Crons')
    first = await deliver(bot.connection, dict(id='a' * 32, profile='default', message='digest 1', title='Crons'))
    crons = bot.authority.db.get_session_by_title('Crons')
    assert crons is not None and crons['id'] != bot.chat
    assert first['session_id'] == crons['id']
    await _settled(bot, 'a' * 32)
    # The next delivery reuses the same chat; the Bot Chat never sees either.
    second = await deliver(bot.connection, dict(id='b' * 32, profile='default', message='digest 2', title='Crons',
                                                session_id=crons['id']))
    assert second['session_id'] == crons['id']
    await _settled(bot, 'b' * 32)
    assert _admitted(bot, crons['id']) == ['bot:' + 'a' * 32, 'bot:' + 'b' * 32]
    assert _admitted(bot, bot.chat) == []
    # The receipt records its chat: the one allowed retry re-targets the same title.
    from tools.bot_live_delivery import read_delivery_result
    assert (read_delivery_result(bot.home, 'a' * 32) or {}).get('title') == 'Crons'


@pytest.mark.asyncio
async def test_untitled_delivery_still_targets_the_bot_chat(door):
    bot = door
    from gateway.session_bot import deliver
    _config(bot.home, 'Crons')
    receipt = await deliver(bot.connection, dict(id='c' * 32, profile='default', message='brief'))
    assert receipt['session_id'] == bot.chat
    await _settled(bot, 'c' * 32)
    assert bot.authority.db.get_session_by_title('Crons') is None


@pytest.mark.asyncio
async def test_an_existing_local_chat_with_that_title_is_reused(door):
    bot = door
    from gateway.session_bot import deliver
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    _config(bot.home, 'Crons')
    chat = create_local_session(bot.authority, bot.connection.actor, {
        'request_id': 'old-crons', 'source': 'gui', 'cwd': str(bot.home), 'model': 'fixture', 'toolsets': []})
    title_new_session(bot.authority, chat, 'Crons')
    receipt = await deliver(bot.connection, dict(id='d' * 32, profile='default', message='digest', title='Crons'))
    assert receipt['session_id'] == chat.session_id
    await _settled(bot, 'd' * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize('title', ['Crons', 'Somebody else\'s chat', 7])
async def test_titles_the_operator_did_not_configure_are_refused(door, title):
    bot = door
    from gateway.session_bot import deliver
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        await deliver(bot.connection, dict(id='e' * 32, profile='default', message='x', title=title))
    assert bot.authority.db.get_session_by_title('Crons') is None
    assert _admitted(bot, bot.chat) == []


@pytest.mark.asyncio
async def test_a_job_pinned_title_is_admissible(door):
    bot = door
    from gateway.session_bot import deliver
    (bot.home / 'cron').mkdir(exist_ok=True)
    (bot.home / 'cron' / 'jobs.json').write_text(json.dumps({'jobs': [
        dict(id='w', deliver='bot-chat', bot_chat_title='Weekly reports')]}), encoding='utf-8')
    receipt = await deliver(bot.connection, dict(id='f' * 32, profile='default', message='w', title='Weekly reports'))
    assert receipt['session_id'] == bot.authority.db.get_session_by_title('Weekly reports')['id']
    await _settled(bot, 'f' * 32)


@pytest.mark.asyncio
async def test_same_id_with_another_title_is_a_conflict(door):
    bot = door
    from gateway.session_bot import deliver
    _config(bot.home, 'Crons')
    await deliver(bot.connection, dict(id='9' * 32, profile='default', message='digest', title='Crons'))
    await _settled(bot, '9' * 32)
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        await deliver(bot.connection, dict(id='9' * 32, profile='default', message='digest'))
    again = await deliver(bot.connection, dict(id='9' * 32, profile='default', message='digest', title='Crons'))
    assert again['status'] == 'settled'
