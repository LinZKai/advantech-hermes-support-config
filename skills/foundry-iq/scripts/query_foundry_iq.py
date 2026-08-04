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


def fail(message: str, exit_code: int = 1) -> None:
    """
    Print a predictable JSON error and stop the program.

    This deliberately avoids printing:
    - Request headers
    - Query Key
    - Full urllib request objects
    - Full exception traces
    """
    result = {
        "ok": False,
        "error": message,
    }

    print(json.dumps(result, ensure_ascii=False))
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
            exit_code=2,
        )

    question = question.strip()

    if not question:
        fail("查詢問題不可為空白。", exit_code=2)

    if len(question) > MAX_QUESTION_LENGTH:
        fail(
            f"查詢問題不可超過 {MAX_QUESTION_LENGTH} 個字元。",
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
    """
    response_messages = payload.get("response")

    if not isinstance(response_messages, list):
        fail("Foundry IQ 回傳格式不符合預期：response 不是陣列。")

    if not response_messages:
        fail("Foundry IQ 沒有回傳任何 response message。")

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
        fail("Foundry IQ 沒有回傳可使用的文字檢索內容。")

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
    """
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
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            response_body = response.read().decode("utf-8")

    except urllib.error.HTTPError as error:
        # Never display request headers or the Query Key.
        fail(f"Foundry IQ 回傳 HTTP {error.code}。")

    except urllib.error.URLError as error:
        # error.reason normally contains DNS, proxy, TLS, or policy details,
        # but not the request API key.
        fail(f"無法連線至 Foundry IQ：{error.reason}。")

    except TimeoutError:
        fail(
            f"Foundry IQ 查詢超過 {REQUEST_TIMEOUT_SECONDS} 秒，已逾時。"
        )

    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        fail("Foundry IQ 回傳內容不是有效的 JSON。")

    if not isinstance(payload, dict):
        fail("Foundry IQ 回傳的最外層內容不是 JSON object。")

    return payload


def main() -> None:
    """
    Main execution flow.
    """
    question = read_question()

    query_key = os.environ.get(QUERY_KEY_ENV_NAME)

    if not query_key:
        fail(
            f"缺少 {QUERY_KEY_ENV_NAME} 環境變數。"
        )

    query_key = query_key.strip()

    if not query_key:
        fail(
            f"{QUERY_KEY_ENV_NAME} 不可為空白。"
        )

    payload = send_request(
        question=question,
        query_key=query_key,
    )

    documents = extract_retrieved_documents(payload)
    references = extract_references(payload)
    activity = extract_activity_summary(payload)

    result = {
        "ok": True,
        "question": question,
        "documents": documents,
        "references": references,
        "activity": activity,
    }

    write_debug_record(
        question=question,
        result=result,
        payload=payload,
    )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()