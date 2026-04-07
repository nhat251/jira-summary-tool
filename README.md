# Jira Issue Summarizer

Python CLI tool for UCTalent Jira issues. It fetches issue data from Jira Cloud, converts Jira description content to plain text, sends the task plus image attachments to Gemini, and prints a JSON list.

## What It Does

- Supports only `https://uctalent.atlassian.net/browse/...`
- Fetches Jira `summary`, `description`, and image attachments
- Sends at most 5 images per Gemini request
- For issues with more than 5 images, stores the intermediate Markdown summary in a temporary `.md` file and reuses it in the next Gemini batch
- Processes multiple issue URLs in parallel
- Always prints a JSON list, even if one issue fails
- Writes one Markdown file per issue to `result/dd-mm-yyyy/ISSUEKEY.md`

## Requirements

- Python 3.11+
- Environment variables:
  - `JIRA_EMAIL`
  - `JIRA_API_TOKEN`
  - `GEMINI_API_KEY`
  - Optional: `GEMINI_MODEL`
- The script auto-loads `.env` from the project root if those variables are not already present in the shell

## Setup

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

If you use VS Code, `.vscode/settings.json` already enables env-file loading from `.env`.

Example `.env`:

```env
JIRA_EMAIL=your_email@uctalent.com
JIRA_API_TOKEN=your_jira_api_token
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
```

## Usage

```powershell
python jira_issue_summarizer.py `
  --url "https://uctalent.atlassian.net/browse/UC-455" `
  --url "https://uctalent.atlassian.net/browse/UC-456"
```

Example output:

```json
[
  {
    "key": "UC-455",
    "title": "Fix login redirect bug",
    "summary": "## Summary\nUsers are redirected to the wrong page after login."
  }
]
```

Generated files:

- `result/07-04-2026/UC-455.md`
- `result/07-04-2026/UC-456.md`

Each file contains the issue key, title, and the final AI summary in Markdown.

If one issue fails, the tool still prints JSON and returns a non-zero exit code:

```json
[
  {
    "key": "UC-455",
    "title": "",
    "summary": "ERROR: Failed to access issue UC-455 (HTTP 403 - Forbidden)"
  }
]
```

## Testing

```powershell
python -m pytest tests
```
