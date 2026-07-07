# Contributing to SanctionPay

Thanks for your interest in improving SanctionPay — an AI-powered sanction
screening agent on the Casper Network. This guide covers how to set up the
project, the branch/PR workflow, and our coding conventions.

## Project layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI service — x402-protected `/check`, real sanction-list ingestion, AI analysis, Casper recording |
| `agent/` | Autonomous Claude agent that screens entities and pays via x402 |
| `contracts/` | Rust + Odra smart contract (`record_check` / `get_result`) for Casper |
| `frontend/` | Static screening dashboard (`index.html`) |
| `docker-compose.yml` | Local stack: Casper NCTL + backend + frontend |

## Local development

### Option A — Docker (full stack incl. local Casper network)

```bash
cp .env.example .env    # fill in keys you have; blanks degrade gracefully
docker compose up
# Frontend  http://localhost:3000
# API docs  http://localhost:8000/docs
```

### Option B — Native (fastest for the backend + UI)

```bash
# Backend
cd backend
python -m venv .venv && . .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend (separate shell)
cd frontend && python -m http.server 3000
```

On Windows you can run both at once with `./run-local.ps1`.

### Contract

```bash
cd contracts
cargo test                 # unit tests (Odra test backend)
cargo odra test -b casper  # against the Casper VM
```

## Branch & PR workflow

1. Fork the repo (or branch, if you have write access).
2. Create a topic branch: `feat/…`, `fix/…`, `chore/…`, `ci/…`, or `docs/…`.
3. Keep PRs focused — one concern per PR.
4. Ensure CI is green (lint, Python import smoke, contract tests).
5. Fill out the PR template and link any related issue.

## Coding conventions

- **Python**: format with `ruff format`; keep functions small and typed. Don't
  use bare `except:` in new code — catch specific exceptions.
- **Rust**: `cargo fmt` before committing; `cargo clippy` should be clean.
- **Secrets**: never commit `.env` or private keys. `.env.example` documents the
  variables; real values stay local.
- **Commits**: imperative mood, present tense (e.g. "add CodeQL workflow").

## Reporting bugs & requesting features

Use the issue templates under **Issues → New issue**. For security issues, do
**not** open a public issue — see [SECURITY.md](SECURITY.md).
