"""A turn that chose silence leaves no trace in a history read (session.resume, /history, REST pages).

Live, a bare silence marker (``NO_REPLY``) vanishes only where the turn was allowed to be silent: a
machinery wake (background process, delegation result, runtime notification), an unaddressed message,
or the Bot Chat. Every history projection must show the same thing the live turn showed: TUI
``/resume``, the dashboard's TUI and the iPhone app paint ``_history_to_messages`` as is, so the
marker row and the "Background Process Finished" row that woke a quiet silent turn are dropped there.
A person's own question answered with a marker-looking reply stays visible (it was shown live).
"""

from hermes_cli.web_routers.sessions import _project_for_display
from tui_gateway.server import _history_to_messages

WAKE = {"role": "user", "content": "[IMPORTANT: Background process proc_1 completed (exit 0).]",
        "display_kind": "process_complete"}


def _user(text, **extra):
    return {"role": "user", "content": text, **extra}


def _bot(text, **extra):
    return {"role": "assistant", "content": text, **extra}


def _texts(messages):
    return [(m["role"], m.get("text")) for m in messages]


def test_a_silent_wake_leaves_no_rows_in_the_resume_projection():
    history = [_user("hi"), _bot("hello"), WAKE, _bot("NO_REPLY"), _user("next"), _bot("ok")]
    assert _texts(_history_to_messages(history)) == [
        ("user", "hi"), ("assistant", "hello"), ("user", "next"), ("assistant", "ok")]


def test_a_persons_question_answered_with_a_marker_like_reply_stays_visible():
    for question, answer in (("what exact token do you end a silent turn with?", "`NO_REPLY`"),
                             ("did Dani reply to the email?", "No reply \U0001F615"),
                             ("is the alert loud or silent?", "Silent.")):
        assert _texts(_history_to_messages([_user(question), _bot(answer)])) == [
            ("user", question), ("assistant", answer)]


def test_the_bot_chat_hides_a_silent_reply_to_a_person_but_keeps_the_person():
    history = [_user("thanks, that's all"), _bot("NO_REPLY \u26a1")]
    assert _texts(_history_to_messages(history, silence_ok_session=True)) == [("user", "thanks, that's all")]
    assert _texts(_history_to_messages(history)) == [
        ("user", "thanks, that's all"), ("assistant", "NO_REPLY \u26a1")]


def test_an_unaddressed_message_may_end_silent():
    history = [_user("lunch?", display_metadata={"reply_expected": False}), _bot("NO_REPLY")]
    assert _texts(_history_to_messages(history)) == [("user", "lunch?")]


def test_a_wake_whose_turn_ran_a_tool_stays_visible():
    history = [WAKE, _bot("", tool_calls=[{"id": "c1", "type": "function",
                                           "function": {"name": "terminal", "arguments": "{}"}}]),
               {"role": "tool", "tool_call_id": "c1", "content": "ok"}, _bot("NO_REPLY")]
    messages = _history_to_messages(history)
    assert [m["role"] for m in messages] == ["user", "tool"]
    assert messages[0]["display_kind"] == "process_complete"


def test_a_wake_whose_turn_spoke_through_codex_message_items_stays_visible():
    codex = [{"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "Build finished: 3 failures"}]}]
    history = [WAKE, _bot("", codex_message_items=codex), _bot("NO_REPLY")]
    messages = _history_to_messages(history)
    assert messages[0]["display_kind"] == "process_complete"
    assert all(m.get("text") != "NO_REPLY" for m in messages)


def test_a_failed_turns_boundary_row_is_never_silence():
    failed = _bot("NO_REPLY", display_kind="failed_turn", display_metadata={
        "error": "provider 500", "error_surface": {"code": "provider_error", "layer": "provider", "retryable": True}})
    interrupted = _bot("NO_REPLY", display_metadata={"interrupted": True})
    for row in (failed, interrupted):
        assert [m["role"] for m in _history_to_messages([WAKE, row])] == ["user", "assistant"]


def test_a_marker_closing_a_wake_reply_is_dropped_but_a_persons_answer_is_kept_whole():
    """Live, only a turn that may end silent strips a closing marker; a person's answer is shown as written."""
    assert _texts(_history_to_messages([WAKE, _bot("Build is green.\n\nNO_REPLY")]))[-1] == (
        "assistant", "Build is green.")
    assert _texts(_history_to_messages([_user("the literal?"), _bot("The literal value is:\nNO_REPLY")])) == [
        ("user", "the literal?"), ("assistant", "The literal value is:\nNO_REPLY")]


def test_a_silent_final_that_also_spoke_public_commentary_stays_with_its_wake():
    from session_rules.silent_turns import silent_turns
    spoke = _bot("NO_REPLY", display_commentary=["Build is ready at https://example.test/report."])
    assert silent_turns([WAKE, spoke]).hidden == set()


def test_a_marker_kept_only_in_codex_message_items_is_silence_too():
    from session_rules.silent_turns import silent_turns
    codex = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "NO_REPLY"}]}]
    assert silent_turns([WAKE, _bot("", codex_message_items=codex, reasoning="nothing new")]).hidden == {0, 1}


def test_rest_pages_mark_silent_turn_rows_instead_of_dropping_them():
    """The desktop pages REST transcripts by backend row offset, so the rows stay and carry a mark."""
    rows = [_user("hi"), _bot("hello"), WAKE, _bot("NO_REPLY"), _user("thanks"), _bot("NO_REPLY")]
    projected = _project_for_display(rows)
    assert len(projected) == len(rows)
    assert [bool(r.get("silent_turn")) for r in projected] == [False, False, True, True, False, False]
    bot_chat = _project_for_display(rows, silence_ok_session=True)
    assert [bool(r.get("silent_turn")) for r in bot_chat] == [False, False, True, True, False, True]
    assert "silent_turn" not in rows[3]  # a display projection, never a mutation of the stored row


def test_rest_pages_carry_the_stripped_reply_only_where_the_turn_may_end_silent():
    rows = [_user("capital?"), _bot("Paris.\n\nNO_REPLY")]
    assert _project_for_display(rows, silence_ok_session=True)[1]["display_content"] == "Paris."
    assert "display_content" not in _project_for_display(rows)[1]


def test_a_cron_delivery_stays_when_the_bot_answers_it_with_silence():
    """A cron delivery's row carries the job's output for the reader (agent/display_row_typing.py): only the
    bot's marker vanishes, never the delivered output."""
    cron = _user('[Cronjob "Team report | Apex" output \u2014 scheduled job, not the user. Review it, act on '
                 'anything that needs action, and summarize for the chat.]\n\nApex morning run is done.',
                 display_kind="internal_notification")
    messages = _history_to_messages([cron, _bot("NO_REPLY")])
    assert [m["role"] for m in messages] == ["user"]
    assert "Apex morning run is done." in (messages[0].get("display_content") or messages[0]["text"])

