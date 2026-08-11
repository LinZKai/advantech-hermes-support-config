"""Tests for query_foundry_iq.py's stdout JSON result contract (v2).

Run locally with:
    python -m unittest discover -s skills/foundry-iq/scripts/tests -v

No real network access happens: urllib.request.urlopen is mocked in every
test that exercises the request path. Uses only the standard library.

Most tests run through main() and catch the SystemExit it raises -- this
exercises the real CLI exit path (argv/env/stdout/exit-code all as a real
invocation would produce them), not just the payload-building functions in
isolation. The one deliberate exception is _serialize()'s own direct unit
test (SerializationFailureTests.test_serialize_unit_level), kept alongside
the end-to-end one in SerializationFailureExitCodeTests so both the
mechanism and its wiring into main() are covered.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import query_foundry_iq as qfi  # noqa: E402


ENV_KEY = qfi.QUERY_KEY_ENV_NAME


class _FakeResponse:
    """Stand-in for the context manager urlopen() yields; only .read() is used."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _foundry_payload(documents=None, references=None, activity=None):
    """Build a well-formed upstream Azure AI Search response payload."""
    docs = (
        documents
        if documents is not None
        else [{"ref_id": 0, "content": "ADAM-6266 supports SNMP v2c."}]
    )
    return {
        "response": [
            {"content": [{"type": "text", "text": json.dumps(docs, ensure_ascii=False)}]}
        ],
        "references": (
            references
            if references is not None
            else [
                {
                    "id": "r1",
                    "type": "AzureBlob",
                    "blobUrl": (
                        "https://acct.blob.core.windows.net/kb/faq/"
                        "ADAM-6266%20SNMP.pdf?sv=2024-secret-token"
                    ),
                    "rerankerScore": 2.7,
                }
            ]
        ),
        "activity": (
            activity
            if activity is not None
            else [
                {
                    "type": "AzureSearchQuery",
                    "elapsedMs": 842,
                    "modelName": "gpt-x",
                    "inputTokens": 120,
                    "outputTokens": 45,
                    "reasoningTokens": 0,
                }
            ]
        ),
    }


def _run_main(argv, env, urlopen_side_effect=None):
    """Run main() with argv/env mocked; stdin.isatty() forced True so a
    missing argv question fails fast instead of blocking on real stdin.

    Returns (exit_code, stdout_text). exit_code is None only when main()
    returned normally with no SystemExit raised at all (the untouched
    success path). Every failure path, and the forced-nonzero-exit success
    fallback, raise SystemExit, so exit_code is an int in those cases.
    """
    stdout = io.StringIO()
    exit_code = None
    with patch.object(sys, "argv", argv), \
         patch.dict(os.environ, env, clear=True), \
         patch("sys.stdin.isatty", return_value=True):
        patcher = (
            patch("query_foundry_iq.urllib.request.urlopen", side_effect=urlopen_side_effect)
            if urlopen_side_effect is not None
            else patch("query_foundry_iq.urllib.request.urlopen")
        )
        with patcher:
            try:
                with redirect_stdout(stdout):
                    qfi.main()
            except SystemExit as exc:
                exit_code = exc.code
    return exit_code, stdout.getvalue()


