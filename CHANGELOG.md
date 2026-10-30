# CHANGELOG

<!-- version list -->

## v3.0.0 (2026-10-30)

### Bug Fixes

- **deps**: Bump vitest to 4.1.12 and postcss-selector-parser to 6.1.5 to clear the moderate npm
  advisories ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **frontend**: Size the rotated chart x-axis from its labels so full names show and the legend no
  longer overlaps them ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **pipeline**: Tell degenerate LLM output apart from a real truncation and report the output budget
  that was hit ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Add a read-only explain to the datasources, record where sample values came from, and
  treat stale stored results in the share, export and chat paths
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Classify result masks by column position from the engine tokens and fail closed on
  ambiguous scopes ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Close strict-mode bypasses, detect MySQL quoting modes, refuse SET statements,
  classify own refusals as REJECTED and let an unreadable connection be re-entered
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Close the phase 3a review findings in row-filter verification, prompt hints,
  fingerprints and stale stored results ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Keep MySQL warnings out of the log, refuse set_config and user-variable assignment
  that bypass masking, warn on removed default patterns, show only a refusal's explanation and drop
  the backtick hint from PostgreSQL prompts ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Log execution errors in their redacted form and give query rejections a shared
  QueryRejectedError base ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Save row-filter keys in their quoted form and handle stale results in the report
  builder and share page ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Show the stale-result notice in the results view, the chat message and the shared
  session page ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Withhold sensitive columns' comments from the LLM, document that a MySQL connection
  sees every database its account can read, and drop the sample-values wording from the generation
  prompt ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

### Chores

- **frontend**: Bundle the fonts with @fontsource instead of loading them from Google Fonts and
  tighten the CSP ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Commit the earlier backend work this branch depends on
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

### Documentation

- Describe the new masking and log protections and the removed-defaults warning, add message_id to
  the edit request, state the savvina_demo default and fix the MySQL analyst password
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- Describe the read-only transaction, the sample-value sources, the key-rotation effect on stored
  results and the generation path ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Describe the exact-name sensitive columns, the strict mode and its limits, and the
  redacted log line ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Describe the privilege audit, its findings, remediation and limits per engine, and
  note that value mappings reach the LLM by design
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Describe the rewriter-planned verification, its time budget, the stale rules and the
  quoted keys ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Per-table row filters, the migration state, masking limits and the live row-filter
  test ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Row filter is editable and validated; LIMIT/OFFSET shapes hoisted
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

### Features

- **frontend**: Add the exact-name sensitive columns list and the strict sensitive-columns toggle to
  the privacy settings ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **frontend**: Show the privilege audit findings with copyable remediation SQL on the connection
  page ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Add exact-name sensitive columns and the opt-in strict sensitive-column guard on
  every executed statement ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Audit the connection role's privileges on PostgreSQL and MySQL/MariaDB and store the
  findings with their REVOKE remediation on the connection
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Editable, validated row filter with LIMIT/OFFSET hoisting and clean verified examples
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Per-table row filters in the Privacy Settings form with validation and a migration
  banner ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Per-table row filters rewritten with sqlglot replace the whole-query filter, which
  now puts a connection in a migration state ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))

