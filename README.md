# General Browser AI Agent

Production-minded MVP for a browser-based AI research agent, built Python-first with FastAPI, Playwright, SQLModel, and a minimal observation UI.

## What this MVP includes

- Modular research agent pipeline
- Typed task, plan, source, evidence, browser action, and answer models
- Controlled browser research via Playwright
- Evidence extraction for web pages and YouTube metadata
- Source ranking and confidence scoring
- Audit-friendly task persistence and action logging
- Minimal Stage 1 debug interface plus Stage 2 operator UI
- Safe-by-default browsing constraints

## Run locally

1. Create a virtual environment and install dependencies:

```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
```

2. Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY` if you want LLM-backed planning and answer synthesis.

3. Start the app:

```bash
python -m uvicorn app.main:app --reload
```

4. Open `http://localhost:8000/`.

## How To Check The Agent

1. Start the server and open the UI.
2. Submit a research query such as:

```text
Compare retrieval augmented generation and long-context prompting, then recommend one useful YouTube explainer.
```

3. Watch these panels update:
- `Task Status`: planning, researching, ranking, verifying, composing
- `Live Browser Preview`: latest browser screenshot from Playwright
- `Ranked Sources`: scored source list
- `Evidence`: extracted excerpts and table data
- `Final Answer`: concise answer with citations
- `YouTube Recommendation`: best matched video when relevant

4. You can also inspect the raw task payload directly:

```bash
curl http://localhost:8000/api/tasks
curl http://localhost:8000/api/tasks/<task_id>
```

## Gemini Notes

- The existing agent code path is preserved; only the LLM adapter was changed.
- The planner, intake, and answer composer still call the same `llm_client` interface.
- Gemini is now the default LLM provider through the REST API.
- Recommended production setup:
  - `gemini-2.5-flash` for intake, planning, ranking helpers, and other fast agent steps
  - `gemini-2.5-pro` for final answer composition and harder reasoning
- Avoid using Gemini 3 as the default production model right now because Google currently documents Gemini 3 models as preview.

## Troubleshooting

- If you see `ModuleNotFoundError: No module named 'sqlmodel'`, the server is usually running from the wrong Python interpreter.
- This project targets Python 3.11+, so create the venv with Python 3.11 and start Uvicorn with `python -m uvicorn ...` instead of the global `uvicorn` command.
- You can confirm the active interpreter with:

```bash
python --version
python -c "import sys; print(sys.executable)"
```

## Architecture

- `app/agent`: intake, planning, orchestration, verification, answer composition
- `app/browser`: Playwright browser control and search execution
- `app/extraction`: page and YouTube extraction
- `app/ranking`: source and video ranking
- `app/storage`: persistence and repositories
- `app/api`: task API and UI routes
- `app/ui`: minimal operator-facing frontend

## Notes

- SQLite is the default local database. Set `DATABASE_URL` to PostgreSQL for production.
- Stage 3 verticals are intentionally not implemented yet. The current codebase is structured so they can be added behind the same task engine later.
