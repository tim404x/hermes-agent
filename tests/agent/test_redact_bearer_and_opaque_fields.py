"""Producer-side masking of short bearer parameters and opaque assignment values."""

import json
import logging
import secrets
from functools import partial
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode

import pytest

from agent.redact import (
    RedactingFormatter, _redact_strict_url_credentials, redact_code_output, redact_log_urls, redact_sensitive_text,
    redact_for_egress, redact_terminal_output, _redact_opaque_fields,
)
from agent.redact_phone_policy import redact_assistant_reply
from hermes_cli.kanban_db import redact_review_value
from tools.file_tools import read_file_tool, search_tool, write_file_tool


def _warning(text):
    record = logging.LogRecord("tools.terminal", logging.WARNING, __file__, 0,
                               "tool error: %s", (text,), None)
    return RedactingFormatter().format(record)


_URL_SINKS = [redact_sensitive_text, partial(redact_sensitive_text, force=True),
              partial(redact_sensitive_text, redact_url_credentials=True), redact_log_urls,
              _warning, redact_review_value, redact_assistant_reply, redact_for_egress]
_SINK_IDS = ["default", "force", "strict", "log-urls", "warning", "review", "assistant", "egress"]


@pytest.mark.parametrize("sink", _URL_SINKS, ids=_SINK_IDS)
def test_url_wrappers_and_encoded_bearers_preserve_every_nonsecret_byte(sink):
    value = "aZ" + secrets.token_urlsafe(24)
    for head, tail in (("[x](https://h/?t=", ")"), ("[x](https://h/?r=x&tk=", ")"),
                       ("<https://h/?tok=", "|label>"), ("`https://h/?T=", "`"),
                       ("/?t=", "]"), ("/?t=", "}"), ("/?t=", ","), ("/?t=", "!"),
                       ("/?t=", "."), ("/?t=", ". Next"), ("/?r=x&amp;t=", ""),
                       (r"/path\u003ft=", ""), (r"/?r=x\u0026t=", "")):
        for encoded in (value, urlencode({"t": value + "=="})[2:], value + "%2D%5f%3d"):
            assert sink(head + encoded + tail).endswith(head + "***" + tail)


def test_first_form_parameter_masks_only_in_a_complete_body():
    value = "aZ" + secrets.token_urlsafe(24)
    for sink in (redact_sensitive_text, redact_log_urls, redact_for_egress):
        assert sink(f"t={value}&r=x") == "t=***&r=x"
        assert sink(f"note t={value} outside a form") == f"note t={value} outside a form"


@pytest.mark.parametrize("sink", _URL_SINKS, ids=_SINK_IDS)
def test_bearer_short_url_is_masked_before_each_sink(sink):
    value = "aZ" + secrets.token_urlsafe(24)
    for prefix in ("https://eye.hubxyz.net/?t=", "/path?t=", "/?tk=", "#tok=", "&T=", ";%2574%4B="):
        assert sink(f"{prefix}{value}&r=timr").endswith(f"{prefix}***&r=timr")


@pytest.mark.parametrize("sink", _URL_SINKS, ids=_SINK_IDS)
def test_noncredential_short_params_and_youtube_ids_survive_each_sink(sink):
    urls = ["https://example.org/?t=" + value for value in (
        "30s", "1h2m3s", "90", "1732617600123", "2026-11-26T10:00:00Z",
        secrets.token_hex(16)[:22], "1" * 32, "a" * 32, "A" * 32,
        secrets.token_hex(16) + ":public",
    )]
    urls.append(f"https://youtube.com/watch?v={secrets.token_hex(16)[:11]}&list=PL{secrets.token_hex(16)}")
    for url in urls:
        assert sink(url).endswith(url)


def test_bearer_value_stops_at_escaped_quotes_and_keeps_padding_out_of_output():
    value = "sk-proj-" + secrets.token_hex(16) + "=="
    text = json.dumps(json.dumps({"url": "/?ToK=" + value}))
    for sink in (redact_sensitive_text, redact_log_urls, _redact_strict_url_credentials):
        assert sink(text) == text.replace(value, "***")


