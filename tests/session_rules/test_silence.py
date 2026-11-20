"""The silence-marker rule lives in ``session_rules`` so storage and history projections can use it without
loading ``gateway`` (storage import boundary, 19 Nov 2026: ``session_rules/preview_text.py`` pulled
``gateway.response_filters`` into every storage runtime import)."""
import subprocess
import sys


def test_session_rules_silence_users_import_without_loading_gateway():
    probe = ("import sys, session_rules.silence, session_rules.preview_text, session_rules.silent_turns; "
             "print(sorted(m for m in sys.modules if m == 'gateway' or m.startswith('gateway.')))")
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == '[]', out.stdout


def test_gateway_filters_use_the_one_session_rules_definition():
    from gateway import response_filters
    from session_rules import silence
    for name in ('LIVE_GATEWAY_SILENT_MARKERS', 'MACHINERY_DISPLAY_KINDS', 'INTERNAL_NOTIFICATION_DISPLAY_KIND',
                 'PRODUCER_NOTICE_DISPLAY_KINDS', 'is_intentional_silence_response', 'strip_trailing_silence_marker',
                 'is_partial_silence_marker', 'ends_with_partial_silence_marker', 'is_machinery_display_kind',
                 'is_autonomous_silence_response', 'SilenceMarkerStreamHold'):
        assert getattr(response_filters, name) is getattr(silence, name), name
    assert silence.strip_trailing_silence_marker("Here is the answer.\n\nNO_REPLY") == "Here is the answer."
    assert silence.is_intentional_silence_response("NO_REPLY") and not silence.is_intentional_silence_response("No")
