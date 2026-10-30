# Connecting to Data

This guide explains how to create, test, manage, and delete data source connections in Savvina AI.

---

## Supported Data Sources

| Source | Type |
|---|---|
| **PostgreSQL** | Relational — asyncpg driver |
| **MySQL / MariaDB** | Relational — aiomysql driver |

---

## Creating a Connection

### Step 1 — Select a Data Source Type

Navigate to **Connections → New Connection**. A card grid shows all available data source types. Click the card for your source.

### Step 2 — Fill in Connection Details

The form is dynamically rendered from the adapter's schema — each source declares its own required fields. See the datasource-specific sections below for field-by-field details.

### Step 3 — Configure Privacy Settings (Optional)

Expand **Privacy Settings** to control what metadata reaches the LLM:

| Setting | Default | Description |
|---|---|---|
| Include sample values | Off | Sends up to 3 example values per column (on MySQL, an ENUM/SET column's members) to the LLM for better accuracy |
| Include column comments | Off | Sends database column description text |
| Include row counts | Off | Sends approximate row counts |
| Sensitive column patterns | (list) | Columns matching these patterns are never sampled and their values are masked in results (e.g., `email`, `ssn`, `password`) |
| Excluded schemas | (empty) | Schemas to completely hide from the LLM |
| Excluded tables | (empty) | Tables to completely hide |
| Excluded columns | (empty) | Individual columns to hide, format: `schema.table.column` |

See [Privacy Controls](04_privacy-controls.md) for full details.

### Step 4 — Choose an Execution Mode

| Mode | Description | Best For |
|---|---|---|
| **Auto-Execute** | Queries run immediately without review | Development databases you trust |
| **Review First** | See and optionally edit the query before it runs | Production databases, sensitive data |
| **Generate Only** | Returns the query text only — copy and run it yourself | Maximum control; no execution through Savvina AI |

See [Execution Modes](03_execution-modes.md) for a detailed comparison.

### Step 5 — Test and Save

Click **Test Connection** to verify credentials without saving. Click **Save Connection** after a successful test.

---

## PostgreSQL

| Field | Required | Example |
|---|---|---|
| Connection name | Yes | `Production DB` |
| Host | Yes | `db.example.com` |
| Port | Yes | `5432` |
| Database | Yes | `myapp` |
| Username | Yes | `readonly_user` |
| Password | Yes | — |
| SSL Mode | Yes | `prefer` — one of: `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |

See [Using a Read-Only Database Role](#using-a-read-only-database-role) below for setup SQL, and [PostgreSQL](../datasources/postgresql.md) for schema introspection notes and SSL guidance.

---

## MySQL / MariaDB

| Field | Required | Example |
|---|---|---|
| Connection name | Yes | `MySQL Production` |
| Host | Yes | `mysql.example.com` |
| Port | Yes | `3306` |
| Database | Yes | `myapp` |
| Username | Yes | `savvina_reader` |
| Password | Yes | — |
| SSL | No | `false` (disable on trusted networks) |

See [MySQL](../datasources/mysql.md) for read-only user setup and MariaDB compatibility notes.

---

## Using a Read-Only Database Role

Always connect Savvina AI using a read-only role. All generated SQL is validated before execution and runs inside a database-enforced read-only transaction; a read-only role is a third layer that also limits what the connection can *read*.

**PostgreSQL example** (replace `myapp` and `public` with your database and schema, and run the last three statements for each schema Savvina should read):

```sql
CREATE ROLE savvina_reader LOGIN PASSWORD '<choose a password>';
GRANT CONNECT ON DATABASE "myapp" TO savvina_reader;
GRANT USAGE ON SCHEMA "public" TO savvina_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO savvina_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA "public" GRANT SELECT ON TABLES TO savvina_reader;
```

`ALTER DEFAULT PRIVILEGES` covers the tables that the role running it creates later; for tables another role creates, run it with `FOR ROLE <that role>`.

**MySQL example:**

```sql
CREATE USER `savvina_reader`@`%` IDENTIFIED BY '<choose a password>';
GRANT SELECT ON `myapp`.* TO `savvina_reader`@`%`;
```

The privilege check generates this script for your schemas when the connection role is a superuser or owns the tables it reads (see [Privilege Check](04_privacy-controls.md#privilege-check)). It names your database and schemas, and keeps the tables the connection excludes out of the new role where the engine allows it.

---

## Refreshing the Schema Cache

Savvina AI caches the database schema after the first introspection. If you add, rename, or drop tables or columns, refresh the schema:

1. Go to **Connections** → click your connection
2. Click **Refresh Schema**

Refreshing the schema also **invalidates the query cache** for this connection.

---

## Connecting from Inside Docker to a Database on Your Host

Use `host.docker.internal` as the hostname. The `docker-compose.yaml` adds this entry to the backend container's `/etc/hosts` automatically.

---

## Deleting a Connection

Deleting a connection **permanently removes**:
- The encrypted credentials
- All chat sessions and messages for this connection
- All cached queries for this connection
- All verified examples for this connection
- The semantic model for this connection

To delete: **Connections → [your connection] → Delete Connection → Confirm**.
