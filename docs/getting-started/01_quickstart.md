# Quick Start Guide

This guide takes you from zero to your first natural-language SQL query in under 10 minutes using a **free** LLM provider.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker | 24+ | [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS, Windows, Linux) or Docker Engine + [CLI plugin](https://docs.docker.com/compose/install/) on Linux servers |
| Docker Compose | v2 | Bundled with Docker Desktop; installed separately as a CLI plugin on plain Docker Engine |
| RAM | 4 GB | 8 GB recommended for Ollama |
| Internet | Required | To pull Docker images and call LLM APIs |

Don't have Docker yet? Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) for your OS, then verify it's running:

```bash
docker --version          # Docker version 24.x or newer
docker compose version    # Docker Compose version v2.x
```

On Linux without Docker Desktop, follow the [Docker Engine install guide](https://docs.docker.com/engine/install/) for your distribution, then install the [Compose plugin](https://docs.docker.com/compose/install/linux/) separately — the standalone `docker-compose` (v1, with a hyphen) is not supported.

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/savvina-ai/savvina
cd savvina
```

---

## Step 2 — Create the Environment File

```bash
cp .env.example .env
```

---

## Step 3 — Generate Required Secrets

### ENCRYPTION_KEY and JWT_SECRET_KEY (Docker — auto-generated)

When running via Docker Compose, **both keys are generated automatically on first boot** and persisted to `/app/data/secrets.env` inside the container's data volume. You do not need to set them manually — the container logs confirm generation:

```
[entrypoint] Generated new ENCRYPTION_KEY → /app/data/secrets.env
[entrypoint] Generated new JWT_SECRET_KEY → /app/data/secrets.env
```

> **Important:** Back up `ENCRYPTION_KEY` from the volume after the first boot. Losing it makes all stored database credentials and API keys permanently unreadable.

If you already have an `ENCRYPTION_KEY` or `JWT_SECRET_KEY` in `.env` from a previous install, the entrypoint migrates them to `secrets.env` automatically on the next start — then you can safely remove them from `.env`.

### ENCRYPTION_KEY and JWT_SECRET_KEY (bare-metal / non-Docker)

If running the backend outside of Docker, generate the keys manually and add them to `.env`:

```bash
# Fernet encryption key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# JWT secret key (at least 32 characters)
python -c "import secrets; print(secrets.token_hex(32))"
```

Add both to `.env`:
```
ENCRYPTION_KEY=your-fernet-key-here
JWT_SECRET_KEY=your-jwt-secret-here
```

### Database passwords

The Docker Compose stack starts several backing services that each need a password. Generate strong random values and fill them in `.env`:

```bash
# Run once per password you need
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

| Variable | Service |
|---|---|
| `APP_DB_PASSWORD` | Internal app PostgreSQL database |
| `SAMPLE_POSTGRES_PASSWORD` | Bundled sample PostgreSQL database |
| `SAMPLE_MYSQL_ROOT_PASSWORD` | MySQL root password |
| `SAMPLE_MYSQL_PASSWORD` | MySQL application user password |

---

## Step 4 — Get a Free LLM API Key

You need at least one LLM provider configured before you can query data. API keys are entered in the UI — in the setup wizard after your first login, or at any time under **Settings → LLM Providers** — and stored encrypted in the app database. They are **not** read from `.env`, so nothing here goes in a config file; just have the key ready.

### Option A — Groq (Recommended)

Groq offers the most generous free tier with the fastest inference.

1. Sign up at **https://console.groq.com** (no credit card required)
2. Go to **API Keys → Create API Key**
3. Copy the key (begins with `gsk_`)

### Option B — Google Gemini

1. Sign up at **https://aistudio.google.com** (requires a Google account)
2. Click **Get API Key → Create API key**
3. Copy the key (begins with `AIza`)

### Option C — Anthropic Claude (Paid)

Create a key at **https://console.anthropic.com** (begins with `sk-ant-`).

### Option D — OpenAI (Paid)

Create a key at **https://platform.openai.com/api-keys** (begins with `sk-`).

> You can add more providers later through the **Settings** page in the UI without restarting.

---

## Step 5 — Set Volume Permissions (First Run Only)

On Linux and WSL, run the init container once to create the volume directories with the correct ownership:

```bash
docker compose run --rm init-permissions
```

This step is safe to skip on macOS and Windows Docker Desktop.

---

## Step 6 — Start the Stack

**Option A — pre-built images (no compiling):**

```bash
docker compose pull
docker compose up --no-build
```

Multi-arch images (`linux/amd64`, `linux/arm64`) are published to Docker Hub as `savvinaai/savvina-backend` and `savvinaai/savvina-frontend`. `latest` is the newest release; set `SAVVINA_IMAGE_TAG=v2.0.0` in `.env` to pin a release. First start takes about a minute (image download plus database migrations).

**Option B — build from source** (needed if you changed the code):

```bash
docker compose up --build
```

The first build downloads Docker base images and installs Python/Node packages — expect 3–5 minutes. Subsequent starts take about 10–20 seconds.

Backend code changes take effect on the next `docker compose up --build`. For hot-reload without rebuilding, opt into `docker-compose.dev.yaml` — see [Development Overrides](../infrastructure/docker.md#development-overrides).

> **Speed tip:** The build downloads the fastembed ONNX embedding model from HuggingFace. Anonymous downloads are rate-limited and can slow or fail the build. Setting a free HuggingFace token in `.env` avoids the limit:
> ```
> HF_TOKEN=hf_...   # huggingface.co → Settings → Access Tokens (read-only token)
> ```
> The token is mounted as a BuildKit secret for the single download step only, so it is never recorded in any image layer or config.

Wait until you see all services healthy:

```
✔ Container savvina-backend-1    Healthy
✔ Container savvina-frontend-1   Started
```

---

## Step 7 — Create Your Admin Account

Navigate to **http://localhost:3000** (or `http://<private-LAN-IP>:<APP_PORT>` — e.g. `http://192.168.1.50:3000` — if you changed `APP_PORT` or are reaching it from another machine). Do **not** use a custom hostname over plain HTTP; as the note below explains, the backend refuses to start with one as a CORS origin.

> **Reaching the UI at anything other than `http://localhost:3000`?** Set `CORS_ORIGINS` in `.env` first, or login will fail with a `403 Origin not allowed`. The backend only accepts requests whose browser `Origin` matches this list, and it defaults to `["http://localhost:3000"]` alone — a LAN IP, a different hostname, or even `http://127.0.0.1:3000` all count as different origins.
>
> Set it to the exact origin (scheme + host + port) you'll open in the browser:
>
> ```bash
> # Reaching the UI from another machine on your LAN by IP, e.g. http://192.168.1.50:3000
> # (also fine for other private ranges: 10.x.x.x, 172.16-31.x.x)
> CORS_ORIGINS=["http://192.168.1.50:3000"]
> ```
>
> A custom hostname over plain HTTP (e.g. `http://savvina.local:3000`) is **not supported** — the backend refuses to start rather than accept it as a CORS origin. Either use the machine's private IP address as the origin instead, or put a TLS-terminating reverse proxy in front and use its `https://` origin (see [Deployment → Configure HTTPS](../administration/deployment.md#5-configure-https)):
>
> ```bash
> CORS_ORIGINS=["https://savvina.example.com"]
> ```
>
> `CORS_ORIGINS` is baked in at process startup, so recreate the backend after editing it (`restart` alone reuses the old environment):
>
> ```bash
> docker compose up -d backend
> ```

On first boot you will see a **Create Admin Account** screen. Fill in:

1. **Organisation name** — a display name for your Savvina instance
2. **Admin account** — your email and a strong password (minimum 12 characters, at least one uppercase letter, one digit, and one special character)

After your account is created, a short **Setup Wizard** walks you through two optional steps:

| Step | What it does |
|---|---|
| Connect a database | Add your first data source connection |
| Configure an LLM | Choose and configure a provider (Claude, Groq, Gemini, Ollama, etc.) |

Click **Skip** on any step to go straight to the dashboard.

> The account creation screen is only shown **once**. After the first account is created, the registration endpoint is permanently closed.

---

## Step 8 — Configure Your LLM Provider

If you skipped the setup wizard, or want to add more providers:

1. Click **Settings** in the left sidebar
2. Stay on the **LLM Providers** tab and click **+ Add \<Provider\> config** for the provider you want (e.g., **+ Add Groq config**). For a service without its own entry — OpenRouter, Together.ai, a self-hosted endpoint — use **+ Add Custom Provider** instead.
3. Enter your API key, then click **Fetch Models** and pick a model (e.g., `llama-3.3-70b-versatile`)
4. Click **Test** to verify connectivity
5. Click **Add** to save the config

See [LLM Providers](../user-guide/06_llm-providers.md) for a full list of supported providers and recommended models.

---

## Step 9 — Connect to a Database

If you skipped the setup wizard, or want to add more connections:

1. Click **Connections** in the left sidebar
2. On the **Connect a Data Source** screen, select your data source type (e.g., **PostgreSQL**)
3. Fill in the connection form

To connect to the **bundled sample PostgreSQL database** that comes with the Docker stack:

| Field | Value |
|---|---|
| Name | `Sample PostgreSQL` |
| Host | `sample-postgres` |
| Port | `5432` |
| Database | `savvina_test` |
| Username | `savvina` |
| Password | *(value of `SAMPLE_POSTGRES_PASSWORD` from your `.env`)* |
| SSL Mode | `disable` |

5. Click **Test Connection** — you should see "Connected successfully"
6. Click **Save Connection**

> When connecting to a database running on your **host machine** outside Docker, use `host.docker.internal` instead of `localhost`.

For MySQL connections, see [Connecting to Data](../user-guide/02_connecting-to-data.md).

---

## Step 10 — Ask Your First Question

1. Click **Chat** in the left sidebar
2. Select your saved connection from the connection dropdown
3. Select your LLM provider from the provider dropdown
4. Type a question, for example:
   - *"How many customers do we have?"*
   - *"Show me the top 5 products by revenue"*
   - *"What were the total sales last month?"*
5. Press **Enter** or click **Send**

Savvina AI will:
1. Check the query cache (instant response if cached)
2. Build a prompt with your schema and semantic context
3. Call the LLM to generate a SQL query
4. Validate the query (read-only check)
5. Execute the query against your database
6. Return the results as a formatted table

---

## Step 11 — Generate a Semantic Model (Optional but Recommended)

The semantic model translates cryptic column names (like `cx_tp_cd`) into plain English (like "Customer Type: E=Enterprise, S=SMB"). It dramatically improves query accuracy.

1. Go to **Connections** → click your connection name
2. Click the **Semantic Model** tab
3. Click **Generate** (uses your active LLM provider)
4. Review the generated descriptions and click **Save**

---

## What's Next?

| Topic | Link |
|---|---|
| All supported data sources | [Connecting to Data](../user-guide/02_connecting-to-data.md) |
| Understanding execution modes | [Execution Modes](../user-guide/03_execution-modes.md) |
| Controlling what the LLM sees | [Privacy Controls](../user-guide/04_privacy-controls.md) |
| Full environment variable reference | [Configuration](02_configuration.md) |
| Production deployment | [Deployment Guide](../administration/deployment.md) |
| Using Ollama for local inference | Start with `docker compose --profile local-llm up` |