- **privacy**: Row filter input in the Privacy Settings form with the save error shown inline
  ([#22](https://github.com/savvina-ai/savvina/pull/22),
  [`d7d5d29`](https://github.com/savvina-ai/savvina/commit/d7d5d2987d87a0a5219d18d25300571131d3a5fb))


## v2.2.0 (2026-10-26)

### Bug Fixes

- **api**: Stop serving the never-updated semantic_model_updated_at on connections and drop it from
  the client types ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **backend**: Answer 503 when an LLM provider is unavailable or a row-lock wait passes
  DB_LOCK_TIMEOUT_SECONDS, and retry both on the semantic page
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **deps**: Fastembed 0.9.1 and pillow 13.0.0 clear the 37 pillow advisories that failed the
  dependency audit ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **frontend**: Bump vite to 8.3.5 and follow-redirects to 1.16.2 so the dependency audit passes
  again ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **privacy**: Strip excluded columns and their foreign keys in _apply_privacy_to_schema so chat
  validation and pruning never see a stale hidden column, and drop the chat filter's private copy of
  that rule ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **providers**: Cerebras default becomes gpt-oss-120b and a 404 for an unknown model, like every
  API error in a health check, is worded for the user instead of dumping the SDK exception
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Apply Drift and Regenerate rename a near-miss column key to its live name instead of
  re-annotating it ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Bound indexed Text identities by bytes so a long join pattern cannot fail a save or
  the backfill, and drop unstorable generated items through the store's own rule
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Check Drift completes an older baseline with stored hidden names so un-hiding a
  column renames its key, a near-miss key of an unseen column is worded as one, and one builder and
  one reference mapper replace the duplicated baseline and reference-field code
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Close the three 2026-10-15 review passes on the semantic store, generation and LLM
  error mapping ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Fit history entity keys to their column so a 512-character table key plus column
  name cannot fail a reconcile or PUT ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Keep generated tables when finishing an interrupted run, and stamp the schema hash
  on regenerate ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Keep tables the connection's role cannot read instead of pruning them as dropped
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Make /generate/batch send a run-less tables_partial header back to /generate/init
  instead of stamping tables with no run id ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Make the table-field writer private so every content write goes through the header
  version bump, tighten the protected-values guard, and correct the privacy docs
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Steer schema-changed errors to Regenerate, keep privacy-hidden items out of drift
  and generation prompts, and skip soft-deleted connections in the backfill
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Treat a bare table name the privacy policy excludes as hidden, not drift
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic-ui**: Make the regenerate dialog describe batched generation accurately
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic-ui**: One generation mutation keyed per connection, runs blocked while edits are
  unsaved, editors paused during a run or apply, a fresh reseed after a failed run, and one retry of
  tables a batch omitted ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

### Documentation

- **security**: Match the disclosure terms savvina-prod and the trust center publish
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Describe the normalised semantic store, resumable generation and how hand edits and
  privacy-hidden items are kept ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

### Features

- **build**: Add optional CA_BUNDLE secret so the model download works behind a TLS-inspecting proxy
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add generation run state and a reconcile that preserves hand edits to the semantic
  store ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add key-addressed table generation grounded in the batch's real schema, plus the
  batch request and response schemas ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add migration 0002 creating the normalised semantic tables, with a test that checks
  it against the ORM ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add migration 0003 that backfills the normalised semantic tables from each
  connection's JSON blob ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add ORM rows for the normalised semantic store and an in-memory SQLite test fixture
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Add the semantic store's save, load and delete over the normalised tables, fitting
  values to PostgreSQL column limits ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Apply Drift annotates columns added to tables the model already holds
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Apply Drift syncs the stored semantic model to schema drift without a full
  regenerate ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Apply user edits through the semantic store with per-field provenance, an audit
  trail and optimistic locking ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Check Drift compares against a live introspection, falls back to the user's cached
  schema when the source cannot be reached, and Schema Refresh shares the same refresh helper
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Keep hand-edited fields and relationships through a full rebuild of the semantic
  store ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Make phased generation resumable and key-addressed on the semantic store, pruning
  tables and columns that left the schema but keeping what the privacy policy hides
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Read the chat path's semantic model from the store, keep privacy-hidden columns out
  of prompts, and stop reading the legacy blob
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Serve read, update, delete, drift and suggestion apply from the semantic store
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic-ui**: Key-addressed generation with a Resume/Finish/Restart banner, confirmed restart,
  and Save locked during runs and kept off stale drafts after failures
  ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

### Performance Improvements

- **chat**: Serve the semantic model from a per-process cache keyed on the header version, and bump
  that version on every content write ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

- **semantic**: Read provenance marks and protected values in one pass under the globals lock
  instead of scanning every row twice ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))

### Refactoring

- **semantic**: Clear the legacy blob inside store.delete_model so both delete routes stop carrying
  their own copy of it ([#21](https://github.com/savvina-ai/savvina/pull/21),
  [`8bfe55d`](https://github.com/savvina-ai/savvina/commit/8bfe55d31310f9b6042c1d7af42c5b1e615988b8))


## v2.1.2 (2026-10-08)

### Bug Fixes

- **deps**: Bump axios to 1.20.0 and brace-expansion to 5.0.12 and 1.1.21 to clear their security
  advisories ([#20](https://github.com/savvina-ai/savvina/pull/20),
  [`54beab4`](https://github.com/savvina-ai/savvina/commit/54beab4c3704dd601dab40deeab47027204dde50))

- **deps**: Bump PyJWT to 2.15.1 to clear the open PyJWT security advisories
  ([#20](https://github.com/savvina-ai/savvina/pull/20),
  [`54beab4`](https://github.com/savvina-ai/savvina/commit/54beab4c3704dd601dab40deeab47027204dde50))

### Continuous Integration

- **audit**: Allow the unfixable build-time braces advisory in the frontend dependency audit
  ([#20](https://github.com/savvina-ai/savvina/pull/20),
  [`54beab4`](https://github.com/savvina-ai/savvina/commit/54beab4c3704dd601dab40deeab47027204dde50))

### Documentation

- **readme**: Add project website and Docker Hub links
  ([#19](https://github.com/savvina-ai/savvina/pull/19),
  [`5b8f67d`](https://github.com/savvina-ai/savvina/commit/5b8f67ddc90431abd8a6e6098ac9903435eda1dd))


## v2.1.1 (2026-09-22)

### Bug Fixes

- **setup**: Fix .env corruption from UID/GID append and improve sample-db docs in README
  ([`fed7296`](https://github.com/savvina-ai/savvina/commit/fed7296d357795b896bb200973577e2b7438b19f))

- **test-dbs**: Derive sample Postgres dates from CURRENT_DATE so seeded data stays current
  ([`2c0d44a`](https://github.com/savvina-ai/savvina/commit/2c0d44aa559dee65d0dc8b9c628a8b1ec4acc383))


## v2.1.0 (2026-09-22)

### Bug Fixes

- Publish multi-arch images to Docker Hub, move TLS out of the frontend container, and close review
  findings ([#17](https://github.com/savvina-ai/savvina/pull/17),
  [`9c91a21`](https://github.com/savvina-ai/savvina/commit/9c91a210dc9e9706f9d358ff9746dd571bb972cd))

### Features

- Docker hub setup ([#17](https://github.com/savvina-ai/savvina/pull/17),
  [`9c91a21`](https://github.com/savvina-ai/savvina/commit/9c91a210dc9e9706f9d358ff9746dd571bb972cd))

### Refactoring

- **settings**: Move the custom-provider service list into its own module so a test can pin which
  services are offered ([#17](https://github.com/savvina-ai/savvina/pull/17),
  [`9c91a21`](https://github.com/savvina-ai/savvina/commit/9c91a210dc9e9706f9d358ff9746dd571bb972cd))

- **settings**: Move the custom-provider service list into its own module so a test can pin which
  services are offered ([#16](https://github.com/savvina-ai/savvina/pull/16),
  [`adcf021`](https://github.com/savvina-ai/savvina/commit/adcf021c12da35b0ef1cc5fdaa13a9bcade8c0ee))


## v2.0.0 (2026-09-20)

### Bug Fixes

- **settings**: Read cache TTL live, share staged-settings and error handling across tabs, and
  finish the GitHub Models removal ([#15](https://github.com/savvina-ai/savvina/pull/15),
  [`86efe30`](https://github.com/savvina-ai/savvina/commit/86efe30d7732f183d575f49e074d095892e74704))

- **settings**: Write settings through to the runtime singleton, stage cache/pruning toggles until
  Save, and restructure the tabs ([#15](https://github.com/savvina-ai/savvina/pull/15),
  [`86efe30`](https://github.com/savvina-ai/savvina/commit/86efe30d7732f183d575f49e074d095892e74704))

### Chores

- **deps**: Update frontend dev dependencies to clear all nine npm audit advisories
  ([#15](https://github.com/savvina-ai/savvina/pull/15),
  [`86efe30`](https://github.com/savvina-ai/savvina/commit/86efe30d7732f183d575f49e074d095892e74704))

### Documentation

- Correct the docs where they disagreed with the code, and delete the unused provider config schema
  they described ([#15](https://github.com/savvina-ai/savvina/pull/15),
  [`86efe30`](https://github.com/savvina-ai/savvina/commit/86efe30d7732f183d575f49e074d095892e74704))

### Refactoring

- **providers**: Remove env-var LLM API keys so provider credentials come only from encrypted
  UI-saved configs ([#15](https://github.com/savvina-ai/savvina/pull/15),
  [`86efe30`](https://github.com/savvina-ai/savvina/commit/86efe30d7732f183d575f49e074d095892e74704))


## v1.0.6 (2026-08-27)

### Bug Fixes

- **ci**: Restore semantic-release under GitPython 3.1.60
  ([#14](https://github.com/savvina-ai/savvina/pull/14),
  [`0a97ffe`](https://github.com/savvina-ai/savvina/commit/0a97ffebb87b53077cd8632c97862fb78339a040))

- **deps**: Bump sqlparse to 0.6.0 and fail closed on its new SQLParseError
  ([#13](https://github.com/savvina-ai/savvina/pull/13),
  [`9732d57`](https://github.com/savvina-ai/savvina/commit/9732d5779c2278743def078cfe939dad3c711df4))

- **semantic**: Stop new business metrics 422ing the model save. Seeds the metric_type discriminator
  and aggregation the backend's union requires, so adding a metric no longer rejects the whole PUT.
  Also syncs the frontend semantic types with models.py — surfacing segments, notes, table
  grain/domain and the column intelligence fields — and closes the feedback loop by sending
  semantic_correction on thumbs-down into a new Suggestions tab.
  ([#13](https://github.com/savvina-ai/savvina/pull/13),
  [`9732d57`](https://github.com/savvina-ai/savvina/commit/9732d5779c2278743def078cfe939dad3c711df4))


## v1.0.5 (2026-08-12)

### Bug Fixes

- Make ChartView containerRef optional and remove nullRef workaround
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

- Report the real version, and stop the frontend suite losing timing races
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

- **deps**: Align Node version across compose, drop no-op esbuild dep, refresh architecture docs
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

- **deps**: Patch cryptography, react-router, and js-yaml vulnerabilities
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

### Chores

- Fix cla assistant action ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

### Documentation

- Correct two inaccurate claims in the Docker infrastructure guide
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))

- Document every endpoint, TRUSTED_PROXIES, and the frontend test suite
  ([#11](https://github.com/savvina-ai/savvina/pull/11),
  [`63283fa`](https://github.com/savvina-ai/savvina/commit/63283fa8b06c93313c16ae793c46eb7791de310c))


## v1.0.4 (2026-07-22)

### Bug Fixes

- Unblock first-run setup and derive DATABASE_URL from APP_DB_PASSWORD
  ([#9](https://github.com/savvina-ai/savvina/pull/9),
  [`7020ca9`](https://github.com/savvina-ai/savvina/commit/7020ca95061f752abfda889e690de478c31401e3))


## v1.0.3 (2026-07-22)

### Bug Fixes

- **deps**: Patch axios, brace-expansion, and json-repair vulnerabilities
  ([#8](https://github.com/savvina-ai/savvina/pull/8),
  [`8606d51`](https://github.com/savvina-ai/savvina/commit/8606d51724e4310a3e4b0fa54d601869fecd2ece))

### Documentation

- Polish README — tagline, badges
  ([`e680bc2`](https://github.com/savvina-ai/savvina/commit/e680bc2ce9595adc4e9d741ffb7fcb85df5e4030))


## v1.0.2 (2026-06-16)

### Bug Fixes

- Close concurrent-batch-generation races, patch form-data, js-yaml, ws vulnerabilities, patch
  starlette, cryptography, python-multipart vulnerabilities
  ([`c26e99d`](https://github.com/savvina-ai/savvina/commit/c26e99d7da8b07745dfda403d199947d21f97a08))

- **deps**: Patch form-data, js-yaml, ws vulnerabilities
  ([`c26e99d`](https://github.com/savvina-ai/savvina/commit/c26e99d7da8b07745dfda403d199947d21f97a08))

- **deps**: Patch starlette, cryptography, python-multipart vulnerabilities
  ([`c26e99d`](https://github.com/savvina-ai/savvina/commit/c26e99d7da8b07745dfda403d199947d21f97a08))

- **semantic**: Close concurrent-batch-generation races
  ([`c26e99d`](https://github.com/savvina-ai/savvina/commit/c26e99d7da8b07745dfda403d199947d21f97a08))


## v1.0.1 (2026-06-15)

### Bug Fixes

- Build and dependency improvements
  ([`8355f18`](https://github.com/savvina-ai/savvina/commit/8355f18f9ce981ad711fa247415383a5402a4a65))

- Vite and react vulnerabilities ([#4](https://github.com/savvina-ai/savvina/pull/4),
  [`1b51042`](https://github.com/savvina-ai/savvina/commit/1b510429e45818aa051457061bd75009d1c9f19f))

### Chores

- Add CLA assistant workflow with dedicated signatures branch
  ([`13f254f`](https://github.com/savvina-ai/savvina/commit/13f254f51af4bf055be5ff006e8fec7d47962a13))

- Remove CLA action, using cla-assistant.io instead
  ([`bd5490e`](https://github.com/savvina-ai/savvina/commit/bd5490e5d531f24827678958fa67c6ca1e9e2b03))

- Update allowlist for cla workflow
  ([`7001c6f`](https://github.com/savvina-ai/savvina/commit/7001c6f6ac897563a6f3366289a8505f55b86885))

### Continuous Integration

- Use PAT for semantic-release to bypass branch protection
  ([`5d2eef3`](https://github.com/savvina-ai/savvina/commit/5d2eef3a4ed3b076b7197d79153d930d8bfc6a39))

- Use PAT for semantic-release to bypass branch protection
  ([`d6bb6b6`](https://github.com/savvina-ai/savvina/commit/d6bb6b618ef8b582fe251caa443156ad486a1b12))

### Performance Improvements

- Added hf_token and auto generation of keys for docker setup acceleration
  ([#4](https://github.com/savvina-ai/savvina/pull/4),
  [`1b51042`](https://github.com/savvina-ai/savvina/commit/1b510429e45818aa051457061bd75009d1c9f19f))


## v1.0.0 (2026-06-02)

- Initial Release

## v1.0.0 (2026-06-01)

- Initial Release
