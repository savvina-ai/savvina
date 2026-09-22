# CHANGELOG

<!-- version list -->

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