def test_bearer_floor_and_delimiters_preserve_neighboring_text():
    value = "aZ" + secrets.token_hex(16)[:22]
    for delimiter in ('"', "\\", "&", "#", ";", "\u00a0", "'", "<", ">"):
        text = f"/path?t={value}{delimiter}after"
        for sink in (redact_sensitive_text, redact_log_urls):
            assert sink(text) == text.replace(value, "***")
            assert sink(text.replace(value, value[:-1])) == text.replace(value, value[:-1])


def test_read_file_masks_line_numbered_json_without_retaining_secret_material(tmp_path):
    value = "aZ" + secrets.token_urlsafe(24)
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"rater": "a", "secret": value}, indent=2))
    output = json.loads(read_file_tool(str(path)))["content"]
    assert output == '1|{\n2|  "rater": "a",\n3|  "secret": "«redacted-secret»"\n4|}'


@pytest.mark.parametrize("suffix", [".py", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".kts", ".ps1",
                                    ".ex", ".exs", ".r", ".jl", ".hs", ".zig", ".tf", ".hcl"])
def test_read_file_preserves_long_source_fixture(tmp_path, suffix):
    value = "aZ" + secrets.token_hex(16)
    path = tmp_path / ("fixture" + suffix)
    source = "CONFIG = " + json.dumps({"secret": value, "apiKey": "test"})
    path.write_text(source)
    assert json.loads(read_file_tool(str(path)))["content"] == "1|" + source


def test_extracted_document_masks_opaque_fields_before_returning_content(tmp_path):
    value = "aZ" + secrets.token_hex(16)
    cell = {"cell_type": "markdown", "metadata": {}, "source": [json.dumps({"secret": value})]}
    path = tmp_path / "data.ipynb"
    path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [cell]}))
    result = json.loads(read_file_tool(str(path)))
    assert result["extracted_document"] and '"secret": "«redacted-secret»"' in result["content"]
    assert value not in result["content"]


def test_search_classifies_each_matching_file_not_the_directory(tmp_path):
    value = "aZ" + secrets.token_urlsafe(24)
    field = json.dumps({"secret": value})
    (tmp_path / "fixture.py").write_text(field)
    (tmp_path / "records").write_text(field)
    result = json.loads(search_tool('"secret"', path=str(tmp_path)))
    hits = {Path(hit["path"]).name: hit["content"] for hit in result["matches"]}
    assert hits == {"fixture.py": field, "records": field.replace(value, "«redacted-secret»")}


def test_default_json_secret_gets_a_full_mask():
    value = secrets.token_hex(16)
    assert redact_sensitive_text(json.dumps({"rater": "a", "secret": value})) == '{"rater": "a", "secret": "***"}'


def test_prefixed_assignment_retains_vendor_display_mask():
    value = "sk-proj-" + secrets.token_hex(16)
    assert json.loads(redact_sensitive_text(json.dumps({"api_key": value})))["api_key"] == value[:6] + "..." + value[-4:]


@pytest.mark.parametrize("template", ["DB_PASSWORD={}", "api_token={}", "db.password={}", 'password = "{}"',
                                      '"DB_PASSWORD": "{}"', r'\"secret\": \"{}\"', r'\"DB_PASSWORD\": \"{}\"',
                                      "password: {}", "SESSION_COOKIE={}"])
def test_prefixless_assignment_keeps_no_head_or_tail(template):
    value = secrets.token_hex(16)
    assert redact_sensitive_text(template.format(value)) == template.format("***")


@pytest.mark.parametrize("prefix", ["export ", "INFO=message "])
def test_execute_code_env_assignment_keeps_no_head_or_tail(prefix):
    value = secrets.token_hex(16)
    assert redact_code_output(f"{prefix}DB_PASSWORD={value}") == f"{prefix}DB_PASSWORD=***"


def test_terminal_masks_opaque_python_repr_field():
    value = "aZ" + secrets.token_urlsafe(24)
    assert redact_terminal_output(repr({"rater": "a", "secret": value}), "python3 dump.py") == repr({"rater": "a", "secret": "***"})


@pytest.mark.parametrize("encode", [json.dumps, lambda fields: json.dumps(json.dumps(fields)), repr])
def test_execute_code_masks_only_strong_opaque_fields_without_changing_encoding(encode):
    value = "aZ" + secrets.token_hex(16) + "+/...~=0"
    text = encode({"client_secret": value, "label": value})
    assert redact_code_output(text) == encode({"client_secret": "***", "label": value})


