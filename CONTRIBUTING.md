# Contributing to Savvina AI

Thank you for your interest in contributing. Please read this document
before opening a Pull Request.

## Contributor License Agreement

By submitting a Pull Request you agree to the terms of our
[Contributor License Agreement](CLA.md). This is required before
any contribution can be accepted.

CLA signing is automated via CLA Assistant and will be triggered
automatically when you open your first PR.

## What we welcome

- Bug fixes with a linked issue
- New data source adapters (single new file in `backend/app/datasources/`)
- New LLM provider adapters (single new file in `backend/app/providers/`)
- Documentation improvements
- Test coverage improvements

## What requires prior discussion

Changes to core services (ChatService, QueryCache, SemanticModel,
PromptBuilder) must have a linked GitHub Issue with maintainer
acknowledgment before a PR is opened. This prevents effort spent
on contributions that conflict with the commercial roadmap.

## How to contribute

1. Fork the repository
2. Create a branch: `feat/your-feature` or `fix/your-fix`
3. Follow conventional commit format (see below)
4. Open a Pull Request against `main`
5. Sign the CLA when prompted
6. Wait for review

## Commit message format

This project uses conventional commits. Every commit must follow
this format:

```
type: short description

body (optional)
```

Types: `feat`, `fix`, `docs`, `chore`, `perf`, `refactor`, `test`

Examples:

```
feat: add connection-level query timeout setting
fix: resolve MySQL timeout on large schemas
docs: update quickstart guide for Ollama
```

PRs with commits that do not follow this format will be asked to
rebase before merging.

## Development setup

```bash
git clone https://github.com/savvina-ai/savvina
cd savvina
cp .env.example .env
docker compose up --build
```

See [docs/development/testing.md](docs/development/testing.md) for
running the test suite.

## Code style

- Backend: `ruff check app/ tests/` must pass with zero warnings
- Backend: `ruff format --check app/ tests/` must pass (run `ruff format app/ tests/` to auto-fix)
- Frontend: `eslint` and `tsc --noEmit` must pass
- All new backend files must include the license header (see below)

## License header

Every new source file must include this header:

Python:
```python
# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.
```

TypeScript/JavaScript:
```typescript
// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.
```
