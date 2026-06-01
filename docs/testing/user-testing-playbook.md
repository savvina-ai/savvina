# Savvina AI — User Testing Playbook

Reference connections, sample questions, and structured QA sessions for
manually validating end-to-end behaviour across all major features.

Use these sessions after deploying a new build, before a release, or when
investigating a regression. Sessions are independent — run them in any order.

> **Credentials note:**
> Passwords for the `savvina` superuser on `sample-postgres` (Connections 1–2) are set via the
> `SAMPLE_POSTGRES_PASSWORD` env var in your `.env` file. The `analyst_readonly_2024` read-only
> role password is seeded by the PostgreSQL init scripts and is fixed unless you re-seed the
> container.

---

## Datasource Playbooks

| Datasource | File | Connections | Sessions |
|---|---|---|---|
| PostgreSQL  | [user-testing-postgresql.md](user-testing-postgresql.md) | 1, 2 | 1, 2, 3, 6, 7, 8, 9, 10 |
| MySQL       | [user-testing-mysql.md](user-testing-mysql.md) | 3 | 11 |

---

## Prerequisites

- All connections above are configured in the app
- At least one LLM provider is configured and healthy (Groq or Gemini free tiers work)
- App is reachable at `http://localhost:3000`

---

## Session Index

| # | Session | Connection | Execution Mode | Focus |
|---|---------|-----------|----------------|-------|
| 1 | [Basic SQL Happy Path](user-testing-postgresql.md#session-1--basic-sql-happy-path) | PostgreSQL Full Access | Auto-Execute | Core query flow |
| 2 | [Permission Boundary](user-testing-postgresql.md#session-2--permission-boundary) | PostgreSQL Read-Only | Auto-Execute | Schema scoping & RLS |
| 3 | [Review First Editing](user-testing-postgresql.md#session-3--review-first-editing) | PostgreSQL Full Access | Review First | Human-in-the-loop SQL |
| 6 | [Multi-Turn Context](user-testing-postgresql.md#session-6--multi-turn-context) | PostgreSQL Full Access | Auto-Execute | Conversation continuity |
| 7 | [LLM Provider Switching](user-testing-postgresql.md#session-7--llm-provider-switching) | PostgreSQL Full Access | Auto-Execute | Provider resilience |
| 8 | [Cache Hit Behaviour](user-testing-postgresql.md#session-8--cache-hit-behaviour) | PostgreSQL Full Access | Auto-Execute | Query cache |
| 9 | [Semantic Model Influence](user-testing-postgresql.md#session-9--semantic-model-influence) | PostgreSQL Full Access | Auto-Execute | Business term mapping |
| 10 | [Generate Only Mode](user-testing-postgresql.md#session-10--generate-only-mode) | PostgreSQL Full Access | Generate Only | SQL export without execution |
| 11 | [MySQL Food Delivery](user-testing-mysql.md#session-11--mysql-food-delivery) | MySQL Food Delivery | Auto-Execute | MySQL adapter & ENUM columns |