def test_opaque_field_requires_twenty_characters_and_two_character_classes():
    value = "aZ" + secrets.token_hex(16)[:18]
    for candidate in (value[:-1], "a" * 20, "9" * 20, value + ":", "«redacted-secret»"):
        text = json.dumps({"secret": candidate})
        assert redact_code_output(text) == text
    assert redact_code_output(json.dumps({"secret": value})) == '{"secret": "***"}'


def test_code_file_preserves_short_prose_placeholder_and_lookup_fields():
    for key, value in (("apiKey", "test"), ("token", "abc123"), ("password", "correct horse battery staple"),
                       ("secret", "example-Sample-Token-12345"), ("token", "process.env.EXAMPLE_TOKEN_12345")):
        for encode in (repr, json.dumps):
            field = encode({key: value})
            assert redact_sensitive_text(field, code_file=True, opaque_fields=True) == field


def test_multiline_field_keeps_gutters_and_search_context(tmp_path):
    value = "aZ" + secrets.token_urlsafe(24)
    path = tmp_path / "data.json"
    path.write_text('{"secret":\n  "' + value + '"}')
    assert json.loads(read_file_tool(str(path)))["content"] == '1|{"secret":\n2|  "«redacted-secret»"}'
    hits = json.loads(search_tool('"secret"', path=str(path), context=1))["matches"]
    assert [hit["content"] for hit in hits] == ['{"secret":', '  "«redacted-secret»"}']
    assert json.loads(write_file_tool(str(path), "{}"))["stale_write_blocked"]


def test_read_line_cap_cannot_expose_a_value_missing_only_its_closing_quote(tmp_path, monkeypatch):
    value = "aZ" + secrets.token_hex(16)
    path = tmp_path / "capped.json"
    prefix = '{"secret": "'
    path.write_text(prefix + value + '"}')
    monkeypatch.setattr("tools.tool_output_limits.get_max_line_length", lambda: len(prefix + value))
    content = json.loads(read_file_tool(str(path)))["content"]
    assert content.startswith("1|" + prefix + "«redacted-secret»")
    assert value not in content


@pytest.mark.parametrize("visible", [5, 24])
def test_search_masks_secret_straddling_match_cap(tmp_path, visible):
    value = "aZ" + secrets.token_urlsafe(32)
    path = tmp_path / "capped.json"
    prefix = '{"secret": "'.rjust(500 - visible)
    path.write_text(prefix + value + '", "label": "public"}')
    hit, = json.loads(search_tool('"secret"', path=str(path)))["matches"]
    assert hit["content"].startswith(prefix)
    assert not hit["content"][len(prefix):].startswith(value[:visible])


def test_complete_quoted_assignment_is_masked_and_still_parseable():
    words = [secrets.token_hex(6) for _ in range(3)]
    for encode in (json.dumps, lambda fields: json.dumps(json.dumps(fields))):
        for suffix in ("", "\\"):
            rendered = redact_sensitive_text(encode({"password": '"'.join(words) + suffix, "next": 1}))
            assert json.loads(rendered) == json.loads(encode({"password": "***", "next": 1}))
    for quote in ('"', "'"):
        for name in ("DB_PASSWORD", "db_password", "db.password", "password"):
            assert redact_sensitive_text(f"{name}={quote}{' '.join(words)}{quote} NEXT=1") == f"{name}={quote}***{quote} NEXT=1"


@pytest.mark.parametrize("prefix", ["SG", "AQ", "xox", "ya29", "sk-proj-"])
def test_only_complete_vendor_tokens_retain_assignment_hints(prefix):
    value = prefix + secrets.token_urlsafe(32)
    expected = value[:6] + "..." + value[-4:] if prefix == "sk-proj-" else "***"
    for template, sink in (("DB_PASSWORD={}", redact_sensitive_text), ("DB_PASSWORD={}", redact_code_output),
                           ("secret: {}", redact_sensitive_text), ("cmd --secret={}", redact_sensitive_text),
                           ("machine host login user password {}", partial(redact_terminal_output, command="cat ~/.netrc")),
                           ("//host/:_authToken={}", partial(redact_terminal_output, command="cat ~/.npmrc")),
                           ("host:5432:db:user:{}", partial(redact_terminal_output, command="cat ~/.pgpass"))):
        assert sink(template.format(value)) == template.format(expected)
    if prefix != "sk-proj-":
        fake_hint = (prefix + "ab" + secrets.token_hex(4))[:6] + "..." + secrets.token_hex(2)
        assert redact_sensitive_text("DB_PASSWORD=" + fake_hint) == "DB_PASSWORD=***"


