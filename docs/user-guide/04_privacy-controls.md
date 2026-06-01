# Privacy Controls

Savvina AI is designed on a **privacy-by-design** principle: you have precise control over exactly what database metadata reaches the LLM. Query results are **never** sent to the LLM — only the schema description (and your question) are included in prompts.

---

## What the LLM Receives

In the default configuration, the LLM system prompt includes:

1. **Schema DDL** — `CREATE TABLE` statements listing table names, column names, data types, primary keys, and foreign key relationships
2. **Sample values** — up to 5 distinct values per column (e.g., `status` column: `active`, `pending`, `cancelled`)
3. **Column comments** — PostgreSQL `COMMENT ON COLUMN` descriptions, if present
4. **Approximate row counts** — from `pg_stat_user_tables`
5. **Semantic model** — your curated business glossary, if one has been generated

**The LLM never receives:** query results, actual data rows, user-entered values, or anything outside the schema/semantic context.

---

## Per-Connection Privacy Settings

Each connection has independent privacy settings. Configure them at **Connections → [your connection] → Privacy Settings**.

### Toggle Controls

| Setting | Default | Effect when disabled |
|---|---|---|
| **Include sample values** | On | Columns have no sample values in the LLM prompt. Reduces accuracy for enum-like columns. Recommended for PII-heavy schemas. Also disables **Named Entity Resolution** — when on, the system extracts quoted strings and capitalised proper nouns from the user's question and matches them against column sample values to surface the correct DB casing as a filter hint (e.g. resolving `"Acme Corp"` → `'acme corp'` if that is how the value is stored). |
| **Include column comments** | On | PostgreSQL column/table comments are omitted from the LLM prompt |
| **Include row counts** | On | Approximate row counts are omitted from the LLM prompt |

### Sensitive Column Patterns

A list of case-insensitive patterns. Any column whose name **contains** a matching pattern is automatically excluded from sample value collection (the column itself is still shown in the schema, but with no sample values — it displays `-- [SENSITIVE]` in the prompt).

**Default patterns:**
```
email, ssn, social_security, password, passwd, secret,
token, api_key, credit_card, card_number, cvv,
phone, mobile, address, salary, wage, income,
bank_account, routing_number, dob, date_of_birth,
national_id, passport, license_number, tax_id
```

You can add custom patterns. For example, to exclude columns containing `internal`:
- Add `internal` to the pattern list

### Excluded Schemas

List of schema names to **completely hide** from the LLM. Tables, columns, and row counts in these schemas are not included in any prompt. Useful to hide internal audit schemas (`pg_audit`, `logs`, etc.).

Format: one schema name per line, e.g.:
```
pg_audit
internal
staging
```

### Excluded Tables

Tables to hide from the LLM entirely. Can be specified as `table_name` (matches in any schema) or `schema.table_name` (exact match).

```
users
public.audit_log
internal.raw_events
```

### Excluded Columns

Individual columns to hide. Format: `schema.table.column`. The column is omitted from the `CREATE TABLE` DDL sent to the LLM.

```
public.customers.email
public.users.password_hash
public.employees.salary
```

---

## Sensitive Column Auto-Detection

Pattern matching is applied **automatically** at schema introspection time. You do not need to manually list every sensitive column — if the column name contains a sensitive pattern, sample values are automatically suppressed.

For example, if your table has a column `customer_email_address`, it matches the `email` pattern and its sample values are suppressed even though you didn't add it to the excluded columns list.

The column still appears in the schema DDL (the LLM knows it exists) but no sample values are sent. This is by design — removing the column entirely would prevent the LLM from writing queries that reference it.

To **fully hide** a column from the LLM (not even mentioned in the schema), add it to **Excluded Columns**.

---

## What Happens During Schema Introspection

When a schema is introspected (on first chat, or after **Refresh Schema**), the adapter:

1. Fetches all schemas, tables, columns, primary keys, and foreign keys
2. Applies `excluded_schemas` — skips these schemas entirely
3. Applies `excluded_tables` — skips these tables
4. For each column:
   - If `include_column_comments = false`, skips fetching comments
   - If `is_column_excluded()` is true (either explicit exclusion or sensitive pattern match), skips sample values
   - Otherwise, if `include_sample_values = true`, fetches up to 5 distinct non-NULL values

---

## Schema Format Sent to the LLM

The LLM receives a cleaned, DDL-style schema representation:

```sql
-- Schema: public
-- Table: public.customers (12,450 rows)
CREATE TABLE public.customers (
    id uuid PRIMARY KEY,
    first_name text,                   -- Sample: 'Alice', 'Bob', 'Carol'
    last_name text,                    -- Sample: 'Smith', 'Jones', 'Williams'
    email text,                        -- [SENSITIVE]
    status text,                       -- Sample: 'active', 'suspended', 'cancelled'
    created_at timestamp
);
-- FK: orders.customer_id -> customers.id
```

Notice:
- `email` shows `[SENSITIVE]` — sample values are suppressed
- Row counts appear in comments (if enabled)
- FK relationships are listed
- Excluded tables/columns are entirely absent

---

## Updating Privacy Settings

Settings can be updated without reconnecting:

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

After updating privacy settings, click **Refresh Schema** to re-introspect with the new settings applied. Previous schema cache and query cache are not automatically invalidated.

---

## Recommendations by Use Case

| Use Case | Recommended Settings |
|---|---|
| Development DB with synthetic data | All toggles on, default patterns |
| Production DB with real PII | Disable sample values for PII tables; add tables to excluded list |
| Financial / HR data | Disable row counts; exclude salary/wage/income columns explicitly |
| Maximum privacy (regulatory) | Disable all three toggles; use excluded schemas to limit scope |
| Internal analytics (known safe) | Keep defaults; generate semantic model for accuracy |
