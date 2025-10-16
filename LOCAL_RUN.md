# Archon Local Runtime Guide

Run the backend and tooling directly on macOS while keeping Supabase in the cloud. This avoids Docker's 8 GB virtualization overhead and lets Archon comfortably fit in 16 GB RAM systems.

## Prerequisites
- Apple silicon macOS with the command line tools installed
- Node.js 18+ and npm (`node --version`)
- Python 3.12+ with [uv](https://docs.astral.sh/uv/getting-started/installation/) in your PATH
- `.env` configured with your cloud Supabase `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
- Docker Desktop stopped (only needed when you explicitly want containers again)

## One-Time Setup
1. Shut down any running containers: `make stop` (or `docker compose down`). Quit Docker Desktop to reclaim RAM.
2. Install project dependencies:
   ```bash
   cd archon-ui-main && npm install
   cd ../python && uv sync --group all --group dev
   ```
3. (Optional) disable Docker Desktop auto-start so it stays dormant during local runs.

## Daily Workflow
Open three terminal tabs/windows from the project root.

1. **Backend API & workers**
   ```bash
   cd python
   uv run python -m src.server.main
   ```
   Uvicorn hot-reloads on code changes and reads ports from `.env` (defaults to 8181).

2. **MCP server (for IDE agents & Claude tooling)**
   ```bash
   cd python
   uv run python -m src.mcp_server.mcp_server
   ```
   Required for the MCP endpoints the UI expects. Ports come from `.env` (default 8051).

3. **Frontend (Vite dev server)**
   ```bash
   cd archon-ui-main
   npm run dev
   ```
   Vite proxies API calls to the backend. Visit http://localhost:3737.

4. **Optional – Agents service**
   ```bash
   cd python
   uv run python -m src.agents.server
   ```
   Only needed if you use the reranking/agents features (port 8052 by default).

## Stopping Services
- Press `Ctrl+C` in each terminal tab when finished.
- No extra cleanup is necessary; remote Supabase persists your data.

## Switching Back to Docker (if needed)
- Start all containers again with `make dev-docker` (or `docker compose up`).
- Stop them with `make stop` when you return to the local workflow.

## Troubleshooting
- **Port conflicts:** adjust the `ARCHON_*_PORT` values in `.env` and restart the affected process.
- **Supabase auth errors:** ensure the `SUPABASE_SERVICE_KEY` is the service-role key, not the anon key.
- **Missing dependencies:** rerun the install commands above or delete `python/.venv` if uv used an old environment.

With this setup the combined backend + MCP + frontend footprint should stay under ~2 GB RAM, eliminating the performance bottlenecks caused by Docker's virtualization layer.

## Maintaining Local Markdown Archive Patch

This environment includes a local-only enhancement that saves crawled markdown to the host filesystem and mounts it into the `archon-server` container via `/Data_dir`. To keep the behaviour intact when pulling upstream updates:

1. Development happens on branch `feature/markdown-archive`. The branch lives on the personal fork at `https://github.com/mstrar76/Archon.git` (remote name `myfork`).
2. Update workflow:
   ```bash
   # sync upstream main
   git checkout main
   git pull origin main

   # rebase local patch branch
   git checkout feature/markdown-archive
   git rebase origin/main

   # push latest branch to personal fork
   git push myfork feature/markdown-archive
   ```
3. The markdown archive directory is bind-mounted from `./data/scraped_markdown` (ignored via `.gitignore`). Keep that path if you expect the container to persist files; adjust `docker-compose.yml` and `ARCHON_MARKDOWN_ARCHIVE_DIR` together if you relocate it.

Future agents should keep the branch rebased before pulling additional upstream changes to avoid losing the archive functionality.