@pytest.mark.parametrize("sink", [redact_terminal_output, redact_code_output])
def test_runtime_json_and_repr_agree_on_dotted_and_hyphenated_keys(sink):
    value = "aZ" + secrets.token_urlsafe(24)
    keys = ("client-secret", "service.client-secret", "x-api-key", "api-key", "x-goog-api-key", "client-key-data")
    for encode in (repr, json.dumps, lambda fields: json.dumps(json.dumps(fields))):
        fields = dict.fromkeys(keys, value)
        assert sink(encode(fields)) == encode(dict.fromkeys(keys, "***"))


def test_json_keeps_qualified_secret_names_outside_the_affixed_key_grammar():
    value = "aZ" + secrets.token_urlsafe(24)
    fields = {"应用_private_key": value, "a" * 130 + "-signing-key": value}
    for sink in (redact_sensitive_text, redact_code_output):
        assert json.loads(sink(json.dumps(fields, ensure_ascii=False))) == dict.fromkeys(fields, "***")


def test_config_metadata_and_paths_round_trip_through_read_and_write(tmp_path):
    config = dict.fromkeys(("secret_id", "secret_name", "secret_version_id", "token_model", "token_path",
                            "secret_env_name", "secret_file", "api_key_ref"), "prod-v2-" + secrets.token_hex(16))
    config.update(private_key="/Users/x/.ssh/id_ed25519", credentials="/home/u/.config/gcloud2/adc.json")
    path = tmp_path / "config.json"
    original = json.dumps(config, indent=2)
    path.write_text(original)
    assert redact_sensitive_text(original) == original
    content = json.loads(read_file_tool(str(path)))["content"]
    recovered = "\n".join(line.partition("|")[2] for line in content.splitlines())
    assert recovered == original
    assert "error" not in json.loads(write_file_tool(str(path), recovered + "\n"))
    assert path.read_text() == original + "\n"


@pytest.mark.parametrize("flood", [True, False], ids=["backslashes", "benign-json"])
def test_opaque_scan_completes_within_budget(flood):
    text = r'\"secret\" :' + "\\" * 100_000 + "x" if flood else json.dumps({f"label{idx}": "plain" for idx in range(6000)})
    assert len(text) >= 100_000
    started = perf_counter()
    assert _redact_opaque_fields(text, True, True, True) == text
    assert perf_counter() - started < 0.5


def test_vendor_json_display_is_independent_of_layout_and_number_of_passes():
    value = "sk-proj-" + secrets.token_urlsafe(32)
    hint = value[:6] + "..." + value[-4:]
    for indent in (None, 2):
        text = json.dumps({"api_key": value}, indent=indent)
        expected = text.replace(value, hint)
        assert redact_sensitive_text(text) == redact_sensitive_text(expected) == expected
    assert _warning(json.dumps({"api_key": value})).endswith(json.dumps({"api_key": hint}))


def test_runtime_structured_auth_and_cookie_headers_are_masked():
    value = "aZ" + secrets.token_urlsafe(24)
    fields = {"Authorization": "Bearer " + value, "Cookie": "sid=" + value, "label": value}
    for sink in (redact_terminal_output, redact_code_output):
        assert json.loads(sink(json.dumps(fields))) == {**fields, "Authorization": "***", "Cookie": "***"}


@pytest.mark.parametrize("quote", ['"', "'"])
def test_transport_clipped_data_field_does_not_leak_through_preview(tmp_path, quote):
    value = "aZ" + secrets.token_urlsafe(10_000)
    prefix = "{" + quote + "secret" + quote + ": " + quote
    path = tmp_path / "wide.txt"
    path.write_text(prefix + value + quote + "}")
    assert json.loads(read_file_tool(str(path)))["content"].startswith("1|" + prefix + "«redacted-secret»")
    hit, = json.loads(search_tool("secret", path=str(path)))["matches"]
    assert hit["content"].startswith(prefix + "«redacted-secret»")
