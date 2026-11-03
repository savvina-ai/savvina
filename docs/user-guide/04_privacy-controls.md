# Privacy Controls

Savvina AI is designed on a **privacy-by-design** principle. **A value read from your database reaches the LLM through one channel, and it is off until an administrator turns it on for the connection:** *Include sample values*. Query results, the conversation history and database error messages never carry one. The one other place a stored value can appear is text you keep in the semantic model, such as its value mappings, which are described [below](#semantic-value-mappings). [Every LLM call and what it sends](#every-llm-call-and-what-it-sends) lists each call in full.

---

## What the LLM Receives

The system prompt always includes the schema DDL and, if one has been generated, the semantic model. Items 2 to 4 are each controlled by their own toggle:

1. **Schema DDL** — `CREATE TABLE` statements listing table names, column names, data types, primary keys and foreign-key relationships
2. **Sample values** — up to 3 example values per column, only when **Include sample values** is on (**off by default**). On PostgreSQL they come from the catalog's most-common-values statistics (`pg_stats`); on MySQL/MariaDB only ENUM and SET columns have them, taken from the column's declared members. A table under a row filter sends no sample values (a MySQL ENUM/SET column keeps its declared members; see [Row Filters](#row-filters)).
3. **Column comments** — PostgreSQL `COMMENT ON` text and MySQL column comments, only when **Include column comments** is on (**off by default**). A sensitive column's comment is never sent, because a comment can spell out the column's values
4. **Approximate row counts** — from the catalog, only when **Include row counts** is on. A table under a row filter sends no row count (see [Row Filters](#row-filters)).
5. **Semantic model** — your business glossary, if one has been generated, including its value mappings
6. **Few-shot examples** — question and SQL pairs from the Example Library (answers you marked 👍) that are similar to the current question

Each call also carries your question and the earlier turns of the conversation. The earlier turns hold questions, generated SQL and explanations, never result rows.

**With Include sample values off, which is the default, no value is read from your tables for the LLM.** A connection whose stored settings do not mention this toggle, including one created through the API without privacy settings, reads as off.

### Semantic value mappings

A semantic model column can carry value mappings: a display label and the stored value it stands for (`Active='A'`). The query-generation prompt includes them on every question, as `Label='raw'` followed by `[SQL values: …]`, so the LLM writes the exact stored value in a `WHERE` clause.

**Value mappings are not controlled by Include sample values, and the sensitive column patterns do not apply to them.** They are sent whatever the toggle says and whatever the column is called. They are left out only for a column marked **Sensitive** in the semantic model, and for a column or table hidden by the excluded schemas, tables or columns lists.

Value mappings are semantic metadata that you curate. Semantic model generation keeps the mappings it proposes only for a column whose values may reach the LLM: **Include sample values** on, and no sensitive pattern matched. For any other column, a proposed mapping is discarded before it is saved; a regenerate also clears the generated mappings such a column got from an earlier run, for each column the regenerate annotates, while mappings you edited are kept. Where the gate is open, a generated mapping is not proof the LLM saw the column's values: generation sees values only as the declared members of a MySQL ENUM or SET column, so elsewhere a mapping may be inferred from the column's name or comment. Check generated mappings against the data. You can edit or delete any mapping in the semantic model editor. If a column's real values must not reach the LLM, remove its value mappings or mark the column **Sensitive** in the semantic model.

A hidden table or column is removed from what the model receives, including metrics, segments, derived columns, joins, default filters and notes that name it. The descriptions of the tables and columns you can see are not scanned for hidden names: a description you wrote by hand that names a hidden column reaches the prompt as written.

### Every LLM call and what it sends

| Call | When it happens | What it sends, beyond the prompt above | How to turn it off |
|---|---|---|---|
| **Query generation** | Every question not answered from the cache | Nothing more | Cannot be turned off; it is the product |
| **Corrections** | The generated SQL fails validation, fails to execute, or returns no rows | The failed SQL. For a validation failure, the validator's message, which is built from your query and your schema. For a query that returned no rows, only that fact. For a database error, **the database's own message is never sent**, because it can quote a stored value (`invalid input syntax for type integer: "Jane Doe"`). Savvina sends its own description instead: the kind of error (unknown name, syntax error, a stored value that could not be converted, …) and a fixed hint. For an unknown or ambiguous name, a grouping error, a type mismatch or a syntax error, it also lists the names the error refers to, but only names the LLM wrote in its query or, for an unknown or ambiguous name, was shown in the schema. Every other kind of error lists no names. Savvina's own refusals (a row filter that cannot be applied, the strict sensitive-column rule, a double quote on a MySQL server with `ANSI_QUOTES`, a backslash on a MySQL server with `NO_BACKSLASH_ESCAPES`, and there, with row filters on, a condition holding a backslash, tab or line break, or a control character inside a string of the query) are the exception: they are passed to the model in their own words, with their hint, since they name only columns and tables from your query or settings. A refusal the model cannot fix by rewriting the query (a row filter that needs migration or is malformed) ends the correction at once: the model is not called and you see the refusal. So do a permission error and a connection lost while the query ran | Automatic. It runs at most a few times per question |
| **Semantic model generation** | When an admin generates or regenerates a connection's semantic model, or applies schema drift | Schema DDL (names, types, keys, and column comments collected while **Include column comments** is on, except a sensitive column's) and foreign-key relationships. Later passes also send the annotations already generated. No column values, except that a MySQL ENUM/SET column's declared members appear in its type when its values are allowed (sample values on and the column not sensitive). Otherwise the member list is withheld | Do not run generation, or edit the model by hand |

Entity resolution (matching a quoted name in your question against known values) and schema pruning run locally and make no LLM call of their own. Their output is part of the query-generation prompt and follows the same toggle.

**Keeping everything on your own hardware:** point Savvina at a local model through Ollama (see [LLM Providers](06_llm-providers.md)). Every call above then stays inside your deployment. The web UI itself makes no request to any third party: fonts and scripts are served by your own instance.

---

## Per-Connection Privacy Settings

Each connection has independent privacy settings. Configure them at **Connections → [your connection] → Privacy Settings**.

### Toggle Controls

| Setting | Default | Effect |
|---|---|---|
| **Include sample values** | Off | When off, no sample value reaches the LLM by any route: not in the schema, not as an ENUM member list, not as a filter hint. This reduces accuracy for enum-like columns. When on, the system also runs **Named Entity Resolution**: quoted strings and capitalised proper nouns in your question are matched against column sample values, and the stored spelling is added as a filter hint (e.g. resolving `"Acme Corp"` → `'acme corp'`). Sensitive columns, excluded columns and excluded tables are never matched. A table under a row filter sends no sample values (a MySQL ENUM/SET column keeps its declared members; see [Row Filters](#row-filters)). |
| **Include column comments** | Off | When off, column and table comments are omitted from the LLM prompt. When on, a sensitive column's comment is still omitted |
| **Include row counts** | On (a connection created in the UI starts with it off) | When off, approximate row counts are omitted from the LLM prompt. A table under a row filter sends no row count (see [Row Filters](#row-filters)). |
| **Strict sensitive columns** | Off | When on, a query that uses a sensitive column anywhere but as a plain column of its SELECT list is refused before it runs (see [Strict Sensitive Columns](#strict-sensitive-columns)) |

### Sensitive Column Patterns

A list of case-insensitive patterns. Any column whose name **contains** a matching pattern is annotated `[SENSITIVE]` in the LLM schema. Its sample values and its comment are withheld even when the toggles are on, and its values are masked in results.

**The column itself is not hidden, and the query is not restricted** (unless **Strict sensitive columns** is on; see [below](#strict-sensitive-columns)). The name and type stay visible so the LLM can use the column in `WHERE`, a join or an aggregate. For example, "all orders for the customer with email `someone@example.com`" filters on the column and returns rows normally.

A whole row read through one call is masked as a whole: `row_to_json(u)`, `to_jsonb(u)`, and the same functions written as an attribute of the row (`u.to_jsonb`, `u.row_to_json`, `u.concat`, `u.json_agg`), which PostgreSQL reads as `f(u)`. So is a bare row reference (`SELECT u FROM users u`), also from inside a correlated subquery or a `LATERAL` subquery (`SELECT (SELECT to_jsonb(u) FROM orders o LIMIT 1) FROM users u`), on a schema-qualified table (`to_jsonb(users) … FROM public.users`) and under an alias that is also an SQL keyword (`FROM users data`). The result column shows `[REDACTED]`. On MySQL a qualified column with one of these names (`u.concat`) is masked too, since the check does not depend on the dialect. On PostgreSQL a column named like the unaliased table it comes from (`SELECT country FROM country`) is masked as well, since without the table's columns it cannot be told from the whole row; give the table an alias (`SELECT c.country FROM country c`) to avoid it.

**Result masking:** when a sensitive column's value appears in a query's output, it is replaced with `[REDACTED]` before the response reaches your browser. This covers the streamed rows and the saved message. Stored results are masked again with the connection's current settings every time they are read: in the chat history, through a share link, and in CSV, Excel and PDF exports. Tightening the settings therefore also applies to results saved earlier. Changing row filters goes further: earlier results are withheld entirely (see [Row Filters](#row-filters)).

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

This holds through `WITH … AS [NOT] MATERIALIZED`, `TABLESAMPLE`, `WITH ORDINALITY`, MySQL index hints (`USE INDEX (…)`) and `PARTITION (…)`, `IS [NOT] DISTINCT FROM` in the SELECT list, a column alias that is an SQL keyword (`email AS from`, `s.from`), a parenthesised join group (`a JOIN (b JOIN c ON …) ON …`), a quoted name written with Unicode escapes (`U&"em\0061il"`) and a table function or `VALUES` list: one whose arguments hold a sensitive column or a whole row (`unnest(emails)`, `jsonb_each_text(to_jsonb(u))`, `LATERAL (VALUES (u::text)) v(r)`) has every column it returns masked, and since its column names are not known, so is every column the query reads without a table qualifier (`SELECT id, value FROM users u, jsonb_each_text(to_jsonb(u))` masks both; `SELECT u.id, value …` keeps `id`).

Where the structure cannot be followed, Savvina falls back in steps. A `SELECT *` or `alias.*` over a real table is matched on the returned column names. If the query has a shape the classifier cannot follow (for example a join group under an alias, `FOR UPDATE`) or cannot be parsed at all, and its text names a sensitive column anywhere, calls one of the row functions above, renames columns by position (a column alias list such as `t(a, b)`, or a `TABLE x` statement), uses `alias.*` or `*` anywhere but as a whole item of the outermost SELECT list, `COUNT(*)` or an `EXISTS`/`IN` subquery, or, on PostgreSQL, uses a bare name that is also the name or alias of a table in the query in a SELECT list or a table function's arguments (a possible whole row: `SELECT u FROM users u …`, `ROW(u.*)`, `(u.*)::text`, `unnest(ARRAY[u::text])`), every column of the result is masked. So is every column when classification fails for any other reason, when the query is too complex to classify in bounded time (deeply nested recursive CTEs, for example), and when it contains a MySQL executable comment. Every column is also masked when the classifier's reading of the SELECT list does not have as many columns as the result and the same test fires; this happens with `SELECT *` over a `USING` or `NATURAL` join of CTEs or derived tables (the join column appears once), so list the columns there.

The LLM is also told not to select sensitive columns at all. Masking is the second line of defence.

**Known limits.** A statistical aggregate over a single row is that row's value. `SUM(salary) … GROUP BY employee_id` (or a grouping on any other unique key) and `SUM(salary) FILTER (WHERE id = 1)` therefore return individual salaries unmasked. Keys inside a JSON value (`data->>'email'`) are not recognised as columns. If either matters for a column, exclude the column instead of relying on the pattern.

On MySQL/MariaDB `"…"` is a string literal, so `IF(kind = "salary", amount, 0)` does not trigger masking there. If the server's `sql_mode` includes `ANSI_QUOTES` (also set by `ANSI`), Savvina reads the mode when it connects, before it checks the query, on every path that runs one (the chat, approving a query, editing and running it, re-sorting a result), and treats `"…"` as a column reference, as on PostgreSQL. On such a server, with **Strict sensitive columns** or a row filter on, a query that contains a double quote is refused before it runs (*The database reads double quotes as names (ANSI_QUOTES) …*): the checks read the query with a parser that takes `"…"` for a string, so they cannot be trusted on it; write names with backticks. Results already stored in the history are re-masked with MySQL's default reading, since no connection is open when they are read.

If the server's `sql_mode` includes `NO_BACKSLASH_ESCAPES`, the server reads a backslash in a string as an ordinary character, while result masking and the checks read it as an escape (`'a\'` goes on past the second quote for them and ends there for the server), so they would disagree with the server about where a string ends. Savvina reads this mode at the same point, and on such a server every query that contains a backslash anywhere is refused before it runs, whatever the privacy settings (*The database reads a backslash in a string as an ordinary character (NO_BACKSLASH_ESCAPES), which the privacy checks cannot follow, so the query was not run.*); the model is told not to use a backslash. A query cannot change either mode: a statement that starts with `SET` is refused (also when the `SET` comes after a comment or sits inside a MySQL executable comment `/*! … */`), and MariaDB's `SET STATEMENT … FOR …` and MySQL's `SET_VAR` optimizer hint are refused anywhere in the query text, including inside a comment or with comments between the words. With row filters on, two more refusals apply on such a server: a map in which any condition contains a backslash, tab, carriage return or line break is refused at save time (`row_filters["schema.table"]: the condition contains a backslash, tab, carriage return or line break, …`) and, if it was saved before the server's mode changed, every query on the connection is refused until the condition is changed (*The row filter for schema.table contains a backslash, tab, carriage return or line break, which the database reads differently (NO_BACKSLASH_ESCAPES) from the way the filter is applied, so the query was not run. Change the condition.*); and a query with a tab, line break or other control character inside a string literal is refused, since the rewrite would write it as a backslash escape the server reads as two characters; the model is told to keep those out of string literals.

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

You can also remove default patterns. The privacy form then lists the removed defaults under the patterns: a column that only they matched loses the `[SENSITIVE]` tag, so its values are no longer masked and its sample values are sent while **Include sample values** is on. **Reset to defaults** restores them.

Masking follows a sensitive column through aliases, expressions, derived tables, CTEs, set operations and whole-row reads by built-in functions (`to_jsonb(u)`, `row_to_json(u)`). A function you define in your own database that takes a whole row (`u.my_func` or `my_func(u)`) is not recognised as a whole-row read by masking or by **Strict sensitive columns**, so its output is not masked: if your database has such a function over a table with sensitive columns, exclude those columns.

### Sensitive Columns

A list of column names matched exactly, case-insensitively, in every table that has them. Use it for names too common for a pattern: `name` as a pattern would also match `username` and `first_name`; as a sensitive column it matches only a column called `name`. The effect is the same as a pattern match: `[SENSITIVE]` in the schema, no sample values, masked results, re-masked stored results. Entries are column names only; `table.column` is not recognised. An entry such as `customers.notes` is refused, in the form and by the API (422), so list the bare name. A connection saved before this check may still hold such an entry: it is ignored (it never matched), does not block saving, and can be removed from the list.

### Strict Sensitive Columns

When this toggle is on, a sensitive column (one that matches a pattern, one listed under **Sensitive columns**, or the bare name of an excluded column) may appear in a query only as a plain column of a SELECT list, with or without an alias. `SELECT email FROM users`, `SELECT email AS contact FROM users`, `SELECT * FROM users`, `SELECT u.* FROM users u` and `SELECT count(*) FROM users` run as usual, and masking covers their output. `SELECT DISTINCT email FROM users` does not run: see *The column's values are compared* below.

The check runs on every query the model writes or you edit, before it is sent to the database: the first execution, self-corrected retries, the re-run after a query returns no rows, approved and edited queries, and re-sorts. A query it refuses is never sent. When the refusal ends the request, you see one of the messages below; during an automatic correction the model gets the message and may fix the query, and a refused re-run after an empty result keeps the empty result.

Few-shot examples from the Example Library are checked too: an example that strict mode would refuse (one you marked 👍 before the toggle was turned on, say) is left out of the prompt, so the model is not shown a query the connection refuses. Cached queries are checked the same way: a cached answer that strict mode would refuse is not reused, and the model writes a new query instead.

**The column is used outside the SELECT list.** The message names the column: *Strict sensitive columns: the query uses the column email outside its SELECT list (in a condition, join, grouping, ordering, expression or under another name). This connection does not allow that.* It is shown for:

- an expression over the column, in the SELECT list too: `lower(email)`, `count(email)`, a `CASE` that reads it;
- a `WHERE`, `JOIN`, `GROUP BY`, `HAVING` or `ORDER BY` on it;
- an alias of it used in any of those places (`SELECT email AS e … WHERE e = …`), also when the alias is given inside a CTE or a subquery; the message then names the alias;
- a position that points at it (`ORDER BY 2` when the second SELECT item is `email`, `GROUP BY 2`, `DISTINCT ON (2)`, also in parentheses (`ORDER BY (2)`) and inside `ROLLUP`, `CUBE` or `GROUPING SETS` (`GROUP BY ROLLUP (1)`), nested or in a parenthesised list (`GROUP BY (1, 2)`); on a `UNION`, `INTERSECT` or `EXCEPT` the position is checked in every branch; an `ORDER BY` written after a parenthesised query, `(SELECT id, email FROM users) ORDER BY 2`, is checked against that query, inside `EXISTS ((SELECT id, email FROM users) ORDER BY 2)` too). A number in a window's `ORDER BY` (`row_number() OVER (ORDER BY 1)`) is not checked as a position: PostgreSQL reads it as a constant and runs the query, and MySQL refuses it. Neither is a number in an aggregate's own `ORDER BY` (`string_agg(name, ',' ORDER BY 1)`, `array_agg(id ORDER BY (2))`, `percentile_cont(0.5) WITHIN GROUP (ORDER BY 1)`): PostgreSQL reads it as a constant, and MySQL reads it in `group_concat(name ORDER BY 1)` as a position among the function's own arguments, which the check already sees;
- `JOIN … USING (email)`;
- a subquery used as a value whose SELECT list carries it: `WHERE (SELECT email FROM users …) LIKE 'a%'`, `WHERE 'x' IN (SELECT email …)`, `ORDER BY (SELECT email …)`, `= ANY (SELECT email …)`, `ARRAY(SELECT email …)`. A scalar subquery that is itself a plain SELECT item (`SELECT (SELECT email FROM users LIMIT 1) AS e FROM t`) runs and is masked; its alias (`e`) counts as sensitive and is required (see *A construct the check cannot follow* below); `EXISTS (SELECT email …)` is not a value and runs.
- a set operation whose later branch puts it under the first branch's name: `SELECT id AS x FROM a UNION ALL SELECT email FROM b` makes `x` sensitive everywhere in the query: a filter such as `… WHERE x LIKE 'a%'` is refused and the message names `x`; `ORDER BY 1` on it is refused too and the message names the sensitive column of the later branch. The name counts everywhere in the statement, the first branch included: `SELECT id FROM a WHERE id > 5 UNION ALL SELECT email FROM b` is refused because of `id`. Give the first branch's item its own alias (`SELECT id AS contact …`) if that gets in the way.

**The column's values are compared.** Only `UNION ALL` may carry a sensitive column. A `UNION` (which removes duplicates), an `INTERSECT` or an `EXCEPT`, with or without `ALL`, compares rows, and the rows it keeps or drops reveal whether a value matches: `SELECT id, email FROM users INTERSECT SELECT id, 'alice@x.com' FROM users` returns a row only for a user whose email is `alice@x.com`. Such a query is refused wherever the set operation stands (at the top, in a derived table, a CTE, an `EXISTS`, a `LATERAL` or an `IN` subquery), also when only a count is selected from it, and the message names the column: *Strict sensitive columns: the query compares the column email across a UNION, INTERSECT or EXCEPT, which reveals its values. This connection does not allow that.* `SELECT email FROM a UNION SELECT email FROM b` is refused; write `UNION ALL`, which keeps every row and compares nothing. An alias that carries a sensitive value counts too, and a set operation that compares no sensitive column (`SELECT id FROM a INTERSECT SELECT id FROM b`) runs. The same holds for `DISTINCT` (MySQL `DISTINCTROW` too) and `GROUP BY ALL`, which remove duplicate rows: `SELECT DISTINCT email FROM users WHERE id IN (1, 2)` returns one row or two depending on whether the two addresses are the same, and a count over a derived `SELECT DISTINCT email` is a yes/no test for any address. A `SELECT` with `DISTINCT` or `GROUP BY ALL` whose list carries a sensitive column, an alias of one or a scalar subquery that reads one is refused wherever it stands, and the message names the column: *Strict sensitive columns: the query removes duplicates of the column email with DISTINCT or GROUP BY ALL, which reveals whether its values match. This connection does not allow that.* The column is masked, so a distinct list of it would show `[REDACTED]` in every row anyway: leave `DISTINCT` out. `DISTINCT ON (id) id, email` runs (duplicates are removed by `id`; `DISTINCT ON (email)` is refused as a use of the column). `SELECT DISTINCT *` is refused as a construct the check cannot follow (*a DISTINCT or GROUP BY ALL over a star*): list the columns.

**A construct the check cannot follow.** The message names the construct, for example *Strict sensitive columns: the query uses a natural join, which the strict check cannot follow, so it was not run.* These are refused whether or not the query names a sensitive column, because the check cannot tell which columns they read:

- a `NATURAL` join;
- a whole-row reference: `u::text`, `to_jsonb(u)`, `SELECT u FROM users u`, and a record-taking built-in or aggregate written as an attribute of the row, `u.to_jsonb`, `u.row_to_json`, `u.concat`, `u.quote_literal`, `u.json_agg` (PostgreSQL reads `u.f` as `f(u)`); the message says *the whole row of u through u.to_jsonb*. On PostgreSQL a real column named like one of these (`concat`, say) is refused when qualified; write it without the qualifier. On MySQL these names are ordinary columns. A user-defined function with a record argument is not recognised;
- a star anywhere but as a SELECT item or in `count(*)`: `row_to_json(u.*)`;
- a position at or past a star (`SELECT * FROM users ORDER BY 1`, `SELECT * FROM users ORDER BY 2`, `SELECT id, * FROM users ORDER BY 3`), or at a scalar subquery item that selects a star (`SELECT (SELECT * FROM v LIMIT 1) AS x FROM t ORDER BY 1`): the star expands to columns the check cannot see, and on a `UNION`, `INTERSECT` or `EXCEPT` this holds in every branch;
- a star inside a subquery used as a value: `WHERE id IN (SELECT * FROM v)`, `WHERE (SELECT * FROM u LIMIT 1) = 'x'` (name the column instead); also the alias of a scalar subquery item that selects a star, used anywhere but as a SELECT item: `SELECT (SELECT * FROM v LIMIT 1) AS x FROM t ORDER BY x`;
- a scalar subquery item that reads a sensitive column (or selects a star) without an alias, wherever it stands: `SELECT (SELECT ssn FROM u LIMIT 1) FROM t`, `SELECT 1 FROM (SELECT (SELECT ssn FROM u LIMIT 1) FROM t) d`. The engine names such an item itself: MySQL after its SQL text, a name `ORDER BY`, `GROUP BY`, `HAVING` and an outer query can use and the check cannot follow (PostgreSQL uses the subquery's own column name, which the check sees). The alias is required on every engine alike; give it one (`… LIMIT 1) AS s`);
- a column list on a table alias (`WITH c(e) AS (…)`);
- a `UNION`, `INTERSECT` or `EXCEPT` with a star in a later branch, whatever the tables hold: `SELECT * FROM a UNION ALL SELECT * FROM b` is refused, since the check cannot see which columns the star brings in under the first branch's names; list the columns instead;
- a set operation with a star in the first branch at or before a position a later branch fills with a sensitive column, or with an unaliased expression there (`SELECT lower(name) FROM a UNION ALL SELECT email FROM b`): give the first branch's item an alias;
- a star in any branch of a `UNION` (without `ALL`), `INTERSECT` or `EXCEPT`: `SELECT * FROM a UNION SELECT id, name FROM b` (the comparison could cover a sensitive column inside the star);
- a set operation operand that is not a `SELECT`: `TABLE users UNION ALL SELECT email FROM users`. The parser misreads `TABLE users`, so the check cannot see its columns; write `SELECT … FROM users`;
- a `VALUES` list in a set operation, which the parser reads as a star: as a later operand (`SELECT id FROM b UNION ALL VALUES (1)`) or in parentheses it is always refused; as the first operand only when a later branch carries a sensitive column (`VALUES (1) UNION ALL SELECT email FROM b`) or the operation is not `UNION ALL` (`VALUES (1) UNION SELECT id FROM b`), while `VALUES (1) UNION ALL SELECT id FROM b` runs. The message says *a VALUES list in a set operation*; write the values as a `SELECT` (`SELECT 1 UNION ALL …`).
- on MySQL/MariaDB, an executable comment (`/*! … */`, `/*M! … */`) anywhere in the query, a string literal included: the server runs its content, which the check cannot read. The message says *a MySQL executable comment*; write the query without one;
- on any connection other than MySQL/MariaDB (that is, PostgreSQL), an escape string continued on a later line (`E'…'` followed, after a line break, by `'…'`): PostgreSQL reads the second piece as part of the escape string, the check's parser as a separate string, so they disagree on where the text ends. The message says *an escape string continued on a later line*; write the string on one line or join the pieces with `||`.

On PostgreSQL a column that has the same name as an unaliased table, or as an alias, in the query is read as a whole-row reference and refused (`SELECT status FROM status`); write it with its table or alias (`SELECT s.status FROM status s`). The refusal's hint tells the model the same, so an automatic correction usually fixes it in one step. A table with an alias is read as a row only through its alias, so `SELECT status FROM orders o JOIN status s ON …` runs. A function in `FROM` or under `LATERAL` (`unnest(…)` too) counts the same way: PostgreSQL names it after its alias, or after the function when it has none, so `SELECT generate_series FROM generate_series(1, 3)` is refused, while `SELECT g.g FROM generate_series(1, 3) g` runs. MySQL has no whole-row references, so there a bare name is always a column.

**A query that cannot be parsed.** The message is *Strict sensitive columns: the query could not be parsed, so it was not run.* In strict mode every query is parsed, whether or not it names a sensitive column, and one the parser cannot read as one plain statement is refused.

**A query nested too deeply.** The message is *Strict sensitive columns: the query is nested too deeply to check, so it was not run.* A query with thousands of nested parentheses or hundreds of nested subqueries exceeds what the parser can follow and is refused; simplify it.

Because patterns match by substring, strict mode also refuses a filter on a column such as `email_verified` when `email` is a pattern; use **Sensitive columns** for exact names if that gets in the way.

The LLM is told the rule, and when a question needs a filter on a sensitive column it is asked to explain that instead of generating a query. If it writes such a query anyway, the rejection goes back to it with a hint, as for any failed query. In **Review First** mode the generated query is shown first and the rejection comes when you run it. Sorting the result grid by a sensitive column is refused too, because the sort adds an `ORDER BY` on that column, so in this mode the grid turns sorting off on every masked column (all values `[REDACTED]`): its header is not clickable and its tooltip says why. A column the check refuses that is not masked can still be clicked, and the sort then fails.

This makes narrowing a masked value down much harder but is not a guarantee: for values that must never be learned, use **Excluded columns**. The mode is off by default because questions that filter on a sensitive column stop working.

### Excluded Schemas

Schema names to **completely hide** from the LLM. Tables, columns and row counts in these schemas are not included in any prompt. Useful for internal audit schemas (`pg_audit`, `logs`, …). On MySQL/MariaDB a schema is a database, and a connection sees **every database its account can read**, not only the one in its settings: list the other databases here, or grant the account access to that one database only (see [MySQL](../datasources/mysql.md#connecting)). One name per line:
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

### Row Filters

Per-table SQL conditions (`row_filters`), one per table the admin lists, e.g.
`{"public.orders": "tenant_id = 42"}`. Every statement sent to the database is rewritten
before it runs: each reference to a listed table becomes a filtered derived table,

```sql
SELECT o.id FROM (SELECT * FROM public.orders WHERE (tenant_id = 42)) AS o
```

so the filter applies to what the query **reads**, whatever the query projects. Aggregates,
joins and CTEs over a filtered table are filtered; tables not listed are unfiltered, by
your choice. The rewrite applies to the first execution, every self-corrected retry, the
re-run after a query returns no rows, approved queries, edited queries, re-sorts and
answers served from the cache. The SQL shown to you, stored with the message, kept as a
verified example and sent to the LLM is the query as the model wrote it.

**Before you change row filters.** Three things happen when you add, change or remove a
filter:

1. Every earlier result on the connection stops being shown. The chat history, share links,
   CSV and Excel exports and PDF reports show *These results were computed under a
   different row filter. Re-run the question.* instead. Re-run what you need and share it
   again; links you already sent stay broken.
2. The database must be reachable when you save a filter change. Otherwise the whole save
   is refused and nothing changes.
3. Filtered tables are queried more slowly, especially on MySQL with large tenants. Index
   the column your condition uses (see **The fence** below).

**Keys.** `schema.table`, written as the database stores the names. Quote a part the way
the engine does (`public."Orders"` on PostgreSQL, `` `db`.`Orders` `` on MySQL) to keep its
case; the stored key has no quotes. The Privacy Settings form shows such a key quoted
(`public."Orders"`), as you would type it, so saving sends the same key back; the API
returns it without quotes. A query that names a filtered table in a different case
is rejected rather than run unfiltered (PostgreSQL folds unquoted names to lowercase, so
`Orders` there means `orders`; MySQL keeps the case). A table in two schemas needs the
schema in the query, or the query is rejected as ambiguous. A column written as
`schema.table.column` (`public.orders.id`) is rewritten to `table.column` (`orders.id`), the
name the derived table goes by. If another table in the query has the same bare name (a
table of another schema, or an alias), that would be ambiguous, so the query is rejected
with the advice to give the filtered table an alias. A column written with a database name
as well (`db.public.orders.id`) is rejected; qualify it with the table name or an alias. An
unaliased table function (`FROM orders(1)`) goes by its function name and counts as such a
table.

**Child tables.** A table without the tenant column is filtered through its parent:
`{"public.order_items": "order_id IN (SELECT o.id FROM orders o WHERE o.tenant_id = 42)"}`.
The tables named inside a condition are not themselves rewritten. Qualify the columns inside
such a subquery with its own table or alias, as here: an unqualified `tenant_id` there is
looked up in `orders` first and, if `orders` has no such column (or loses it later), in
`order_items` and then in the query around it, which is not what you meant.

**Setting them.** Use the **Row filters** list in the Privacy Settings form (one row per
table; the table input offers the cached schema's names), or send `row_filters` to
`POST /api/v1/connections` or `PUT /api/v1/connections/{id}/privacy`. An absent field on
`PUT` leaves the map unchanged; `{}` clears it. **Reset to defaults** keeps the filters.
Saving filters clears the connection's cached answers but keeps the cached schema.

**Validation.** Each key must be `schema.table`; each condition is rejected (HTTP 422,
naming the key and the reason) when it contains `;`, a comment (`--`, `/*`, or `#` on
MySQL), a `$`, starts with `WHERE`, is longer than 1000 characters, or is not a single
well-formed condition for the connection's dialect (so `tenant_id = 7 UNION SELECT 1`,
`... LIMIT 5` and an unbalanced parenthesis are refused).
Keys are limited to the database's own identifier length (63 bytes on PostgreSQL, 64
characters on MySQL); conditions may not contain control characters other than tab and
newlines, placeholders (`:tenant`, `?`), data-modifying statements, or more than 20 levels
of nested parentheses.
An unquoted key part is folded as the engine folds it (PostgreSQL lowercases, MySQL keeps
the case), so `Public.Orders` on PostgreSQL is stored as `public.orders`; quote a part to
keep its case. Every new or changed filter is then **checked against your database** before
it is stored, with a planning check (`EXPLAIN`) of the condition exactly as queries will run
it, which reads none of your data: once as written, as it runs at the top level of a query,
and once with its columns qualified, as it runs inside a subquery (see **How a condition
runs**), so a condition that only one of the two forms accepts is refused. A condition the rewriter cannot apply is refused with
`row_filters["public.orders"]: <reason>`; so is a filter on a catalog view the rewriter
never lets a query read, such as `pg_catalog.pg_stats` or MySQL's
`information_schema.column_statistics`. A table that does not exist or a column the table
does not have is refused with
`row_filters["public.orders"]: rejected by the database: <the engine's message>` (shown as
described under [Error Messages You See](#error-messages-you-see)). On MySQL a type
mismatch such as `id = 'abc'` is only a warning, so it is not refused. If the database
cannot be reached, or the check has not finished within the query timeout plus five
seconds, the save is refused with
`row filters could not be verified against the database: <reason>` and nothing changes.
Unchanged filters are not re-checked, so you can save other settings while the database is
down as long as the filters stay as they are.

**How a condition runs.** The condition is read with the connection's SQL dialect and
written back into each query from that reading, so what runs is that reading of your text.
On MySQL servers with `PIPES_AS_CONCAT` enabled, `||` in a condition is read as `OR`, not as
the server would read it; avoid it. Under `ANSI_QUOTES` a `"..."` in a condition is read as a
string; write a column name with backticks. A backslash inside a string in a condition is read
as an escape: before an ordinary letter it is dropped (`'dom\user'` runs as `'domuser'`). Under
`NO_BACKSLASH_ESCAPES` a tab or a line break inside a string is also written back as a backslash
escape (`\t`, `\n`), which the server reads as written, so the condition compares with a
different value. On a server with `NO_BACKSLASH_ESCAPES` such a condition is refused when you
save it, and a condition saved earlier refuses every query until it is changed (see the
`NO_BACKSLASH_ESCAPES` paragraph under [Sensitive Column Patterns](#sensitive-column-patterns)).
On other servers keep backslashes, tabs and line breaks out of strings in conditions all the
same: the condition runs as read, not as written.
The condition is trusted as written: it runs inside the read-only transaction and the
statement timeout, and a subquery in it reads its own tables unfiltered.
When the filtered table is read inside a subquery of the query (an `EXISTS`, an `IN`, a CTE),
each column of the condition that has no table name, outside a subquery of the condition
itself, is written with the table's name (`tenant_id = 42` runs as `orders.tenant_id = 42`).
Unqualified, a column that was dropped or renamed after the save (which is not checked
again) could resolve to a column of the same name in the surrounding query, and every row
would pass; qualified, the query fails with the database's unknown-column error. At the top
level of a query the condition runs as written, since nothing outside the filtered table can
supply a column there. On PostgreSQL a bare `user`, `current_role` or `system_user` is the
session value, not a column, and stays as written, and so does a bare column named like the
table (`orders IS NOT NULL`, `(orders).tenant_id`), which there is the table's whole row.
A condition that names a function in quotes with capitals (`"Lower"(name)`) or uses a
`{...}` literal is refused, since the rewriter would write it back as a different call.

**What the LLM is told about a filtered table.** The schema sent to the model normally
carries, when you allow it, sample values and an approximate row count per table. Both
describe the whole table, hidden rows included, so for a filtered table neither is sent:
no sample values from the catalog's statistics (PostgreSQL), no `(≈N rows)` header, and no
entity-resolution hint from its values. A MySQL ENUM or SET column keeps its declared
members: they are part of the type, not of any row. Value mappings in the semantic model
are not affected (see [Semantic value mappings](#semantic-value-mappings)). Turning a filter
off restores the hints at once; no schema refresh is needed. A filter whose table name
differs from the database's only in letter case withholds the hints as well. While a
connection is in the migration state (see below) no table sends them.

**The fence.** Queries on a filtered table are slower than the same queries without a
filter. Savvina makes the database apply your condition before anything the model wrote
touches the rows, so an error raised by the model's own expressions cannot reveal that a
hidden row exists. The cost: the model's predicates on a filtered table cannot use an index
on that table unless your condition itself does, so `WHERE id = 5` on a tenant with
millions of rows scans that tenant's rows. On MySQL every filtered table is read in full
into a temporary table on each query before the rest of the query runs, so a `LIMIT 10`
preview of a large tenant costs a scan of the whole tenant. On PostgreSQL a correlated
subquery over a filtered table rescans the tenant's rows once per outer row. Index the
column your condition filters on (for `tenant_id = 42`, an index on `tenant_id`). On
MariaDB this protection is unverified: MariaDB's optimizer is not MySQL's.

**Results computed under an earlier filter.** Every result is stored with a fingerprint of
the row filters it was computed under. When you add, change or remove a filter, results
stored before the change are no longer served: the chat history shows *These results were
computed under a different row filter. Re-run the question.* in place of the table. Anyone
opening a share link to a single result, or downloading its CSV or Excel file, gets that
message instead of the data; a CSV or Excel export from the chat does the same, a shared
session shows the notice per message, and a PDF report carries it in place of the table.
The rule is "same filters or nothing": loosening a filter also retires older results.
Results saved before this version are treated as computed with no filters, so on a
connection that has row filters every older result shows the notice until it is re-run.
Answers served from the cache are unaffected, because saving filters clears the cache. A
report section built from such a result shows the notice and no chart.

**Fail-closed.** A statement the rewriter cannot parse, a query with more than one
statement, a CTE named like a filtered table (inside its own body that name would be the
unfiltered table; rename the CTE), an ambiguous or mis-cased table name, and a construct
the rewriter cannot write back are rejected before they are sent; the chat shows the reason.
So are functions that read rows from SQL or a table name given as a string
(`query_to_xml`, `table_to_xml` and the rest of that family, `ts_stat`, `ts_rewrite`,
`crosstab`, `connectby`, the `dblink` functions) and the statistics views that expose column values (`pg_stats`,
`pg_statistic`, MySQL `information_schema.column_statistics`, MariaDB `mysql.column_stats`;
on MySQL also written without a schema, since the current database can be that schema).
So is a PostgreSQL function name in quotes with capitals (`"Lower"(x)`): the rewriter would
write it back as the built-in `LOWER(x)`, a different function from the one PostgreSQL calls;
write the name in lowercase without quotes. A quoted name sqlglot does not know
(`"MyFunc"(x)`) keeps its case. A `{...}` literal, which neither engine reads, is refused too.

**Limits.** Row filters rewrite table references in the SQL text. A function that runs SQL
from a string and is not on the list above (an extension, a function defined in your
database) can still read a filtered table unfiltered. If the connection's database user can
call such functions, restrict them at the database, or rely on native row-level security.
The fence does not hide *how long* a query takes: the time still depends on the rows your
condition excludes.

**Three controls, three jobs.** Row filters decide which rows the database reads. Excluded
schemas, tables and columns decide what the model is told about. Sensitive patterns,
sensitive column names and excluded columns also decide what is shown: their values come back as `[REDACTED]`. Use
them together; none of them replaces another.

**Upgrading from 2.2.** The old single `row_filter_sql` is no longer applied. A connection
that still has one and no `row_filters` is in a migration state: every query is rejected
with "Row filter needs migration" until you open its privacy settings. The form shows a
banner with the previous condition. Either add the per-table rows and save, or click
**Remove the old filter** (after a confirmation) to run unfiltered. A save that would drop
the old filter without doing either is refused. While the connection is in the migration
state every stored result shows the stale-filter notice, and once you save per-table
filters, results from before the migration keep showing it and must be re-run.

---

## Sensitive Column Auto-Detection

Pattern matching is applied **automatically** at schema introspection time. If your table has a column `customer_email_address`, it matches `email`. The column appears in the schema as `[SENSITIVE]`, no sample values are sent for it, and its result values are masked, without you listing it anywhere. For a name too common for a pattern, list it under **Sensitive columns** (see [Sensitive Columns](#sensitive-columns)).

**What masking does, and does not, protect.** Masking changes what is displayed, not what
the query reads. A `WHERE`, `JOIN`, `ORDER BY` or `GROUP BY` on a sensitive column still
works on the real values, so someone who can ask free-form questions can narrow a masked
value down (`WHERE salary > 99999 AND salary < 100001` returns the matching rows with the
salary redacted), and an error raised by an expression over the value tells them something
too. Masking prevents accidental exposure; it is not a defence against a user who is
trying to extract the value. For that, restrict the rows with row filters, remove the
column with **Excluded columns** so the model never sees it, restrict the database
account itself, or turn on **Strict sensitive columns**, which refuses most such queries
before they run. Statistical aggregates (`COUNT`, `SUM`, `AVG`) over a sensitive column are
returned unmasked on purpose (with strict mode off; strict mode refuses them); a group of
one row reveals its value. A query that stores a value and reads it back under another
column in the same statement is refused whatever the strict setting: `set_config` on
PostgreSQL, a user-variable assignment (`@v := email`) on MySQL.

A query under row filters is regenerated before it runs, which can spell a function
differently (`strpos(email, '@')` becomes `POSITION('@' IN email)`, `now()` becomes
`CURRENT_TIMESTAMP`). An unaliased column is therefore given the name the database would give
the query as written: on PostgreSQL the function's name (`strpos`, `now`), on MySQL the
expression's text as written (`IFNULL(a,0)`). This holds in every `SELECT` of the query:
the result, a CTE, a derived table, a scalar subquery, every branch of a `UNION`, an `IN` or
`EXISTS` subquery. So `WITH x AS (SELECT now() FROM orders) SELECT now FROM x`, and a
`GROUP BY strpos` or `ORDER BY now` inside a subquery, keep working under a filter. On MySQL
a name inside a CTE or derived table is used only up to 64 characters (MySQL's limit for a
column name there; 256 for a result column). A MySQL column whose text cannot be used (it
holds a comment, or is too long) and that reads a filtered table is named after the query's
own text for it, cut to 64 characters, never after the rewritten text, which would show your
condition in the column header.
On PostgreSQL `date_part('year', d)` is written back as `date_part`, not as `EXTRACT`, so it
keeps its name and its `double precision` type. A function written without parentheses stays
a function: MySQL's `UTC_DATE`, `UTC_TIME` and `UTC_TIMESTAMP` and PostgreSQL's
`CURRENT_TIME`, in queries and in conditions. Where the name cannot be predicted the column
comes back under the new spelling (for example a PostgreSQL `date_part` whose second argument
starts with a keyword such as `current_date` or a subquery, shown as `extract`); give such a
column an alias to keep its name. The masking lands on the right column either way.

---

## What Happens During Schema Introspection

When a schema is introspected (on first chat, after **Refresh Schema**, or on the first question after the privacy settings are saved), the adapter:

1. Fetches all schemas, tables, columns, primary keys and foreign keys
2. Applies `excluded_schemas` and skips these schemas entirely
3. Applies `excluded_tables` and skips these tables
4. For each column:
   - If `include_column_comments` is off, skips comments
   - If the column is in `excluded_columns`, removes it entirely
   - If the column matches a sensitive pattern, keeps it but collects no values for it (on MySQL, its ENUM/SET member list is withheld from the type too). Its comment is collected but withheld whenever the schema is read (below)
   - Otherwise, if `include_sample_values` is on, attaches example values. PostgreSQL reads the catalog's most-common values (`pg_stats`). MySQL reads an ENUM/SET column's declared members. No table is scanned

Excluded schemas, tables and columns, and the withholding of sample values, ENUM/SET member lists and sensitive columns' comments, are applied again every time a cached schema is read. A schema cached under older settings therefore never shows the LLM more than the current settings allow. The schema explorer on the connection page reads it the same way, so it shows what the LLM sees: no excluded object, sample values only where they may be sent, and no row count or statistics sample values on a row-filtered table. The table embeddings used to pick the tables relevant to a question are built from the schema after this pass too.

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

## Privilege Check

Savvina reads what the connection's database role can do and shows the findings at **Connections → [your connection] → Edit settings**, above **Privacy Settings**. The check runs when a connection is created, when its connection details are saved (**Edit connection → Save**), on a privacy save that changes the row filters, the excluded tables or the excluded schemas, and on **Check again** in the findings banner (`POST /api/v1/connections/{id}/test`, which checks only after a successful connection test). It reads only the database catalog, never a table row, and it never blocks a save or a query. A check that could not run (the database refused the connection, the catalog could not be read in time) is shown as *Privileges could not be checked*, with the reason. On PostgreSQL the reason is the database's error, redacted as other database errors are. On MySQL it is a sentence for the error number (the server refused the user name or password, the database does not exist, the server could not be reached, and so on), or *MySQL error N* for a number without a sentence; it never repeats the server's text, which can name the user and host. When **Check again** cannot connect, the panel shows the connection error and the last result stays.

Two groups of findings:

- **Row-level security in the database does not apply to this connection's role** (PostgreSQL, shown when a row-filtered table has row-level security enabled): the role is a superuser, has `BYPASSRLS`, or owns a filtered table that does not `FORCE ROW LEVEL SECURITY` (a role that inherits the owner's privileges counts as the owner). Savvina's own row filters still apply; the finding says that the database's policies do not. When no row-filtered table has row-level security, the superuser and `BYPASSRLS` findings are listed with the second group; on MySQL the superuser finding always is.
- **More privileges than Savvina needs**:
  - the role can write on any table of the database: `INSERT`, `UPDATE` or `DELETE`, or a column-level `INSERT (a)` or `UPDATE (b)`. On MySQL this is every privilege that changes data, structure or files (`DROP`, `ALTER`, `CREATE`, `INDEX`, `TRIGGER`, `EVENT`, `LOCK TABLES`, `FILE` and the rest, and on MariaDB `DELETE HISTORY`) and `ALL PRIVILEGES`, granted at any level. `ALL PRIVILEGES` on a database or a table is revoked as the write privileges valid there, so the account keeps `SELECT`; a grant on a single function or procedure is not read. On PostgreSQL a table the role owns, or whose owner it inherits, counts, and so does membership in `pg_write_all_data`;
  - the role can read a table this connection excludes (the LLM never sees it, but a query edited by hand can still read it; see [Editing Queries (Review First Mode)](03_execution-modes.md#editing-queries-review-first-mode)). On PostgreSQL, membership in `pg_read_all_data` counts;
  - on PostgreSQL, the role can execute an installed function that runs SQL passed as a string (`dblink`, `crosstab`, `connectby`; Savvina refuses `dblink` and `dblink_exec` in every query, and the others only on a connection with row filters);
  - on MySQL, the account has `SUPER` alone, which grants no table access but allows server administration.

On PostgreSQL a superuser gets no finding of the second group, because every privilege follows from it; `BYPASSRLS` and an owned table without `FORCE` are still reported for it. On MySQL an account with `ALL PRIVILEGES` at the global level (MySQL 8 lists them one by one: the check counts `SUPER` together with every privilege that changes data or structure) is reported as a superuser and its other findings are not listed; `SUPER` alone is reported on its own and hides nothing.

Each finding comes with the SQL that fixes it and a **Copy** button; run it as a database administrator, then **Check again**. Each `REVOKE` names the grantee the privilege comes from: the connection role, `PUBLIC`, a PostgreSQL group role the connection role inherits, or a MySQL role active in the session. On MySQL each `REVOKE` names the account or the active role that holds the privilege. A privilege the account holds both directly and through a role is shown on the account; revoke it there and check again to see the role's copy. When MySQL prints the account's and a role's privileges together as `ALL PRIVILEGES`, the role's `REVOKE` can also name a privilege only the account holds; MySQL accepts it. For example, on PostgreSQL:

```sql
REVOKE DELETE, INSERT, UPDATE ON "public"."customers" FROM PUBLIC;
REVOKE UPDATE ("status") ON "public"."orders" FROM "analyst";
REVOKE DELETE, INSERT, UPDATE ON "sales"."invoices" FROM "app_writers";
-- "sales"."ledger": the role owns it or inherits the owner; ownership cannot be revoked.
```

A privilege that comes from owning the table cannot be revoked; the finding says so in a comment and ends with the script below. When both the write finding and the excluded-tables finding rest on ownership, only the write finding carries the script and the other points to it, so the role is created once. Names in comment lines have control characters replaced with `?`, so a name with a line break cannot turn into a statement when you copy the SQL. A list longer than 50 tables is cut with *and N more*, and a list of more than 500 write grants is cut at 500 (the fix ends with a comment saying so): revoke what is listed and check again. A long list of revokes on PostgreSQL collapses into `REVOKE ... ON ALL TABLES IN SCHEMA`; run it as the tables' owner or a superuser, since a table you hold no privilege on aborts the statement. On PostgreSQL 14 and later, membership in `pg_read_all_data` or `pg_write_all_data` reads or writes every table without a grant on it; the check reports it and the fix revokes the membership (`REVOKE pg_write_all_data FROM "analyst";`). Only a membership whose privileges are inherited counts (on PostgreSQL 16 the membership's own `INHERIT` option; before 16 the member's `INHERIT` attribute). When a role other than the bootstrap superuser granted the membership, the `REVOKE` adds `GRANTED BY <that role>`, one `REVOKE` per grantor. When a group role the connection role inherits holds the membership, the `REVOKE` names that group, and a comment says it removes the membership from the group's other members too.

The superuser finding, and a write or excluded-tables finding that rests on ownership comes with a script that creates a dedicated read-only role (`savvina_reader`) with `SELECT` on every schema the connection does not exclude. Replace the password placeholder, run it as an administrator, point the connection at the new role (**Edit connection**) and **Check again**. It is the script shown under [Using a Read-Only Database Role](02_connecting-to-data.md#using-a-read-only-database-role). It creates a new role and leaves the existing one untouched, so it is safe to run when the connection role is also your administrator account.

The script keeps the tables this connection excludes out of the new role: on PostgreSQL it revokes `SELECT` on each of them after the grants; on MySQL a grant on a whole database cannot leave one table out, so the script names the excluded tables that stay readable. A `SELECT` that `PUBLIC` holds on an excluded table still reaches the new role; the excluded-tables finding revokes it from `PUBLIC`. On MySQL, when an excluded table is readable through a grant on its whole database (or on `*.*`), the fix revokes that grant, and a comment says it removes `SELECT` on every table of the database. To keep Savvina's access afterwards, grant `SELECT` per database or per table.

The function fix revokes `EXECUTE` from `PUBLIC` as well, because an extension grants it to every role on install, so other roles lose it too; the banner and a comment before the `REVOKE`s say so. It names `PUBLIC` and the connection role only: a function granted through a group role stays executable until you revoke it from that role.

The fix for an owned table is `ALTER TABLE ... FORCE ROW LEVEL SECURITY`. When no permissive `SELECT` policy applies to the connection role on that table (a restrictive policy alone lets no row through), a comment before it warns that after `FORCE` the role reads no rows from it: create a policy first, or every Savvina result on that table comes back empty.

A connection created before this release shows *Privileges have not been checked yet* with a **Check now** button. When there is nothing to report, Privacy Settings ends with *Privileges checked \<date\>: the role has only the access Savvina needs.* **Check again** appears only once there is a finding or an error to show.

The check does not enumerate the tables under an excluded schema when it looks for excluded tables the role can read, does not look inside user-defined functions, and does not read column-level `SELECT` grants (`GRANT SELECT (a, b) ON ...`); column-level `INSERT` and `UPDATE` grants are reported with their columns. Write privileges are checked on every table outside the system schemas, on both engines. Privileges a MySQL account holds only through a role it has not activated do not apply to the session and are not reported. The `SUPER` fix has one `REVOKE SUPER ON *.*` per grantee that holds it, the account or an active role. When both hold it, it shows on the account first: revoke it there and check again.

---

## Error Messages You See

When a query fails at the database, the error shown in the chat and stored with the message depends on the kind of error. For an unknown or ambiguous name, a grouping error, a type mismatch, a permission error or a syntax error, you see the database's message as is: these messages quote names from the query, not stored values. For any other error, such as a value that could not be converted, you see only the first line of the message, with every quoted part replaced by `[REDACTED]`. When the connection to the database drops while the query runs, you see "The connection to the database was lost while the query ran. Try again." instead of the driver's message. Savvina does not log the full text; the server log holds the same redacted line. MySQL warnings, which also quote stored values (`Truncated incorrect DOUBLE value: '…'` for a query that compares a text column with a number), are not read at all, so they reach neither the chat nor the log. Savvina's own rejections (a row filter that cannot be applied, the strict sensitive-column rule, a double quote in a query on a MySQL server with `ANSI_QUOTES` while either is on, a backslash in a query on a MySQL server with `NO_BACKSLASH_ESCAPES`, and there, with row filters on, a condition holding a backslash, tab or line break, or a control character inside a string of the query) are shown as written; they quote names from the query or the settings, never a value.

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

Saving privacy settings clears the connection's cached schema, table embeddings and cached query answers. The next question introspects the schema again under the new settings, so a newly enabled toggle takes effect without a manual **Refresh Schema**. A save that changes only `row_filters` keeps the cached schema (the filters do not change introspection) and clears the rest.

A save from a form loaded before another privacy save of the same connection (in another tab, say) is refused rather than overwriting the newer settings (renaming the connection or editing its config in between does not count): the form shows *The privacy settings changed since this form was loaded. They have been reloaded: apply your changes again.* and reloads the stored settings, dropping the edits it held. If that reload fails, the form says so and keeps what it holds.

---

## Recommendations by Use Case

| Use Case | Recommended Settings |
|---|---|
| Development DB with synthetic data | Turn sample values on; default patterns |
| Production DB with real PII | Leave sample values off; add PII tables to the excluded list; review the semantic model's value mappings |
| Financial / HR data | Disable row counts; exclude salary/wage/income columns explicitly |
| Maximum privacy (regulatory) | All three toggles off; use excluded schemas to limit scope; remove value mappings you do not want sent; run a local model via Ollama |
| Internal analytics (known safe) | Sample values on; generate a semantic model for accuracy |
