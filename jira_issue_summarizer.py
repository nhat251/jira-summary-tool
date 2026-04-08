from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

UCTALENT_HOST = "uctalent.atlassian.net"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
JIRA_FIELDS = "summary,description,attachment"
JIRA_TIMEOUT_SECONDS = 30
GEMINI_TIMEOUT_SECONDS = 60
MAX_IMAGES_PER_BATCH = 5
ERROR_PREFIX = "ERROR:"
RESULTS_ROOT_DIR = Path(__file__).resolve().with_name("result")
BATCH_CONTEXT_FILE_NAME = "_batch_context.md"
JIRA_BROWSE_PATH_PATTERN = re.compile(r"^/browse/([A-Z][A-Z0-9]*-\d+)$")
ISSUE_KEY_HINT_PATTERN = re.compile(r"/browse/([A-Z][A-Z0-9]*-\d+)")


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()

    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()

    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]

    return key, value


def load_env_file(env_path: str | Path | None = None) -> None:
    if env_path is None:
        env_path = Path(__file__).resolve().with_name(".env")
    else:
        env_path = Path(env_path)

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue

        key, value = parsed
        os.environ.setdefault(key, value)


def get_result_date_label(current_datetime: datetime | None = None) -> str:
    if current_datetime is None:
        current_datetime = datetime.now()
    return current_datetime.strftime("%d-%m-%Y")


def get_results_directory(
    root_dir: str | Path | None = None,
    current_date: str | None = None,
) -> Path:
    base_dir = RESULTS_ROOT_DIR if root_dir is None else Path(root_dir)
    date_label = current_date or get_result_date_label()
    directory = base_dir / date_label
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_result_markdown(result: dict[str, str]) -> str:
    key = result.get("key") or "UNKNOWN"
    title = result.get("title", "").strip()
    summary = result.get("summary", "").strip()

    parts = [f"# {key}"]
    if title:
        parts.append(f"**Title:** {title}")
    parts.append("## Summary")
    parts.append(summary or "_No summary returned._")
    return "\n\n".join(parts).strip() + "\n"


def persist_issue_result(
    result: dict[str, str],
    root_dir: str | Path | None = None,
    current_date: str | None = None,
) -> Path:
    key = result.get("key") or "unknown-issue"
    directory = get_results_directory(root_dir=root_dir, current_date=current_date)
    file_path = directory / f"{key}.md"
    file_path.write_text(build_result_markdown(result), encoding="utf-8")
    return file_path


def build_batch_context_entry(result: dict[str, str]) -> str:
    key = result.get("key") or "UNKNOWN"
    title = result.get("title", "").strip()
    summary = result.get("summary", "").strip() or "Khong ro"

    heading = f"## {key}"
    if title:
        heading = f"{heading} - {title}"

    return f"{heading}\n\n{summary}\n"


def get_batch_context_path(
    root_dir: str | Path | None = None,
    current_date: str | None = None,
) -> Path:
    return get_results_directory(root_dir=root_dir, current_date=current_date) / BATCH_CONTEXT_FILE_NAME


def read_batch_context(batch_context_path: Path | None) -> str:
    if batch_context_path is None or not batch_context_path.exists():
        return ""
    return batch_context_path.read_text(encoding="utf-8").strip()


def append_to_batch_context(batch_context_path: Path, result: dict[str, str]) -> None:
    existing = read_batch_context(batch_context_path)
    entry = build_batch_context_entry(result).strip()
    content = f"{existing}\n\n{entry}".strip() if existing else entry
    batch_context_path.write_text(f"{content}\n", encoding="utf-8")


def parse_jira_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Invalid format or unsupported URL")

    host = (parsed.hostname or "").lower()
    match = JIRA_BROWSE_PATH_PATTERN.fullmatch(parsed.path)
    if host != UCTALENT_HOST or not match:
        raise ValueError("Invalid format or unsupported URL")

    return host, match.group(1)


def extract_issue_key_hint(url: str) -> str:
    match = ISSUE_KEY_HINT_PATTERN.search(url)
    return match.group(1) if match else ""


def _join_non_empty(parts: list[str], separator: str = "") -> str:
    return separator.join(part for part in parts if part)