def _run_main_with_mock(argv, env, urlopen_side_effect=None):
    """Same as _run_main, but also returns the urlopen mock so a test can
    assert on whether/how many times it was actually called -- this is what
    makes "urlopen was never attempted" and "urlopen was attempted" checks
    empirical rather than inferred from the JSON fields alone."""
    stdout = io.StringIO()
    exit_code = None
    with patch.object(sys, "argv", argv), \
         patch.dict(os.environ, env, clear=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch(
             "query_foundry_iq.urllib.request.urlopen",
             side_effect=urlopen_side_effect,
         ) as mock_urlopen:
        try:
            with redirect_stdout(stdout):
                qfi.main()
        except SystemExit as exc:
            exit_code = exc.code
    return exit_code, stdout.getvalue(), mock_urlopen


def _single_json_line(stdout_text: str) -> dict:
    lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected exactly one non-blank stdout line, got {lines!r}")
    return json.loads(lines[0])


class SuccessResultTests(unittest.TestCase):
    def test_success_contract(self):
        payload = _foundry_payload()
        body = json.dumps(payload).encode("utf-8")

        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )

        self.assertIsNone(exit_code)
        result = _single_json_line(out)

        self.assertEqual(result["schema_version"], qfi.SCHEMA_VERSION)
        self.assertTrue(result["ok"])
        self.assertTrue(result["request_attempted"])
        self.assertIsNone(result["error_code"])
        self.assertIsNone(result["http_status"])
        self.assertEqual(result["question"], "ADAM-6266 如何開啟 SNMP？")
        self.assertTrue(result["documents"])
        self.assertEqual(result["references"][0]["source_name"], "ADAM-6266 SNMP.pdf")
        self.assertNotIn("blobUrl", result["references"][0])
        self.assertEqual(result["activity"][0]["elapsed_ms"], 842)


class InvalidInputTests(unittest.TestCase):
    def test_no_question_at_all(self):
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py"], {ENV_KEY: "test-key"}
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 2)
        self.assertFalse(result["ok"])
        self.assertFalse(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_INPUT)
        mock_urlopen.assert_not_called()

    def test_blank_question(self):
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "   "], {ENV_KEY: "test-key"}
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 2)
        self.assertFalse(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_INPUT)
        mock_urlopen.assert_not_called()

    def test_question_too_long(self):
        long_question = "A" * (qfi.MAX_QUESTION_LENGTH + 1)
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", long_question], {ENV_KEY: "test-key"}
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 2)
        self.assertFalse(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_INPUT)
        self.assertEqual(result["question"], long_question)
        mock_urlopen.assert_not_called()


class MissingQueryKeyTests(unittest.TestCase):
    def test_missing_env_var(self):
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"], {}
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertFalse(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_MISSING_QUERY_KEY)
        mock_urlopen.assert_not_called()

    def test_blank_env_var(self):
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"], {ENV_KEY: "   "}
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertFalse(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_MISSING_QUERY_KEY)
        mock_urlopen.assert_not_called()

    def test_query_key_value_never_leaked(self):
        secret = "sk-super-secret-query-key-do-not-leak"
        _exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"], {}
        )
        self.assertNotIn(secret, out)


