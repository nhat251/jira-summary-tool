# Python Jira -> Gemini Summary CLI

## Summary

- Tạo một CLI Python 3.11 standalone gồm `jira_issue_summarizer.py`, `requirements.txt`, và `tests/test_jira_issue_summarizer.py`.
- CLI chỉ nhận nhiều `--url`, trích key từ từng Jira issue URL, gọi đúng Jira host lấy dữ liệu issue, gửi sang Gemini để tóm tắt, rồi in JSON list ra stdout theo đúng thứ tự input.
- Biến môi trường bắt buộc: `JIRA_EMAIL`, `JIRA_API_TOKEN`, `GEMINI_API_KEY`. Biến tùy chọn: `GEMINI_MODEL`, mặc định `gemini-3-flash-preview`.

## Key Changes

- Jira ingestion: parse từng URL, suy ra base host động, trích key dạng `ABC-123`, rồi gọi `GET /rest/api/3/issue/{key}?fields=summary,description,attachment` bằng `requests.Session` + `HTTPBasicAuth`.
- Description handling: chuyển `description` sang plain text bằng một recursive walker cho Atlassian Document Format; nếu `description` là string hoặc `null` thì passthrough/fallback an toàn.
- Attachment handling: chỉ xử lý attachment có `mimeType` bắt đầu bằng `image/`; bỏ qua toàn bộ file khác. Ảnh sẽ được tải từ Jira, encode base64, rồi gửi sang Gemini dưới dạng `inline_data`.
- Gemini request: giữ nguyên prompt text:
  `Summarize this Jira task for a developer:\nTitle: {title}\nDescription: {description}`
  và đính kèm 0..n image parts phía sau trong cùng request `generateContent`.
- Networking/runtime: dùng `ThreadPoolExecutor` để xử lý nhiều issue song song, có timeout và retry ngắn cho lỗi 429/5xx, giữ nguyên output order.
- Failure policy: nếu một item lỗi, vẫn trả object cùng schema `{ "key": "...", "title": "...", "summary": "ERROR: ..." }`; in đủ JSON list và exit non-zero nếu có ít nhất một item lỗi.

## Public Interface

- Command: `python jira_issue_summarizer.py --url <jira-url> --url <jira-url> ...`
- Output giữ đúng schema: `[{"key":"...","title":"...","summary":"..."}]`
- Không thêm `.env` loader; script đọc trực tiếp environment hiện có để giữ dependency tối thiểu và đúng yêu cầu `requests`.

## Test Plan

- Parse URL cho `browse` URL mẫu `https://uctalent.atlassian.net/browse/UC-455`, board/detail URLs, query strings, URL sai format, và mixed Jira hosts.
- ADF-to-text cho paragraph, list, heading, code block, table, link/mention, description rỗng/null.
- Attachment filtering để chỉ ảnh được biến thành Gemini inline parts, file không phải ảnh bị bỏ qua.
- Mock `requests` cho Jira/Gemini để kiểm tra request payload, response parsing, partial failure, và output order khi chạy song song.

## Assumptions

- `GEMINI _API_KEY` trong mô tả là lỗi gõ; script sẽ dùng `GEMINI_API_KEY`.
- Đây là tool nội bộ UCTalent, nhưng sẽ hỗ trợ mọi Jira Cloud URL hợp lệ bằng cách suy ra host từ từng issue URL.
- Ảnh không OCR cục bộ; Gemini sẽ nhận raw image bytes dưới dạng base64 vision input.
- Model được để configurable vì tài liệu Google hiện dùng `gemini-3-flash-preview` cho `generateContent` text/image, trong khi các model 2.0 đã có lịch shutdown trong năm 2026.

## References

- Atlassian issue API: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
- Atlassian basic auth: https://developer.atlassian.com/cloud/forms/security/basic-auth/
- Gemini text generation REST: https://ai.google.dev/gemini-api/docs/text-generation
- Gemini image understanding / inline image data: https://ai.google.dev/gemini-api/docs/image-understanding
- Gemini pricing / free tier: https://ai.google.dev/gemini-api/docs/pricing
- Gemini deprecations: https://ai.google.dev/gemini-api/docs/deprecations