def _format_list_item(text: str, prefix: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    formatted = [f"{prefix} {lines[0]}"]
    formatted.extend(f"  {line}" for line in lines[1:])
    return "\n".join(formatted)


def _extract_adf_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    content = node.get("content", [])

    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return node.get("attrs", {}).get("text", "")
    if node_type == "emoji":
        return node.get("attrs", {}).get("text", "")
    if node_type == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    if node_type in {"doc", "tableCell", "tableHeader"}:
        return _join_non_empty([_extract_adf_text(child) for child in content])
    if node_type in {"paragraph", "heading", "blockquote", "codeBlock"}:
        text = _join_non_empty([_extract_adf_text(child) for child in content]).strip()
        return f"{text}\n\n" if text else ""
    if node_type == "bulletList":
        items = [
            _format_list_item(_extract_adf_text(item).strip(), "-")
            for item in content
        ]
        items_text = "\n".join(item for item in items if item)
        return f"{items_text}\n\n" if items_text else ""
    if node_type == "orderedList":
        items: list[str] = []
        for index, item in enumerate(content, start=1):
            item_text = _format_list_item(_extract_adf_text(item).strip(), f"{index}.")
            if item_text:
                items.append(item_text)
        items_text = "\n".join(items)
        return f"{items_text}\n\n" if items_text else ""
    if node_type == "listItem":
        return _join_non_empty([_extract_adf_text(child) for child in content])
    if node_type == "table":
        rows = [_extract_adf_text(child).rstrip() for child in content]
        table_text = "\n".join(row for row in rows if row)
        return f"{table_text}\n\n" if table_text else ""
    if node_type == "tableRow":
        cells = [_extract_adf_text(child).strip() for child in content]
        row_text = " | ".join(cell for cell in cells if cell)
        return f"{row_text}\n" if row_text else ""

    return _join_non_empty([_extract_adf_text(child) for child in content])


def extract_adf_text(node: Any) -> str:
    text = _extract_adf_text(node).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_description(description_field: Any) -> str:
    if not description_field:
        return ""
    if isinstance(description_field, str):
        return description_field.strip()
    if isinstance(description_field, dict) and description_field.get("type") == "doc":
        return extract_adf_text(description_field)
    return str(description_field).strip()


def chunk_items(items: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]


def build_jira_session(email: str, token: str) -> requests.Session:
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    session.headers.update({"Accept": "application/json"})
    return session


def fetch_issue(session: requests.Session, key: str) -> dict[str, Any]:
    api_url = f"https://{UCTALENT_HOST}/rest/api/3/issue/{key}?fields={JIRA_FIELDS}"
    response = session.get(api_url, timeout=JIRA_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to access issue {key} (HTTP {response.status_code} - {response.reason})"
        )
    return response.json()


def collect_issue_images(
    session: requests.Session,
    issue_key: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []

    for attachment in attachments:
        mime_type = attachment.get("mimeType", "")
        content_url = attachment.get("content")
        if not mime_type.startswith("image/") or not content_url:
            continue

        response = session.get(content_url, timeout=JIRA_TIMEOUT_SECONDS)
        if response.status_code != 200:
            continue

        image_index = len(images) + 1
        filename = attachment.get("filename", f"image-{image_index}")
        images.append(
            {
                "label": f"Issue: {issue_key} | Image {image_index} | {filename}",
                "mime_type": mime_type,
                "data": response.content,
            }
        )

    return images


def build_related_context_block(related_context_markdown: str) -> str:
    if not related_context_markdown.strip():
        return ""

    return (
        "Related issue context from earlier URLs in this same run:\n"
        f"{related_context_markdown.strip()}\n\n"
        "Treat these issues as related work items, but mention a cross-issue link only when that link is directly supported by the provided issue content or prior summaries.\n\n"
    )


def build_initial_prompt(
    title: str,
    description: str,
    related_context_markdown: str = "",
) -> str:
    related_context_block = build_related_context_block(related_context_markdown)
    return (
        "Summarize this Jira task for a developer.\n"
        "Write the answer in Vietnamese.\n"
        "Use only facts that are explicitly present in the title, description, images, and related issue context provided below.\n"
        "Do not infer missing details, root cause, solution, priority, owner, timeline, or acceptance criteria.\n"
        "Do not add assumptions, general best practices, or extra analysis that is not directly supported.\n"
        "If something is not stated, write 'Khong ro' instead of guessing.\n"
        "Keep the output length proportional to the source. If the issue content is long or detailed, keep the summary detailed.\n"
        "Do not shorten away concrete examples, sample values, exact labels, exact messages, URLs, route patterns, delays, counts, IDs, or quoted strings when they help explain the behavior.\n"
        "If the user story contains a concrete URL, sample input, exact text, or example scenario, keep it explicitly in the summary.\n"
        "The goal of shortening is only to remove near-duplicate repetition. Do not remove useful examples or clarifying details.\n"
        "Output must follow this exact Markdown schema and use bullets only inside each section:\n"
        "# Tom tat\n"
        "- ...\n"
        "# Du lieu quan sat duoc\n"
        "- ...\n"
        "# Lien ket issue lien quan\n"
        "- Neu co moi lien he ro rang voi issue khac trong batch, neu issue key va moi lien he do.\n"
        "- Neu khong ro, ghi 'Khong ro'.\n"
        "# Phan tich gioi han\n"
        "- Chi neu nhung nhan dinh co bang chung ro rang tu input.\n"
        "- Neu khong du du lieu, ghi 'Khong ro'.\n"
        "# Viec can lam\n"
        "- ...\n"
        f"Title: {title}\n"
        f"Description: {description}\n\n"
        f"{related_context_block}"
        "Return only the Markdown summary. Use images only as supporting context and do not add unsupported conclusions."
    )


def build_followup_prompt(issue_key: str, title: str) -> str:
    return (
        f"Update the existing Markdown summary for Jira issue {issue_key}.\n"
        "Use only the existing summary and the new images.\n"
        "Do not invent new details or infer anything that is not clearly visible in the current inputs.\n"
        "Preserve factual statements that are already supported; only revise them when the new images clearly justify it.\n"
        "Keep the output length proportional to the current summary and new evidence. If the task is detailed, keep the result detailed.\n"
        "Preserve concrete examples, sample values, exact labels, exact messages, URLs, route patterns, delays, counts, IDs, and quoted strings from the existing summary whenever they are still supported.\n"
        "Only remove content when it is clearly repetitive. Do not remove useful examples or clarifying details.\n"
        "Keep the exact same Markdown schema and bullet-only style.\n"
        "Do not add new sections.\n"
        "If a section has no supported content, keep a bullet with 'Khong ro'.\n"
        f"Title: {title}\n"
        "Produce the full updated Markdown summary.\n"
        "Return only the complete updated Markdown, with no extra commentary."
    )


def extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "").strip() for part in parts if part.get("text")]
    response_text = "\n".join(text for text in texts if text).strip()
    if not response_text:
        raise RuntimeError("Gemini returned no text")

    return response_text