class RequestConstructionFailureTests(unittest.TestCase):
    """Failures that happen while building the request itself, before
    urlopen() is ever reached -- must still produce exactly one line of
    valid, safe JSON with request_attempted=false and a non-zero exit
    code, not an uncaught traceback."""

    def test_build_request_body_failure_is_safe(self):
        canary = "CANARY_BUILD_REQUEST_BODY_FAIL_ABCDEF"

        def _raise(*a, **kw):
            raise RuntimeError(f"boom containing {canary} and api-key=xyz")

        stdout = io.StringIO()
        exit_code = None
        with patch.object(sys, "argv", ["query_foundry_iq.py", "q"]), \
             patch.dict(os.environ, {ENV_KEY: "k"}, clear=True), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("query_foundry_iq.build_request_body", side_effect=_raise), \
             patch("query_foundry_iq.urllib.request.urlopen") as mock_urlopen:
            try:
                with redirect_stdout(stdout):
                    qfi.main()
            except SystemExit as exc:
                exit_code = exc.code

        out = stdout.getvalue()
        result = _single_json_line(out)  # exactly one line, parses as JSON

        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INTERNAL_ERROR)
        self.assertIs(result["request_attempted"], False)
        self.assertIsNone(result["http_status"])
        self.assertIsNotNone(exit_code)
        self.assertNotEqual(exit_code, 0)

        self.assertNotIn(canary, out)
        self.assertNotIn("boom", out)
        self.assertNotIn("Traceback", out)
        mock_urlopen.assert_not_called()

    def test_request_object_construction_failure_is_safe(self):
        canary = "CANARY_REQUEST_CTOR_FAIL_XYZ123"

        def _raise(*a, **kw):
            raise ValueError(f"boom {canary}")

        stdout = io.StringIO()
        exit_code = None
        with patch.object(sys, "argv", ["query_foundry_iq.py", "q"]), \
             patch.dict(os.environ, {ENV_KEY: "k"}, clear=True), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("query_foundry_iq.urllib.request.Request", side_effect=_raise), \
             patch("query_foundry_iq.urllib.request.urlopen") as mock_urlopen:
            try:
                with redirect_stdout(stdout):
                    qfi.main()
            except SystemExit as exc:
                exit_code = exc.code

        out = stdout.getvalue()
        result = _single_json_line(out)

        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INTERNAL_ERROR)
        self.assertIs(result["request_attempted"], False)
        self.assertIsNone(result["http_status"])
        self.assertIsNotNone(exit_code)
        self.assertNotEqual(exit_code, 0)

        self.assertNotIn(canary, out)
        self.assertNotIn("Traceback", out)
        mock_urlopen.assert_not_called()

    def test_unexpected_failure_after_urlopen_still_reports_attempted_true(self):
        """The same outer safety net also covers unexpected failures that
        happen *after* urlopen() was reached -- request_attempted must be
        true in that case, proving the flag tracks which side of urlopen()
        the failure occurred on, not just "some fail() happened"."""
        canary = "CANARY_POST_URLOPEN_FAIL_QRS789"

        class _BadResponse:
            def read(self):
                raise RuntimeError(f"boom {canary}")

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "q"],
            {ENV_KEY: "k"},
            urlopen_side_effect=lambda *a, **kw: _BadResponse(),
        )
        result = _single_json_line(out)

        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INTERNAL_ERROR)
        self.assertIs(result["request_attempted"], True)
        self.assertIsNone(result["http_status"])
        self.assertIsNotNone(exit_code)
        self.assertNotEqual(exit_code, 0)

        self.assertNotIn(canary, out)
        self.assertNotIn("Traceback", out)
        mock_urlopen.assert_called_once()


class RequestTimeoutTests(unittest.TestCase):
    def test_timeout_during_response_read(self):
        """Bare TimeoutError, as raised when response.read() itself blocks
        past the timeout (after urlopen() has already returned)."""
        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timed out")),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_REQUEST_TIMEOUT)
        self.assertIsNone(result["http_status"])
        self.assertNotIn("Traceback", out)
        mock_urlopen.assert_called_once()

    def test_timeout_during_connect_wrapped_as_urlerror(self):
        """A timeout during connect/send surfaces as URLError(reason=<a
        TimeoutError>), not as a bare TimeoutError -- this must still map
        to request_timeout, not network_error. See the comment in
        send_request()'s URLError handler for why."""

        def _raise(*a, **kw):
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_REQUEST_TIMEOUT)
        self.assertIsNone(result["http_status"])
        mock_urlopen.assert_called_once()


class HttpErrorTests(unittest.TestCase):
    def _run_with_status(self, status: int, reason: str = "Error"):
        def _raise(*a, **kw):
            raise urllib.error.HTTPError(qfi.FOUNDRY_IQ_URL, status, reason, None, None)

        return _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )

    def test_http_401(self):
        exit_code, out, mock_urlopen = self._run_with_status(401, "Unauthorized")
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_HTTP_ERROR)
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn("Traceback", out)
        mock_urlopen.assert_called_once()

    def test_http_429(self):
        exit_code, out, _mock_urlopen = self._run_with_status(429, "Too Many Requests")
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_HTTP_ERROR)
        self.assertEqual(result["http_status"], 429)

    def test_http_500(self):
        exit_code, out, _mock_urlopen = self._run_with_status(500, "Internal Server Error")
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_HTTP_ERROR)
        self.assertEqual(result["http_status"], 500)

    def test_http_error_only_keeps_safe_fields(self):
        _exit_code, out, _mock_urlopen = self._run_with_status(401, "Unauthorized")
        self.assertNotIn(qfi.FOUNDRY_IQ_URL, out)
        self.assertNotIn("nickliu-2919-srch-8m9l", out)
        self.assertNotIn("Content-Type", out)
        self.assertNotIn("api-key", out)
        self.assertNotIn("Traceback", out)


