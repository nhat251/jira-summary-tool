import os
import json
from unittest.mock import MagicMock, patch

import pytest

import jira_issue_summarizer as summarizer
from jira_issue_summarizer import (
    ERROR_PREFIX,
    MAX_IMAGES_PER_BATCH,
    extract_adf_text,
    load_env_file,
    main,
    parse_jira_url,
    persist_issue_result,
    process_url,
    summarize_with_gemini,
)


def make_gemini_response(text):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": text},
                    ]
                }
            }
        ]
    }
    return response


def test_parse_jira_url_supports_only_uctalent_browse_urls():
    host, key = parse_jira_url("https://uctalent.atlassian.net/browse/UC-455")
    assert host == "uctalent.atlassian.net"
    assert key == "UC-455"

    with pytest.raises(ValueError, match="Invalid format or unsupported URL"):
        parse_jira_url("https://foo.atlassian.net/browse/UC-455")

    with pytest.raises(ValueError, match="Invalid format or unsupported URL"):
        parse_jira_url("https://uctalent.atlassian.net/projects/UC/issues/UC-455")


def test_extract_adf_text_handles_paragraphs_lists_and_tables():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Intro"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "First"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Second"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "A"}],
                                    }
                                ],
                            },
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "B"}],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
        ],
    }

    text = extract_adf_text(adf)

    assert "Intro" in text
    assert "- First" in text
    assert "- Second" in text
    assert "A | B" in text


@patch("jira_issue_summarizer.requests.post")
def test_summarize_with_gemini_uses_text_only_when_no_images(mock_post):
    mock_post.return_value = make_gemini_response("Text only summary")

    summary = summarize_with_gemini(
        issue_key="UC-455",
        title="Fix login bug",
        description="Users cannot log in.",
        images=[],
        api_key="gemini-key",
        model="gemini-test",
    )

    assert summary == "Text only summary"
    payload = mock_post.call_args.kwargs["json"]
    parts = payload["contents"][0]["parts"]
    assert len(parts) == 1
    assert "Summarize this Jira task for a developer" in parts[0]["text"]


@patch("jira_issue_summarizer.requests.post")
def test_summarize_with_gemini_batches_images_and_cleans_temp_markdown(mock_post, tmp_path):
    mock_post.side_effect = [
        make_gemini_response("# Batch 1"),
        make_gemini_response("# Final summary"),
    ]
    images = [
        {
            "label": f"Issue: UC-455 | Image {index} | img-{index}.png",
            "mime_type": "image/png",
            "data": b"image-bytes",
        }
        for index in range(1, MAX_IMAGES_PER_BATCH + 3)
    ]

    summary = summarize_with_gemini(
        issue_key="UC-455",
        title="Fix dashboard bug",
        description="Something is broken.",
        images=images,
        api_key="gemini-key",
        model="gemini-test",
        temp_dir=str(tmp_path),
    )

    assert summary == "# Final summary"
    assert mock_post.call_count == 2

    first_parts = mock_post.call_args_list[0].kwargs["json"]["contents"][0]["parts"]
    second_parts = mock_post.call_args_list[1].kwargs["json"]["contents"][0]["parts"]

    assert len(first_parts) == 1 + (MAX_IMAGES_PER_BATCH * 2)
    assert first_parts[1]["text"] == "Issue: UC-455 | Image 1 | img-1.png"
    assert "Current Markdown summary for issue UC-455" in second_parts[1]["text"]
    assert "# Batch 1" in second_parts[1]["text"]
    assert not list(tmp_path.glob("*.md"))


@patch("jira_issue_summarizer.requests.post")
@patch("jira_issue_summarizer.requests.Session")
def test_process_url_returns_error_object_for_issue_http_errors(mock_session_cls, mock_post):
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    issue_response = MagicMock()
    issue_response.status_code = 403
    issue_response.reason = "Forbidden"
    mock_session.get.return_value = issue_response

    result = process_url(
        "https://uctalent.atlassian.net/browse/UC-455",
        "jira@example.com",
        "jira-token",
        "gemini-key",
        "gemini-test",
    )

    assert result["key"] == "UC-455"
    assert result["title"] == ""
    assert result["summary"].startswith(ERROR_PREFIX)
    assert "HTTP 403 - Forbidden" in result["summary"]
    mock_post.assert_not_called()


