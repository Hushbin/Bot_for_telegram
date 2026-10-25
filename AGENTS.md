# Repository Guidelines

## Project Structure & Module Organization

`main.py` builds the Telegram application; `handlers.py` implements conversations and commands. `config.py` loads settings, while `database.py` and `storage.py` provide OCI persistence. `local_backend.py` uses SQLite and files for offline development. Database schema and setup live in `db/schema.sql` and `scripts/`; VM deployment files are in `deploy/`. Tests live in `tests/test_*.py`, with shared fakes in `tests/fakes.py`. Keep runtime data in ignored `.local/` and credentials out of Git.

## Documentation

Keep `README.md` and `AGENTS.md` at the repository root. Put other maintained
Markdown documentation in `docs/` and link it from `docs/README.md`. When moving
or removing a document, update references and preserve still-relevant decisions
or unresolved findings in the current guides. Remove completed plans and
superseded drafts rather than leaving conflicting instructions; Git preserves
history. Keep validation records dated and distinguish automated checks from
manual checks that have not been performed.

## Build, Test, and Development Commands

Use Python 3.10 or newer. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.local.example .env  # Preserve any existing .env
.venv/bin/python main.py
.venv/bin/python -m unittest discover -s tests -v
```

Set `APP_BACKEND=local` and a separate test `BOT_TOKEN` in `.env` to run without OCI; the bot still needs Telegram access and polls for messages. The test command needs no credentials or network. Edit direct dependencies in `requirements.in`, then regenerate the pinned, hashed `requirements.txt` with `uv pip compile requirements.in --universal --generate-hashes --python-version 3.10 -o requirements.txt`.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` for modules, functions, and variables, and `PascalCase` for classes. Follow the existing type hints and keep handler logic separate from persistence adapters. No formatter or linter is configured; keep changes consistent with nearby code.

## Testing Guidelines

Use `unittest`; name files `test_*.py` and methods `test_*`. Add focused tests for changed conversations, storage behavior, or schema migrations. `test_store_contract.py` runs against fake and SQLite stores by default; Oracle contract tests require an explicitly configured **throwaway** database. CI runs the full default suite on pull requests. There is no coverage threshold. Agents should run available checks themselves and report results; ask the user to run a check only when their access or action is required.

## Commit & Pull Request Guidelines

History uses concise imperative subjects (`Move bot catalog and media to OCI`) and prefixes such as `feat:` or `chore:`. Keep commits focused. Pull requests should explain behavior changes, link relevant issues, list test results, and include a bot chat example for user-facing changes. Call out schema changes: run `scripts/setup_db.py` before deploying a release that adds columns. Successful pushes to `master` with changes beyond Markdown documentation trigger deployment through `.github/workflows/deploy.yml`.

## Security & Configuration

Never commit `.env`, `.setup.env`, wallet files, tokens, or database passwords. Use `.env.example` and `.env.local.example` as templates. Keep OCI credentials and production data out of tests; use the local backend for routine development.