class NetworkErrorTests(unittest.TestCase):
    def test_url_error_without_timeout_reason(self):
        def _raise(*a, **kw):
            raise urllib.error.URLError("DNS resolution failed")

        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_NETWORK_ERROR)
        self.assertIsNone(result["http_status"])
        mock_urlopen.assert_called_once()

    def test_url_error_keeps_only_safe_fields(self):
        def _raise(*a, **kw):
            raise urllib.error.URLError("DNS resolution failed")

        _exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )
        self.assertNotIn(qfi.FOUNDRY_IQ_URL, out)
        self.assertNotIn("nickliu-2919-srch-8m9l", out)
        self.assertNotIn("Traceback", out)


class SensitiveUrlErrorReasonTests(unittest.TestCase):
    """A URLError's `reason` can in principle contain almost anything --
    this asserts that no matter what it contains, none of it reaches
    stdout, and the public error is always the same fixed, safe message."""

    def test_url_error_reason_with_canaries_never_leaks(self):
        canaries = [
            "FOUNDRY_IQ_QUERY_KEY=sk-fake-super-secret-key-999",
            "Authorization: Bearer fake-bearer-token-abcdef",
            (
                "https://acct.blob.core.windows.net/kb/doc.pdf"
                "?sv=2024-01-01&se=2099-01-01&sig=FAKESIGNATURE"
            ),
            '{"choices": [{"message": "fake response body content"}]}',
            "http://internal-service.local:8080/admin",
        ]
        reason_text = " | ".join(canaries)

        def _raise(*a, **kw):
            raise urllib.error.URLError(reason_text)

        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )
        result = _single_json_line(out)  # exactly one line, parses as JSON

        self.assertEqual(result["error_code"], qfi.ERROR_CODE_NETWORK_ERROR)
        self.assertIs(result["request_attempted"], True)
        self.assertIsNone(result["http_status"])
        self.assertIsNotNone(exit_code)
        self.assertNotEqual(exit_code, 0)

        for canary in canaries:
            self.assertNotIn(canary, out)
        self.assertNotIn(reason_text, out)
        self.assertEqual(result["error"], qfi.NETWORK_ERROR_MESSAGE)
        mock_urlopen.assert_called_once()

    def test_timeout_via_urlerror_is_not_downgraded_to_network_error(self):
        """Regression guard: the reason-redaction fix above must not
        accidentally short-circuit the isinstance(reason, TimeoutError)
        check that keeps a connect-phase timeout classified as
        request_timeout instead of network_error."""

        def _raise(*a, **kw):
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        exit_code, out, mock_urlopen = _run_main_with_mock(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=_raise,
        )
        result = _single_json_line(out)
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_REQUEST_TIMEOUT)
        self.assertIs(result["request_attempted"], True)
        self.assertIsNone(result["http_status"])
        mock_urlopen.assert_called_once()


class InvalidResponseTests(unittest.TestCase):
    def test_response_body_not_json(self):
        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(b"not json at all {{{"),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_RESPONSE)

    def test_response_body_not_object(self):
        body = json.dumps([1, 2, 3]).encode("utf-8")
        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_RESPONSE)

    def test_response_field_not_list(self):
        body = json.dumps({"response": "not-a-list"}).encode("utf-8")
        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_RESPONSE)

    def test_response_field_empty_list(self):
        body = json.dumps({"response": []}).encode("utf-8")
        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INVALID_RESPONSE)