def upload_gemini_file(api_key: str, image: dict[str, Any]) -> dict[str, str]:
    start_headers = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(image["data"])),
        "X-Goog-Upload-Header-Content-Type": image["mime_type"],
        "Content-Type": "application/json",
    }
    start_response = requests.post(
        f"{GEMINI_API_BASE_URL}/upload/v1beta/files",
        headers=start_headers,
        json={"file": {"display_name": image["label"]}},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if start_response.status_code not in {200, 201}:
        raise RuntimeError(
            f"Gemini file upload start failed (HTTP {start_response.status_code})"
        )

    upload_url = start_response.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Gemini file upload did not return an upload URL")

    upload_headers = {
        "Content-Length": str(len(image["data"])),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    upload_response = requests.post(
        upload_url,
        headers=upload_headers,
        data=image["data"],
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if upload_response.status_code not in {200, 201}:
        raise RuntimeError(
            f"Gemini file upload failed (HTTP {upload_response.status_code})"
        )

    payload = upload_response.json().get("file", {})
    file_name = payload.get("name")
    file_uri = payload.get("uri")
    mime_type = payload.get("mimeType") or image["mime_type"]
    if not file_name or not file_uri:
        raise RuntimeError("Gemini file upload returned incomplete metadata")

    return {
        "name": file_name,
        "uri": file_uri,
        "mime_type": mime_type,
        "label": image["label"],
    }


def delete_gemini_file(api_key: str, file_name: str) -> None:
    response = requests.delete(
        f"{GEMINI_API_BASE_URL}/v1beta/{file_name}",
        headers={"x-goog-api-key": api_key},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if response.status_code not in {200, 204}:
        raise RuntimeError(
            f"Gemini file delete failed for {file_name} (HTTP {response.status_code})"
        )


def call_gemini(api_key: str, model: str, parts: list[dict[str, Any]]) -> str:
    url = f"{GEMINI_API_BASE_URL}/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {"contents": [{"parts": parts}]}
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=GEMINI_TIMEOUT_SECONDS,
    )

    if response.status_code != 200:
        message = ""
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = response.text.strip()

        detail = f": {message}" if message else ""
        raise RuntimeError(
            f"Gemini request failed (HTTP {response.status_code}){detail}"
        )

    return extract_gemini_text(response.json())


def create_temp_markdown_path(issue_key: str, temp_dir: str | None = None) -> Path:
    handle, path = tempfile.mkstemp(
        prefix=f"{issue_key.lower()}-",
        suffix=".md",
        dir=temp_dir,
        text=True,
    )
    os.close(handle)
    return Path(path)


def build_uploaded_file_parts(files: list[dict[str, str]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for file_info in files:
        parts.append({"text": file_info["label"]})
        parts.append(
            {
                "file_data": {
                    "mime_type": file_info["mime_type"],
                    "file_uri": file_info["uri"],
                }
            }
        )
    return parts


def summarize_with_gemini(
    issue_key: str,
    title: str,
    description: str,
    images: list[dict[str, Any]],
    api_key: str,
    model: str,
    related_context_markdown: str = "",
    temp_dir: str | None = None,
) -> str:
    image_batches = chunk_items(images, MAX_IMAGES_PER_BATCH)
    markdown_path: Path | None = None

    try:
        if len(image_batches) > 1:
            markdown_path = create_temp_markdown_path(issue_key, temp_dir=temp_dir)

        current_summary = ""
        if not image_batches:
            return call_gemini(
                api_key,
                model,
                [{"text": build_initial_prompt(title, description, related_context_markdown)}],
            ).strip()

        for index, batch_images in enumerate(image_batches):
            uploaded_files: list[dict[str, str]] = []
            if index == 0:
                parts = [{"text": build_initial_prompt(title, description, related_context_markdown)}]
            else:
                existing_markdown = markdown_path.read_text(encoding="utf-8")
                parts = [
                    {"text": build_followup_prompt(issue_key, title)},
                    {
                        "text": (
                            f"Current Markdown summary for issue {issue_key}:\n\n"
                            f"{existing_markdown}"
                        )
                    },
                ]

            try:
                for image in batch_images:
                    uploaded_files.append(upload_gemini_file(api_key, image))

                parts.extend(build_uploaded_file_parts(uploaded_files))
                current_summary = call_gemini(api_key, model, parts).strip()
            finally:
                for uploaded_file in uploaded_files:
                    try:
                        delete_gemini_file(api_key, uploaded_file["name"])
                    except RuntimeError:
                        pass

            if markdown_path is not None:
                markdown_path.write_text(current_summary, encoding="utf-8")

        return current_summary
    finally:
        if markdown_path is not None and markdown_path.exists():
            markdown_path.unlink()


def process_url(
    url: str,
    jira_email: str,
    jira_token: str,
    gemini_api_key: str,
    gemini_model: str,
    related_context_markdown: str = "",
    temp_dir: str | None = None,
) -> dict[str, str]:
    key = extract_issue_key_hint(url)
    title = ""
    session: requests.Session | None = None

    try:
        _, key = parse_jira_url(url)
        session = build_jira_session(jira_email, jira_token)

        issue_payload = fetch_issue(session, key)
        fields = issue_payload.get("fields", {})
        title = fields.get("summary", "")
        description = parse_description(fields.get("description"))
        images = collect_issue_images(session, key, fields.get("attachment", []))
        summary = summarize_with_gemini(
            issue_key=key,
            title=title,
            description=description,
            images=images,
            api_key=gemini_api_key,
            model=gemini_model,
            related_context_markdown=related_context_markdown,
            temp_dir=temp_dir,
        )
        return {"key": key, "title": title, "summary": summary}
    except Exception as error:
        return {
            "key": key,
            "title": title,
            "summary": f"{ERROR_PREFIX} {error}",
        }
    finally:
        if session is not None:
            session.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UCTalent Jira issue summarizer")
    parser.add_argument("--url", action="append", required=True, help="UCTalent Jira issue URL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file()

    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    gemini_model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

    if not all([jira_email, jira_token, gemini_api_key]):
        print(
            "ERROR: Missing required environment variables: "
            "JIRA_EMAIL, JIRA_API_TOKEN, GEMINI_API_KEY",
            file=sys.stderr,
        )
        return 1

    date_label = get_result_date_label()
    get_results_directory(current_date=date_label)
    batch_context_path: Path | None = None
    if len(args.url) > 1:
        batch_context_path = get_batch_context_path(current_date=date_label)
        batch_context_path.write_text("", encoding="utf-8")

    results: list[dict[str, str]] = []
    persist_errors: list[str] = []

    for index, url in enumerate(args.url):
        related_context_markdown = read_batch_context(batch_context_path)
        result = process_url(
            url,
            jira_email,
            jira_token,
            gemini_api_key,
            gemini_model,
            related_context_markdown=related_context_markdown,
        )
        results.append(result)
        try:
            persist_issue_result(result, current_date=date_label)
            if batch_context_path is not None and not result["summary"].startswith(ERROR_PREFIX):
                append_to_batch_context(batch_context_path, result)
        except OSError as error:
            persist_errors.append(str(error))
            issue_key = result.get("key") or args.url[index]
            print(
                f"ERROR: Failed to write result file for {issue_key}: {error}",
                file=sys.stderr,
            )

    final_results = results
    print(json.dumps(final_results, indent=2, ensure_ascii=False))

    has_errors = any(
        result["summary"].startswith(ERROR_PREFIX)
        for result in final_results
    )
    return 1 if has_errors or persist_errors else 0


if __name__ == "__main__":
    sys.exit(main())
