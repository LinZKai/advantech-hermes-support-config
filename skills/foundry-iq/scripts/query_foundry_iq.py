#!/usr/bin/env python3

"""
A restricted Foundry IQ retrieval client.

This program:
1. Accepts one technical question.
2. Sends it to one fixed Foundry IQ Knowledge Base endpoint.
3. Reads the Query Key from an environment variable.
4. Parses every response message, content item, and retrieved document.
5. Returns predictable JSON for Hermes to consume.
6. Optionally appends each retrieval to a log, so that a reviewer can check
   an answer against what the knowledge base actually returned.

It does not allow the caller to choose an arbitrary URL, HTTP method,
API version, or request header.

Result contract (schema_version "foundry-iq-result-v2")
---------------------------------------------------------
Every invocation prints exactly one JSON object to stdout and nothing else.

All results (success and failure) carry:
  - schema_version: str            -- always "foundry-iq-result-v2"
  - ok: bool
  - request_attempted: bool        -- whether an Azure HTTP request was
                                       actually sent (see below)
  - error_code: str | None         -- machine-readable failure category,
                                       always None when ok is true
  - question: str | None           -- the user's question, once it has been
                                       safely read; None if not yet available
  - http_status: int | None        -- the HTTP status code, only when
                                       error_code is "http_error"; None
                                       otherwise (always present, so a
                                       consumer never needs to guard the key
                                       lookup itself)

Success additionally carries: documents, references, activity.
Failure additionally carries: error (a human-readable, pre-sanitized string).

`request_attempted` is set from actual control flow, never inferred from the
process exit code:
  - False: failure happened before an Azure HTTP request was sent (invalid
    input, missing/blank Query Key).
  - True: an Azure HTTP request was sent, or the program reached the point
    where one would have been sent right before send_request() -- covers a
    successful call, a timeout, a network/HTTP failure, an unparsable
    response, or a well-formed response with no usable documents.

This contract is purely additive over the v1 contract (ok/question/
documents/references/activity on success; ok/error on failure). Existing
consumers that only read those v1 keys continue to work unmodified.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


# Fixed destination. Hermes cannot use this script to contact another URL.
FOUNDRY_IQ_URL = (
    "https://nickliu-2919-srch-8m9l.search.windows.net/"
    "knowledgebases/kb-index-test/retrieve"
    "?api-version=2026-05-01-preview"
)

# Name of the environment variable that holds the Azure AI Search Query Key.
QUERY_KEY_ENV_NAME = "FOUNDRY_IQ_QUERY_KEY"

# Optional retrieval log, used to verify what the knowledge base actually
# returned.
#
# Logging is off unless one of the following is true:
#
# 1. FOUNDRY_IQ_DEBUG_DIR names a directory, or
# 2. a directory named `logs` exists beside this script's skill folder.
#
# The second form exists because the log destination is a plain path, not a
# secret, and does not belong in a credential store. Creating the directory
# turns logging on; removing or renaming it turns logging off.
DEBUG_DIR_ENV_NAME = "FOUNDRY_IQ_DEBUG_DIR"

DEBUG_DIR_NAME = "logs"

DEBUG_FILE_NAME = "foundry_iq_retrievals.jsonl"

# Prevent requests from waiting forever.
REQUEST_TIMEOUT_SECONDS = 60

# Prevent unexpectedly huge prompts from being sent to the knowledge base.
MAX_QUESTION_LENGTH = 4000

# Contract version for the stdout JSON result. Bump this whenever the result
# shape changes, so a consumer can branch on it instead of guessing from
# which keys happen to be present.
SCHEMA_VERSION = "foundry-iq-result-v2"

# Stable, machine-readable failure categories. A consumer should branch on
# this, not on the human-readable `error` string (which is for a person, and
# is not guaranteed to stay wordable-for-wordable across changes).
#
#   invalid_input      -- the question itself was missing, blank, or too
#                          long. Never reaches the network.
#   missing_query_key  -- FOUNDRY_IQ_QUERY_KEY is unset or blank. Never
#                          reaches the network.
#   request_timeout    -- the Azure HTTP request was sent but timed out,
#                          whether the timeout happened while connecting/
#                          sending or while reading the response body.
#   http_error         -- the Azure HTTP request was sent and a response was
#                          received, but with a non-2xx HTTP status. The
#                          status code itself is available in `http_status`.
#   network_error      -- the Azure HTTP request could not be completed at
#                          the transport layer (DNS/TLS/connection refused,
#                          or any other non-timeout OSError). No HTTP status
#                          was ever received, so `http_status` is None.
#                          Kept distinct from http_error because the two
#                          point at different remediation paths (network/
#                          infra vs. a response Azure actually sent).
#   invalid_response   -- a response was received but its shape did not
#                          match what this script expects: not valid JSON,
#                          not a JSON object, missing/malformed `response`
#                          array, or an empty `response` array. This is a
#                          contract violation (bug or upstream API change),
#                          distinct from a well-formed response that simply
#                          has nothing useful in it (see no_documents).
#   no_documents       -- the response was well-formed and fully parsed, but
#                          contained no usable retrieved text. This is a
#                          legitimate, well-formed empty result, not a
#                          malfunction.
#   internal_error     -- this script's own result payload could not be
#                          serialized to JSON. Should not occur in practice;
#                          exists only as a last-resort fallback so a bug in
#                          payload construction can never leak a Python
#                          repr, a secret, or a traceback to stdout.
ERROR_CODE_INVALID_INPUT = "invalid_input"
ERROR_CODE_MISSING_QUERY_KEY = "missing_query_key"
ERROR_CODE_REQUEST_TIMEOUT = "request_timeout"
ERROR_CODE_HTTP_ERROR = "http_error"
ERROR_CODE_NETWORK_ERROR = "network_error"
ERROR_CODE_INVALID_RESPONSE = "invalid_response"
ERROR_CODE_NO_DOCUMENTS = "no_documents"
ERROR_CODE_INTERNAL_ERROR = "internal_error"

# Fixed, safe messages used wherever the underlying exception's own text
# cannot be trusted not to carry request/response content (a URLError's
# `reason`, an arbitrary unexpected exception's str()/repr(), etc.).
# Deliberately never interpolate anything exception-specific into these.
NETWORK_ERROR_MESSAGE = "無法連線至 Foundry IQ。"
UNEXPECTED_ERROR_MESSAGE = "Foundry IQ 查詢過程發生非預期錯誤。"


def _serialize(payload: dict[str, Any]) -> tuple[str, bool]:
    """
    Serialize one result payload to a single-line JSON string.

    Returns (json_text, serialized_ok). serialized_ok is False only when the
    *original* payload could not be serialized and the safe internal_error
    fallback had to be used instead. A caller that needs to react to this
    (see main()'s success path) must use this return value, not parse the
    JSON text back out to guess -- the text alone carries no exit-code
    information, and re-parsing it would be inferring the very thing this
    function already knows for certain.

    Never lets a serialization failure leak a Python repr, a secret, or a
    traceback to stdout: falls back to a minimal, safe internal_error
    payload built from scratch (not from the failed payload's contents).
    """
    try:
        return json.dumps(payload, ensure_ascii=False), True
    except (TypeError, ValueError):
        fallback = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "request_attempted": bool(payload.get("request_attempted")),
            "error_code": ERROR_CODE_INTERNAL_ERROR,
            "error": "Foundry IQ 內部錯誤：結果序列化失敗。",
            "question": None,
            "http_status": None,
        }
        return json.dumps(fallback, ensure_ascii=False), False


def fail(
    message: str,
    *,
    error_code: str,
    request_attempted: bool,
    question: str | None = None,
    http_status: int | None = None,
    exit_code: int = 1,
) -> None:
    """
    Print a predictable JSON error and stop the program.

    This deliberately avoids printing:
    - Request headers
    - Query Key
    - Full urllib request objects
    - Full exception traces

    `error_code` and `request_attempted` must be supplied by the caller from
    actual control flow -- this function never infers either from
    `exit_code`, so the exit code stays free to carry its own (looser, CLI
    convention) meaning without affecting the JSON contract.

    `exit_code` here is always non-zero at every call site regardless of
    whether the payload above serializes cleanly or falls back to
    internal_error -- both outcomes are already a failure, so unlike
    main()'s success path, no extra branching on serialized_ok is needed
    here to keep stdout and the exit code consistent with each other.
    """
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "request_attempted": request_attempted,
        "error_code": error_code,
        "error": message,
        "question": question,
        "http_status": http_status,
    }
    text, _serialized_ok = _serialize(result)
    print(text)
    raise SystemExit(exit_code)


def read_question() -> str:
    """
    Read the user's question.

    Supported methods:

    1. Command-line argument:
       python3 query_foundry_iq.py "ADAM-6266 如何關閉 SNMP？"

    2. Standard input:
       printf '%s' 'ADAM-6266 如何關閉 SNMP？' | python3 query_foundry_iq.py
    """
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        question = sys.stdin.read()
    else:
        fail(
            "未提供查詢問題。請透過命令列參數或標準輸入提供問題。",
            error_code=ERROR_CODE_INVALID_INPUT,
            request_attempted=False,
            exit_code=2,
        )

    question = question.strip()

    if not question:
        fail(
            "查詢問題不可為空白。",
            error_code=ERROR_CODE_INVALID_INPUT,
            request_attempted=False,
            exit_code=2,
        )

    if len(question) > MAX_QUESTION_LENGTH:
        fail(
            f"查詢問題不可超過 {MAX_QUESTION_LENGTH} 個字元。",
            error_code=ERROR_CODE_INVALID_INPUT,
            request_attempted=False,
            # The question was already safely read in full at this point --
            # it is the caller's own input, not additional information --
            # so it is included rather than withheld.
            question=question,
            exit_code=2,
        )

    return question


def build_request_body(question: str) -> dict[str, Any]:
    """
    Build the request body using the exact structure that succeeded
    in Postman.
    """
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": question,
                    }
                ],
            }
        ],
        "includeActivity": True,
    }


def parse_text_documents(text_value: str) -> list[dict[str, Any]]:
    """
    Parse one text content block.

    In the observed Foundry IQ response, content[].text is not the final
    document text directly. It is a JSON-encoded string such as:

    [
      {
        "ref_id": 0,
        "content": "retrieved document text"
      }
    ]

    This function handles:
    - A JSON array of documents
    - A single JSON document object
    - Plain text as a defensive fallback
    """
    text_value = text_value.strip()

    if not text_value:
        return []

    try:
        parsed_value = json.loads(text_value)
    except json.JSONDecodeError:
        return [
            {
                "ref_id": None,
                "content": text_value,
            }
        ]

    if isinstance(parsed_value, dict):
        candidate_documents = [parsed_value]
    elif isinstance(parsed_value, list):
        candidate_documents = parsed_value
    else:
        return []

    documents: list[dict[str, Any]] = []

    for candidate in candidate_documents:
        if not isinstance(candidate, dict):
            continue

        content = candidate.get("content")

        if not isinstance(content, str):
            continue

        content = content.strip()

        if not content:
            continue

        documents.append(
            {
                "ref_id": candidate.get("ref_id"),
                "content": content,
            }
        )

    return documents


def extract_retrieved_documents(
    payload: dict[str, Any],
    question: str,
) -> list[dict[str, Any]]:
    """
    Traverse all possible response and content array elements.

    The structure is treated as:

    response[]
      └── content[]
            └── text
                  └── JSON-decoded documents[]

    We do not assume:
    - response has exactly one element
    - content has exactly one element
    - the decoded document array has exactly one element

    `question` is only used to populate the `question` field of a fail()
    call here -- an Azure HTTP request has already been sent by this point,
    so every fail() below reports request_attempted=True.
    """
    response_messages = payload.get("response")

    if not isinstance(response_messages, list):
        fail(
            "Foundry IQ 回傳格式不符合預期：response 不是陣列。",
            error_code=ERROR_CODE_INVALID_RESPONSE,
            request_attempted=True,
            question=question,
        )

    if not response_messages:
        fail(
            "Foundry IQ 沒有回傳任何 response message。",
            error_code=ERROR_CODE_INVALID_RESPONSE,
            request_attempted=True,
            question=question,
        )

    all_documents: list[dict[str, Any]] = []

    for message in response_messages:
        if not isinstance(message, dict):
            continue

        content_items = message.get("content")

        if not isinstance(content_items, list):
            continue

        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue

            # At present we only consume textual grounding content.
            if content_item.get("type") != "text":
                continue

            text_value = content_item.get("text")

            if not isinstance(text_value, str):
                continue

            all_documents.extend(parse_text_documents(text_value))

    if not all_documents:
        fail(
            "Foundry IQ 沒有回傳可使用的文字檢索內容。",
            error_code=ERROR_CODE_NO_DOCUMENTS,
            request_attempted=True,
            question=question,
        )

    return all_documents


def extract_source_name(blob_url: Any) -> str | None:
    """
    Reduce an internal Blob URL to a bare document file name.

    Hermes is required to cite the source document but must never expose
    internal storage locations, so the container path, host, and any query
    string are removed here rather than relying on prompt instructions.

    "https://acct.blob.core.windows.net/kb/faq/ADAM-6233%20SNMP.pdf?sv=..."
    becomes "ADAM-6233 SNMP.pdf".
    """
    if not isinstance(blob_url, str):
        return None

    path = urllib.parse.urlsplit(blob_url).path
    file_name = urllib.parse.unquote(path.rsplit("/", 1)[-1]).strip()

    return file_name or None


def extract_references(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Return non-secret reference metadata.

    This can help Hermes understand which retrieved document corresponds
    to a ref_id. The Query Key, request headers, and internal Blob URLs
    are never included.
    """
    raw_references = payload.get("references", [])

    if not isinstance(raw_references, list):
        return []

    references: list[dict[str, Any]] = []

    for raw_reference in raw_references:
        if not isinstance(raw_reference, dict):
            continue

        references.append(
            {
                "id": raw_reference.get("id"),
                "type": raw_reference.get("type"),
                "source_name": extract_source_name(
                    raw_reference.get("blobUrl")
                ),
                "reranker_score": raw_reference.get("rerankerScore"),
            }
        )

    return references


def extract_activity_summary(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Return a small, non-sensitive activity summary.

    We intentionally do not return the entire activity payload because
    Hermes normally only needs basic diagnostic information.
    """
    raw_activity = payload.get("activity", [])

    if not isinstance(raw_activity, list):
        return []

    activity_summary: list[dict[str, Any]] = []

    for activity in raw_activity:
        if not isinstance(activity, dict):
            continue

        activity_summary.append(
            {
                "type": activity.get("type"),
                "elapsed_ms": activity.get("elapsedMs"),
                "model_name": activity.get("modelName"),
                "input_tokens": activity.get("inputTokens"),
                "output_tokens": activity.get("outputTokens"),
                "reasoning_tokens": activity.get("reasoningTokens"),
            }
        )

    return activity_summary


def sanitize_for_debug(node: Any) -> Any:
    """
    Copy a response payload with credential-bearing URL fragments removed.

    Blob URLs may carry a SAS token in the query string. The retrieval log is
    written to disk and may be shared while reviewing behaviour, so the token
    is stripped before anything is recorded. The path is kept, because it
    identifies which document was returned.
    """
    if isinstance(node, dict):
        sanitized: dict[str, Any] = {}

        for key, value in node.items():
            if key == "blobUrl" and isinstance(value, str):
                sanitized[key] = urllib.parse.urlsplit(value)._replace(
                    query="",
                    fragment="",
                ).geturl()
            else:
                sanitized[key] = sanitize_for_debug(value)

        return sanitized

    if isinstance(node, list):
        return [sanitize_for_debug(item) for item in node]

    return node


def resolve_debug_dir() -> str | None:
    """
    Decide where, if anywhere, the retrieval log should be written.

    The environment variable wins when it is set. Otherwise a `logs` directory
    beside the skill folder enables logging simply by existing, which keeps a
    non-secret path out of the credential provider.
    """
    configured_dir = os.environ.get(DEBUG_DIR_ENV_NAME, "").strip()

    if configured_dir:
        return configured_dir

    skill_dir = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    default_dir = os.path.join(skill_dir, DEBUG_DIR_NAME)

    if os.path.isdir(default_dir):
        return default_dir

    return None


def write_debug_record(
    question: str,
    result: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """
    Append one retrieval record when logging is enabled.

    This exists so that a reviewer can check whether a statement in an answer
    was actually present in the retrieved documents, rather than supplied from
    model memory.

    Logging must never break an answer. Any filesystem problem is ignored, and
    the Query Key is never part of a response payload, so it cannot be written.
    """
    debug_dir = resolve_debug_dir()

    if not debug_dir:
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "documents": result.get("documents"),
        "references": result.get("references"),
        "activity": result.get("activity"),
        "raw_response": sanitize_for_debug(payload),
    }

    try:
        os.makedirs(debug_dir, exist_ok=True)

        debug_path = os.path.join(debug_dir, DEBUG_FILE_NAME)

        with open(debug_path, "a", encoding="utf-8") as debug_file:
            debug_file.write(
                json.dumps(record, ensure_ascii=False) + "\n"
            )

    except OSError:
        return


def send_request(
    question: str,
    query_key: str,
) -> dict[str, Any]:
    """
    Send one POST request to the fixed Foundry IQ endpoint.

    request_dispatched flips to True immediately before urlopen() is
    called, and stays False for anything that fails while still building
    the request locally (before any network I/O). Every fail() call below
    reports request_attempted from this flag rather than a hardcoded
    literal, so a failure is correctly attributed to whichever side of the
    urlopen() call it actually happened on.

    The outer `except Exception` is a last-resort safety net around the
    whole function -- e.g. for a failure while building the request itself,
    or any other unexpected error not already handled above. It uses
    `except Exception`, not `except BaseException`, specifically so it
    never catches KeyboardInterrupt or the SystemExit every fail() call
    below raises: those propagate straight through it untouched.
    """
    request_dispatched = False
    try:
        request_body = build_request_body(question)

        encoded_body = json.dumps(
            request_body,
            ensure_ascii=False,
        ).encode("utf-8")

        request = urllib.request.Request(
            url=FOUNDRY_IQ_URL,
            data=encoded_body,
            headers={
                "Content-Type": "application/json",
                "api-key": query_key,
            },
            method="POST",
        )

        try:
            request_dispatched = True
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                response_body = response.read().decode("utf-8")

        except urllib.error.HTTPError as error:
            # HTTPError is itself a URLError subclass, so it must be caught
            # before the broader `except urllib.error.URLError` below --
            # reversing this order would make every HTTPError match the
            # URLError clause instead and lose the HTTP status code. Only
            # the integer status code is kept -- never the response body,
            # the URL, or any header.
            fail(
                f"Foundry IQ 回傳 HTTP {error.code}。",
                error_code=ERROR_CODE_HTTP_ERROR,
                request_attempted=request_dispatched,
                question=question,
                http_status=error.code,
            )

        except urllib.error.URLError as error:
            # A timeout that happens while connecting or sending the
            # request (i.e. inside this urlopen() call) is not raised as a
            # bare TimeoutError -- urllib's internal do_open() catches
            # OSError (TimeoutError is an OSError subclass) during connect/
            # send and re-raises it wrapped as URLError(reason=<the
            # original OSError>). Only a timeout while *reading the
            # response body* (after urlopen() has already returned, in
            # response.read() below) reaches the bare `except TimeoutError`
            # clause further down. Checking error.reason here is what makes
            # both phases report the same request_timeout error_code --
            # reordering the except clauses cannot fix this on its own,
            # because URLError and TimeoutError are sibling OSError
            # subclasses, neither nested inside the other.
            if isinstance(error.reason, TimeoutError):
                fail(
                    f"Foundry IQ 查詢超過 {REQUEST_TIMEOUT_SECONDS} 秒，已逾時。",
                    error_code=ERROR_CODE_REQUEST_TIMEOUT,
                    request_attempted=request_dispatched,
                    question=question,
                )
            # error.reason is not included in the message: it can carry
            # DNS/proxy/TLS/policy details verbatim from the underlying
            # OSError, and this script does not control -- and so cannot
            # guarantee the safety of -- that text. A fixed, generic
            # message is used instead; anyone who needs the real reason
            # for debugging has to reproduce it directly, not read it off
            # this script's stdout.
            fail(
                NETWORK_ERROR_MESSAGE,
                error_code=ERROR_CODE_NETWORK_ERROR,
                request_attempted=request_dispatched,
                question=question,
            )

        except TimeoutError:
            # Reached for a timeout during response body reading
            # (response.read(), after urlopen() itself already returned) --
            # see the URLError branch above for the connect/send-phase
            # case.
            fail(
                f"Foundry IQ 查詢超過 {REQUEST_TIMEOUT_SECONDS} 秒，已逾時。",
                error_code=ERROR_CODE_REQUEST_TIMEOUT,
                request_attempted=True,
                question=question,
            )

        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError:
            fail(
                "Foundry IQ 回傳內容不是有效的 JSON。",
                error_code=ERROR_CODE_INVALID_RESPONSE,
                request_attempted=True,
                question=question,
            )

        if not isinstance(payload, dict):
            fail(
                "Foundry IQ 回傳的最外層內容不是 JSON object。",
                error_code=ERROR_CODE_INVALID_RESPONSE,
                request_attempted=True,
                question=question,
            )

        return payload

    except Exception:
        # Anything not already handled above -- most notably a failure
        # while building request_body/encoded_body/the Request object
        # itself, before request_dispatched ever flips True, but also any
        # other unexpected error at any point in this function. The caught
        # exception's own str()/repr() is never included: an arbitrary
        # exception here could in principle stringify to something built
        # from the request (its own message construction is not this
        # script's own code above), so only the fixed, safe message is
        # used.
        fail(
            UNEXPECTED_ERROR_MESSAGE,
            error_code=ERROR_CODE_INTERNAL_ERROR,
            request_attempted=request_dispatched,
        )


def main() -> None:
    """
    Main execution flow.
    """
    question = read_question()

    query_key = os.environ.get(QUERY_KEY_ENV_NAME)

    if not query_key:
        fail(
            f"缺少 {QUERY_KEY_ENV_NAME} 環境變數。",
            error_code=ERROR_CODE_MISSING_QUERY_KEY,
            request_attempted=False,
            question=question,
        )

    query_key = query_key.strip()

    if not query_key:
        fail(
            f"{QUERY_KEY_ENV_NAME} 不可為空白。",
            error_code=ERROR_CODE_MISSING_QUERY_KEY,
            request_attempted=False,
            question=question,
        )

    payload = send_request(
        question=question,
        query_key=query_key,
    )

    documents = extract_retrieved_documents(payload, question)
    references = extract_references(payload)
    activity = extract_activity_summary(payload)

    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "request_attempted": True,
        "error_code": None,
        "question": question,
        "http_status": None,
        "documents": documents,
        "references": references,
        "activity": activity,
    }

    write_debug_record(
        question=question,
        result=result,
        payload=payload,
    )

    text, serialized_ok = _serialize(result)
    print(text)
    if not serialized_ok:
        # The payload above could not be serialized as-is, so stdout now
        # shows the safe internal_error fallback instead of the success
        # result this function otherwise returns from with an implicit exit
        # code of 0. That implicit 0 would contradict what stdout just
        # said, so it must be forced non-zero explicitly here -- this is
        # decided from serialized_ok (known for certain from _serialize()'s
        # own control flow), never by parsing `text` back to check which
        # payload it turned out to be.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