@patch("jira_issue_summarizer.requests.post")
@patch("jira_issue_summarizer.requests.Session")
def test_process_url_summarizes_only_successful_images(mock_session_cls, mock_post):
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    issue_response = MagicMock()
    issue_response.status_code = 200
    issue_response.json.return_value = {
        "fields": {
            "summary": "Fix Bug",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "String from ADF"}],
                    }
                ],
            },
            "attachment": [
                {
                    "mimeType": "image/png",
                    "filename": "screen-1.png",
                    "content": "https://files/image-1",
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "spec.pdf",
                    "content": "https://files/spec",
                },
                {
                    "mimeType": "image/jpeg",
                    "filename": "screen-2.jpg",
                    "content": "https://files/image-2",
                },
            ],
        }
    }

    image_ok = MagicMock()
    image_ok.status_code = 200
    image_ok.content = b"image-1-bytes"

    image_fail = MagicMock()
    image_fail.status_code = 404

    mock_session.get.side_effect = [issue_response, image_ok, image_fail]
    mock_post.return_value = make_gemini_response("This is a summary")

    result = process_url(
        "https://uctalent.atlassian.net/browse/UC-455",
        "jira@example.com",
        "jira-token",
        "gemini-key",
        "gemini-test",
    )

    assert result == {
        "key": "UC-455",
        "title": "Fix Bug",
        "summary": "This is a summary",
    }

    payload = mock_post.call_args.kwargs["json"]
    parts = payload["contents"][0]["parts"]

    assert "String from ADF" in parts[0]["text"]
    assert parts[1]["text"] == "Issue: UC-455 | Image 1 | screen-1.png"
    assert len(parts) == 3


def test_main_prints_json_returns_non_zero_and_writes_markdown_results(
    monkeypatch,
    capsys,
    tmp_path,
):
    monkeypatch.setenv("JIRA_EMAIL", "jira@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(summarizer, "RESULTS_ROOT_DIR", tmp_path / "result")
    monkeypatch.setattr(summarizer, "get_result_date_label", lambda: "07-04-2026")

    def fake_process_url(url, *_args, **_kwargs):
        if url.endswith("UC-455"):
            return {"key": "UC-455", "title": "A", "summary": "All good"}
        return {"key": "UC-456", "title": "", "summary": "ERROR: denied"}

    with patch("jira_issue_summarizer.process_url", side_effect=fake_process_url):
        exit_code = main(
            [
                "--url",
                "https://uctalent.atlassian.net/browse/UC-455",
                "--url",
                "https://uctalent.atlassian.net/browse/UC-456",
            ]
        )

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 1
    assert output == [
        {"key": "UC-455", "title": "A", "summary": "All good"},
        {"key": "UC-456", "title": "", "summary": "ERROR: denied"},
    ]
    assert (tmp_path / "result" / "07-04-2026" / "UC-455.md").read_text(encoding="utf-8")
    assert (tmp_path / "result" / "07-04-2026" / "UC-456.md").read_text(encoding="utf-8")


def test_load_env_file_sets_missing_values_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JIRA_EMAIL=loaded@example.com",
                "JIRA_API_TOKEN='loaded-token'",
                'GEMINI_API_KEY="loaded-key"',
                "# comment",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.setenv("JIRA_API_TOKEN", "existing-token")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    load_env_file(env_file)

    assert os.environ["JIRA_EMAIL"] == "loaded@example.com"
    assert os.environ["JIRA_API_TOKEN"] == "existing-token"
    assert os.environ["GEMINI_API_KEY"] == "loaded-key"


def test_persist_issue_result_writes_expected_markdown_file(tmp_path):
    result = {
        "key": "UC-455",
        "title": "Search bug",
        "summary": "## Summary\nUsers see wrong results.",
    }

    file_path = persist_issue_result(
        result,
        root_dir=tmp_path / "result",
        current_date="07-04-2026",
    )

    assert file_path == tmp_path / "result" / "07-04-2026" / "UC-455.md"
    content = file_path.read_text(encoding="utf-8")
    assert "# UC-455" in content
    assert "**Title:** Search bug" in content
    assert "Users see wrong results." in content
