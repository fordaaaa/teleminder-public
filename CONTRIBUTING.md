# Contributing to Teleminder

Fork the repository, create a branch, and open a pull request against `main`. Describe what changed, why, and how you checked it. For fixes, include a regression test with synthetic data when practical.

Install the development dependencies and run the checks:

```sh
uv sync --frozen --extra dev --python 3.12
uv run pytest -q
uv run ruff check teleminder tests scripts
uv run ruff format --check teleminder tests scripts
```

Use `feat:` for a feature commit and `fix:` for a correction. Keep Telegram IDs, bot tokens, MCP credentials, server addresses, and SQLite files out of commits and pull requests.

For a suspected security issue, follow [SECURITY.md](SECURITY.md).
