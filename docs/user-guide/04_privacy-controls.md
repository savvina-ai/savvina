# Privacy Controls

Savvina AI is designed on a **privacy-by-design** principle. **A value read from your database reaches the LLM through one channel, and it is off until an administrator turns it on for the connection:** *Include sample values*. Query results, the conversation history and database error messages never carry one. The one other place a stored value can appear is text you keep in the semantic model, such as its value mappings, which are described [below](#semantic-value-mappings). [Every LLM call and what it sends](#every-llm-call-and-what-it-sends) lists each call in full.

---

## What the LLM Receives

The system prompt always includes the schema DDL and, if one has been generated, the semantic model. Items 2 to 4 are each controlled by their own toggle:

1. **Schema DDL** — `CREATE TABLE` statements listing table names, column names, data types, primary keys and foreign-key relationships
2. **Sample values** — up to 3 example values per column, only when **Include sample values** is on (**off by default**). On PostgreSQL they come from the catalog's most-common-values statistics (`pg_stats`); on MySQL/MariaDB only ENUM and SET columns have them, taken from the column's declared members
3. **Column comments** — PostgreSQL `COMMENT ON` text and MySQL column comments, only when **Include column comments** is on (**off by default**)
4. **Approximate row counts** — from the catalog, only when **Include row counts** is on
5. **Semantic model** — your business glossary, if one has been generated, including its value mappings
6. **Few-shot examples** — question and SQL pairs from the Example Library (answers you marked 👍) that are similar to the current question

Each call also carries your question and the earlier turns of the conversation. The earlier turns hold questions, generated SQL and explanations, never result rows.

**With Include sample values off, which is the default, no value is read from your tables for the LLM.** A connection whose stored settings do not mention this toggle, including one created through the API without privacy settings, reads as off.

### Semantic value mappings

A semantic model column can carry value mappings: a display label and the stored value it stands for (`Active='A'`). The query-generation prompt includes them on every question, as `Label='raw'` followed by `[SQL values: …]`, so the LLM writes the exact stored value in a `WHERE` clause.

**Value mappings are not controlled by Include sample values, and the sensitive column patterns do not apply to them.** They are sent whatever the toggle says and whatever the column is called. They are left out only for a column marked **Sensitive** in the semantic model, and for a column or table hidden by the excluded schemas, tables or columns lists.

Value mappings are semantic metadata that you curate. Semantic model generation proposes some, from the schema the LLM was shown (including an ENUM's members on MySQL when sample values are allowed for that column), and you can edit or delete any of them in the semantic model editor. If a column's real values must not reach the LLM, remove its value mappings or mark the column **Sensitive** in the semantic model.

### Every LLM call and what it sends

| Call | When it happens | What it sends, beyond the prompt above | How to turn it off |
|---|---|---|---|
| **Query generation** | Every question not answered from the cache | Nothing more | Cannot be turned off; it is the product |
| **Corrections** | The generated SQL fails validation, fails to execute, or returns no rows | The failed SQL. For a validation failure, the validator's message, which is built from your query and your schema. For a query that returned no rows, only that fact. For a database error, **the database's own message is never sent**, because it can quote a stored value (`invalid input syntax for type integer: "Jane Doe"`). Savvina sends its own description instead: the kind of error (unknown name, syntax error, a stored value that could not be converted, …) and a fixed hint. For an unknown or ambiguous name, a grouping error, a type mismatch, a permission error or a syntax error, it also lists the names the error refers to, but only names the LLM wrote in its query or, for an unknown or ambiguous name, was shown in the schema. Every other kind of error lists no names | Automatic. It runs at most a few times per question |
| **Semantic model generation** | When an admin generates or regenerates a connection's semantic model, or applies schema drift | Schema DDL (names, types, keys, and column comments collected while **Include column comments** is on) and foreign-key relationships. Later passes also send the annotations already generated. No column values, except that a MySQL ENUM/SET column's declared members appear in its type when its values are allowed (sample values on and the column not sensitive). Otherwise the member list is withheld | Do not run generation, or edit the model by hand |

Entity resolution (matching a quoted name in your question against known values) and schema pruning run locally and make no LLM call of their own. Their output is part of the query-generation prompt and follows the same toggle.

**Keeping everything on your own hardware:** point Savvina at a local model through Ollama (see [LLM Providers](06_llm-providers.md)). Every call above then stays inside your deployment. The web UI itself makes no request to any third party: fonts and scripts are served by your own instance.

---

## Per-Connection Privacy Settings

Each connection has independent privacy settings. Configure them at **Connections → [your connection] → Privacy Settings**.

### Toggle Controls

| Setting | Default | Effect |
|---|---|---|
| **Include sample values** | Off | When off, no sample value reaches the LLM by any route: not in the schema, not as an ENUM member list, not as a filter hint. This reduces accuracy for enum-like columns. When on, the system also runs **Named Entity Resolution**: quoted strings and capitalised proper nouns in your question are matched against column sample values, and the stored spelling is added as a filter hint (e.g. resolving `"Acme Corp"` → `'acme corp'`). Sensitive columns, excluded columns and excluded tables are never matched |
| **Include column comments** | Off | When off, column and table comments are omitted from the LLM prompt |
| **Include row counts** | On (a connection created in the UI starts with it off) | When off, approximate row counts are omitted from the LLM prompt |

### Sensitive Column Patterns

A list of case-insensitive patterns. Any column whose name **contains** a matching pattern is annotated `[SENSITIVE]` in the LLM schema. Its sample values are withheld even when the toggle is on, and its values are masked in results.

**The column itself is not hidden, and the query is not restricted.** The name and type stay visible so the LLM can use the column in `WHERE`, a join or an aggregate. For example, "all orders for the customer with email `someone@example.com`" filters on the column and returns rows normally.

**Result masking:** when a sensitive column's value appears in a query's output, it is replaced with `[REDACTED]` before the response reaches your browser. This covers the streamed rows and the saved message. Stored results are masked again with the connection's current settings every time they are read: in the chat history, through a share link, and in CSV, Excel and PDF exports. Tightening the settings therefore also applies to results saved earlier.

Masking follows what the SELECT list exposes, not what the column is called:

- `SELECT salary FROM employees` → masked
- `SELECT salary AS comp FROM employees` → `comp` masked (the alias is tracked)
- `SELECT lower(email) AS contact FROM users` → `contact` masked (an expression over a sensitive column)
- `SELECT lower(email) FROM users` → masked by position, whatever name the database gives the column
- `SELECT * FROM employees` → the `salary` column in the returned rows is masked (matched on the returned column name)
- `SELECT "salary"` on PostgreSQL, `` SELECT `salary` `` on MySQL → masked exactly as the bare form
- `SELECT AVG(salary) …`, `SELECT COUNT(email) …` → **not** masked. A statistical aggregate returns a statistic, not a stored value. Only these are exempt: `COUNT`, `SUM`, `AVG`, `STDDEV`/`VARIANCE` and their variants, `CORR`/`COVAR_*`/`REGR_*`, `PERCENTILE_CONT`, `BOOL_AND`/`BOOL_OR` and `EVERY`
- `SELECT MAX(salary) …`, `string_agg(email, ',')`, `array_agg(…)`, `group_concat(…)` → masked. These aggregates return stored values (as do `MIN`, `LISTAGG`, `PERCENTILE_DISC`, `MEDIAN`, `MODE`, `BIT_AND`/`BIT_OR` and the JSON aggregates)
- `SELECT RANK() OVER (ORDER BY salary) …` → not masked. A ranking window function (`RANK`, `DENSE_RANK`, `ROW_NUMBER`, `NTILE`, `PERCENT_RANK`, `CUME_DIST`) returns a position, not the value
- `SELECT SUM(salary) OVER (…)` → masked. An aggregate with `OVER` is a window function, and a running total or a one-row partition returns single values
- `SELECT name FROM employees WHERE salary > 50000` → nothing masked, because the column is only a filter

Masking follows the column through the query's structure. A sensitive column renamed inside a CTE, a derived table (including a column alias list such as `AS t(a, b)`) or a nested or scalar subquery is still masked: `WITH x AS (SELECT email AS c FROM users) SELECT c FROM x` masks `c`. In a `UNION`/`INTERSECT`/`EXCEPT`, a position is masked when **any** branch puts a sensitive column there. Matching on the returned column name applies as well, except to an output the list above marks as not masked, such as `COUNT(email) AS email`, which is a count and not an address.

Where the structure cannot be followed, Savvina falls back in steps. A `SELECT *` or `alias.*` over a real table is matched on the returned column names. If the query has a shape the classifier cannot parse, the outermost SELECT list is still checked for sensitive columns. If the query cannot be parsed at all and its text names a sensitive column anywhere, every column of the result is masked.

The LLM is also told not to select sensitive columns at all. Masking is the second line of defence.

**Known limits.** A statistical aggregate over a single row is that row's value. `SUM(salary) … GROUP BY employee_id` (or a grouping on any other unique key) and `SUM(salary) FILTER (WHERE id = 1)` therefore return individual salaries unmasked. Keys inside a JSON value (`data->>'email'`) are not recognised as columns. If either matters for a column, exclude the column instead of relying on the pattern.

On MySQL/MariaDB `"…"` is a string literal, so `IF(kind = "salary", amount, 0)` does not trigger masking there. On PostgreSQL a double-quoted token is read as a column reference, which errs toward masking.

If you need a column gone entirely, invisible to the LLM and unusable in any clause, use **Excluded columns** instead.

**Default patterns:**
```
email, ssn, social_security, password, passwd, secret,
token, api_key, credit_card, card_number, cvv,
phone, mobile, address, salary, wage, income,
bank_account, routing_number, dob, date_of_birth,
national_id, passport, license_number, tax_id
```

You can add custom patterns. For example, to treat any column containing `internal` as sensitive, add `internal` to the list.

### Excluded Schemas

Schema names to **completely hide** from the LLM. Tables, columns and row counts in these schemas are not included in any prompt. Useful for internal audit schemas (`pg_audit`, `logs`, …). One name per line:
```
pg_audit
internal
staging
```

### Excluded Tables

Tables to hide from the LLM entirely, as `table_name` (any schema) or `schema.table_name` (exact):
```
users
public.audit_log
internal.raw_events
```

### Excluded Columns

Individual columns to hide, as `schema.table.column`. The column is omitted from the `CREATE TABLE` DDL. As a backstop, its values are masked if a query returns it anyway (a `SELECT *`, for example):
```
public.customers.email
public.users.password_hash
public.employees.salary
```

### Row Filter

An optional SQL condition (`row_filter_sql`, e.g. `tenant_id = 42`) that every statement sent to the database is wrapped in:

```sql
SELECT * FROM (
<the query>
) AS _sift_q WHERE tenant_id = 42
```

This applies to the first execution, every self-corrected retry, the re-run after a query returns no rows, approved queries, edited queries, re-sorts and answers served from the cache. A trailing row limit is moved outside the wrap, so the filter applies before rows are cut: `LIMIT n`, `LIMIT n OFFSET m`, and on PostgreSQL `OFFSET m LIMIT n`, on MySQL `LIMIT m, n`. The SQL shown to you, stored with the message, kept as a verified example and sent to the LLM is the query without the wrap.

**Setting it.** Use the **Row filter** field in the Privacy Settings form, when creating a connection or editing one, or send `row_filter_sql` to `POST /api/v1/connections` or `PUT /api/v1/connections/{id}/privacy`. Empty the field, or send `""`, to remove the filter; omitting the field or sending `null` on `PUT` leaves it unchanged. **Reset to defaults** in the form leaves the row filter as it is. Changing the filter invalidates the connection's query cache.

**Validation.** The filter is checked when it is saved and rejected (HTTP 422, with the reason) when it contains `;`, a comment (`--`, `/*`, or `#` on MySQL), a `$`, starts with the word `WHERE`, has parentheses that do not balance as the connection's engine reads them, contains a string the check cannot read safely (a backslash or an unterminated quote), or is longer than 1000 characters. The check is textual: a `;` or comment marker inside a string literal is rejected too. A wrong column name is not caught here; it fails at the database on the first query.

**Known limit:** the filter is applied to the query's **output**, so the query must return every column the filter names. A query that does not, such as `SELECT COUNT(*) …`, most aggregates, or on MySQL a join whose output has two columns with the same name, fails with a database error instead of running unfiltered. This is deliberate: the filter fails closed.

A query that the wrap cannot safely enclose is rejected before it is sent: one whose parentheses do not balance, one with a backslash inside a string literal, a MySQL executable comment (`/*! … */`), an unterminated string or comment, a PostgreSQL dollar-quote tag with a non-ASCII character (`$café$…$café$`), or on MySQL two `$` signs with only letters, digits or `_` between them outside a string, comment or backtick-quoted name (`$x$`, `$$`, also an identifier such as `a$b$c`).

---

## Sensitive Column Auto-Detection

Pattern matching is applied **automatically** at schema introspection time. If your table has a column `customer_email_address`, it matches `email`. The column appears in the schema as `[SENSITIVE]`, no sample values are sent for it, and its result values are masked, without you listing it anywhere.

---

## What Happens During Schema Introspection

When a schema is introspected (on first chat, after **Refresh Schema**, or on the first question after the privacy settings are saved), the adapter:

1. Fetches all schemas, tables, columns, primary keys and foreign keys
2. Applies `excluded_schemas` and skips these schemas entirely
3. Applies `excluded_tables` and skips these tables
4. For each column:
   - If `include_column_comments` is off, skips comments
   - If the column is in `excluded_columns`, removes it entirely
   - If the column matches a sensitive pattern, keeps it but collects no values for it (on MySQL, its ENUM/SET member list is withheld from the type too)
   - Otherwise, if `include_sample_values` is on, attaches example values. PostgreSQL reads the catalog's most-common values (`pg_stats`). MySQL reads an ENUM/SET column's declared members. No table is scanned

Excluded schemas, tables and columns, and the withholding of sample values and ENUM/SET member lists, are applied again every time a cached schema is read. A schema cached under older settings therefore never shows the LLM more than the current settings allow.

---

## Schema Format Sent to the LLM

```sql
-- Schema: public
-- Table: public.customers (≈12,450 rows)
CREATE TABLE public.customers (
    id uuid NOT NULL  -- Primary Key,
    first_name text  -- e.g. 'Alice', 'Bob', 'Carol',
    email text  -- [SENSITIVE],
    status text  -- e.g. 'active', 'suspended', 'cancelled',
    created_at timestamp without time zone
);
```

Sample values appear only when **Include sample values** is on, and the row count only when **Include row counts** is on. A foreign key appears as an annotation on its column (`-- FK: customer_id -> public.customers.id`). `email` shows `[SENSITIVE]` and never carries values. Excluded tables and columns are absent. On MySQL, identifiers are backtick-quoted and the section label is `Database`.

---

## Read-Only Execution

Independently of privacy settings, every statement is validated as a single read-only `SELECT` (or `WITH … SELECT`) **and** executed inside a database-enforced read-only transaction. On PostgreSQL the statement runs in a `READ ONLY` transaction. On MySQL/MariaDB the session is set to `SET SESSION TRANSACTION READ ONLY` for the statement and restored afterwards. A write that the text check misses, such as `nextval()`, is refused by the engine itself. Connect with a read-only role as well, for defence in depth; see [Connecting to Data](02_connecting-to-data.md#using-a-read-only-database-role).

---

## Error Messages You See

When a query fails at the database, the error shown in the chat and stored with the message depends on the kind of error. For an unknown or ambiguous name, a grouping error, a type mismatch, a permission error or a syntax error, you see the database's message as is: these messages quote names from the query, not stored values. For any other error, such as a value that could not be converted, you see only the first line of the message, with every quoted part replaced by `[REDACTED]`. The full text is written to the server log only.

PostgreSQL also reports a stored value that a query looked up or parsed under these name and syntax codes: `SELECT ssn::regclass …` fails with `relation "123456789" does not exist`. So on PostgreSQL such an error counts as a name or syntax error only when the database points to a place in the query text. An error without that position, or one raised while the database parsed a stored value (as for `::regtype`), is treated as a value error and redacted. Permission errors usually carry no position and always keep their text.

**Known limit:** only quoted values are redacted. A PostgreSQL function defined in your database that `RAISE`s an error with a value written into its message unquoted can show that value in the error.

**Upgrading:** messages saved before this release may still show the database's raw error text in the session history. Their results are re-masked every time they are read; their error text is not.

---

## Updating Privacy Settings

**UI:** Connections → [your connection] → Privacy Settings → Save

**API:**
```bash
PUT /api/v1/connections/{id}/privacy
Content-Type: application/json

{
  "include_sample_values": false,
  "excluded_tables": ["audit_log", "sessions"],
  "sensitive_column_patterns": ["email", "ssn", "token", "internal_code"]
}
```

Saving privacy settings clears the connection's cached schema, table embeddings and cached query answers. The next question introspects the schema again under the new settings, so a newly enabled toggle takes effect without a manual **Refresh Schema**.

---

## Recommendations by Use Case

| Use Case | Recommended Settings |
|---|---|
| Development DB with synthetic data | Turn sample values on; default patterns |
| Production DB with real PII | Leave sample values off; add PII tables to the excluded list; review the semantic model's value mappings |
| Financial / HR data | Disable row counts; exclude salary/wage/income columns explicitly |
| Maximum privacy (regulatory) | All three toggles off; use excluded schemas to limit scope; remove value mappings you do not want sent; run a local model via Ollama |
| Internal analytics (known safe) | Sample values on; generate a semantic model for accuracy |
