"""Producer-side masking of short bearer parameters and opaque assignment values."""

import json
import logging
import secrets
from functools import partial
from pathlib import Path

import pytest

from agent.redact import (
    RedactingFormatter, _redact_strict_url_credentials, redact_code_output, redact_log_urls, redact_sensitive_text,
    redact_terminal_output,
)
from agent.redact_phone_policy import redact_assistant_reply
from hermes_cli.kanban_db import redact_review_value
from tools.file_tools import read_file_tool, search_tool


def _warning(text):
    record = logging.LogRecord("tools.terminal", logging.WARNING, __file__, 0,
                               "tool error: %s", (text,), None)
    return RedactingFormatter().format(record)


_URL_SINKS = [redact_sensitive_text, partial(redact_sensitive_text, force=True),
              partial(redact_sensitive_text, redact_url_credentials=True), redact_log_urls,
              _warning, redact_review_value, redact_assistant_reply]
_SINK_IDS = ["default", "force", "strict", "log-urls", "warning", "review", "assistant"]


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
    assert '3|  "secret": "«redacted-secret»"' in output
    assert value not in output and value[:6] not in output and value[-4:] not in output
    assert not any(value[start:start + 6] in output for start in range(len(value) - 5))


def test_read_file_preserves_long_source_fixture(tmp_path):
    value = "aZ" + secrets.token_hex(16)
    path = tmp_path / "fixture.py"
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