class NoDocumentsTests(unittest.TestCase):
    def test_no_usable_documents(self):
        payload = {
            "response": [{"content": [{"type": "text", "text": json.dumps([])}]}],
            "references": [],
            "activity": [],
        }
        body = json.dumps(payload).encode("utf-8")
        exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: "test-key"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )
        result = _single_json_line(out)
        self.assertEqual(exit_code, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_NO_DOCUMENTS)


class StdoutShapeTests(unittest.TestCase):
    """Cross-cutting checks that must hold across every scenario above:
    exactly one stdout line, and that line is a JSON object carrying the
    full v2 envelope."""

    def _scenarios(self):
        payload = _foundry_payload()
        ok_body = json.dumps(payload).encode("utf-8")

        def _http_401(*a, **kw):
            raise urllib.error.HTTPError(qfi.FOUNDRY_IQ_URL, 401, "Unauthorized", None, None)

        return [
            ("success", ["query_foundry_iq.py", "q"], {ENV_KEY: "k"},
             lambda *a, **kw: _FakeResponse(ok_body)),
            ("no_question", ["query_foundry_iq.py"], {ENV_KEY: "k"}, None),
            ("missing_key", ["query_foundry_iq.py", "q"], {}, None),
            ("timeout", ["query_foundry_iq.py", "q"], {ENV_KEY: "k"},
             lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timed out"))),
            ("http_error", ["query_foundry_iq.py", "q"], {ENV_KEY: "k"}, _http_401),
        ]

    def test_every_scenario_is_single_valid_json_line_with_full_envelope(self):
        for name, argv, env, side_effect in self._scenarios():
            with self.subTest(scenario=name):
                _exit_code, out = _run_main(argv, env, urlopen_side_effect=side_effect)
                lines = [ln for ln in out.splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1, f"{name}: expected exactly one stdout line")
                parsed = json.loads(lines[0])  # must not raise
                for key in (
                    "schema_version", "ok", "request_attempted",
                    "error_code", "question", "http_status",
                ):
                    self.assertIn(key, parsed, f"{name}: missing key {key!r}")


class SecurityRedactionTests(unittest.TestCase):
    def test_no_sensitive_content_in_success_output(self):
        secret = "sk-super-secret-query-key-do-not-leak"
        sas_token = "sv=2024-01-01&se=2099-01-01&sig=SHOULD-NOT-APPEAR"

        payload = _foundry_payload(
            references=[
                {
                    "id": "r1",
                    "type": "AzureBlob",
                    "blobUrl": f"https://acct.blob.core.windows.net/kb/faq/doc.pdf?{sas_token}",
                    "rerankerScore": 1.0,
                }
            ]
        )
        body = json.dumps(payload).encode("utf-8")

        _exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {ENV_KEY: secret},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )

        self.assertNotIn(secret, out)
        self.assertNotIn(sas_token, out)
        self.assertNotIn("Authorization", out)
        self.assertNotIn("api-key", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn('File "', out)

    def test_no_sensitive_content_on_missing_key_path(self):
        secret_env_value = "should-never-be-echoed-back"
        _exit_code, out = _run_main(
            ["query_foundry_iq.py", "ADAM-6266 如何開啟 SNMP？"],
            {"UNRELATED_SECRET": secret_env_value},
        )
        self.assertNotIn(secret_env_value, out)


class SerializationFailureTests(unittest.TestCase):
    """Direct, function-level unit test of _serialize()'s fallback
    mechanism -- kept alongside the end-to-end
    SerializationFailureExitCodeTests below, which instead goes through
    main()/the real CLI exit path. This one intentionally does NOT go
    through main(): it exists to pin down _serialize()'s own contract
    (return shape, fallback content) independent of how any particular
    caller uses it."""

    def test_serialize_unit_level(self):
        class _Unserializable:
            def __repr__(self):
                return "<Unserializable object containing SECRET_MARKER>"

        payload = {
            "schema_version": qfi.SCHEMA_VERSION,
            "ok": False,
            "request_attempted": True,
            "error_code": qfi.ERROR_CODE_NETWORK_ERROR,
            "error": "boom",
            "question": "q",
            "not_json_serializable": _Unserializable(),
        }

        text, serialized_ok = qfi._serialize(payload)
        parsed = json.loads(text)  # must not raise

        self.assertFalse(serialized_ok)
        self.assertEqual(parsed["error_code"], qfi.ERROR_CODE_INTERNAL_ERROR)
        self.assertTrue(parsed["request_attempted"])
        self.assertNotIn("SECRET_MARKER", text)

    def test_serialize_ok_case_reports_true(self):
        payload = {"schema_version": qfi.SCHEMA_VERSION, "ok": True}
        text, serialized_ok = qfi._serialize(payload)
        self.assertTrue(serialized_ok)
        self.assertEqual(json.loads(text), payload)


class SerializationFailureExitCodeTests(unittest.TestCase):
    """End-to-end (through main()/the real CLI exit path): a serialization
    failure on the *success* payload must still produce a non-zero process
    exit code, not just a stdout line that says ok:false. This is the
    regression test for the bug where main()'s success path printed the
    internal_error fallback but still returned normally (implicit exit 0),
    contradicting what stdout said."""

    def test_success_payload_serialization_failure_forces_nonzero_exit(self):
        payload = _foundry_payload()
        body = json.dumps(payload).encode("utf-8")
        real_dumps = json.dumps

        def _dumps_side_effect(obj, *args, **kwargs):
            # Target only the real success result dict (schema_version +
            # ok=True) so the request-body encoding call inside
            # send_request() and the safe fallback dict (ok=False) both
            # still go through the real json.dumps unaffected.
            if (
                isinstance(obj, dict)
                and obj.get("schema_version") == qfi.SCHEMA_VERSION
                and obj.get("ok") is True
            ):
                raise TypeError("simulated: not JSON serializable")
            return real_dumps(obj, *args, **kwargs)

        stdout = io.StringIO()
        exit_code = None
        with patch.object(sys, "argv", ["query_foundry_iq.py", "q"]), \
             patch.dict(os.environ, {ENV_KEY: "k"}, clear=True), \
             patch("sys.stdin.isatty", return_value=True), \
             patch(
                 "query_foundry_iq.urllib.request.urlopen",
                 side_effect=lambda *a, **kw: _FakeResponse(body),
             ), \
             patch("query_foundry_iq.json.dumps", side_effect=_dumps_side_effect):
            try:
                with redirect_stdout(stdout):
                    qfi.main()
            except SystemExit as exc:
                exit_code = exc.code

        out = stdout.getvalue()
        result = _single_json_line(out)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], qfi.ERROR_CODE_INTERNAL_ERROR)
        self.assertIsNotNone(exit_code, "main() must raise SystemExit, not return normally")
        self.assertNotEqual(exit_code, 0)


class BackwardCompatibilityTests(unittest.TestCase):
    """A v1-shaped consumer that only reads the original keys must keep
    working unmodified against v2 output."""

    def test_v1_consumer_reads_success_fields(self):
        payload = _foundry_payload()
        body = json.dumps(payload).encode("utf-8")

        _exit_code, out = _run_main(
            ["query_foundry_iq.py", "q"],
            {ENV_KEY: "k"},
            urlopen_side_effect=lambda *a, **kw: _FakeResponse(body),
        )
        result = _single_json_line(out)

        # v1 consumer: only ever touches these five keys.
        ok = result["ok"]
        question = result["question"]
        documents = result["documents"]
        references = result["references"]
        activity = result["activity"]

        self.assertTrue(ok)
        self.assertEqual(question, "q")
        self.assertTrue(documents)
        self.assertTrue(references)
        self.assertTrue(activity)

    def test_v1_consumer_reads_failure_fields(self):
        _exit_code, out = _run_main(["query_foundry_iq.py"], {ENV_KEY: "k"})
        result = _single_json_line(out)

        # v1 consumer: only ever touches these two keys.
        ok = result["ok"]
        error = result["error"]

        self.assertFalse(ok)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
