# Quick Start Guide

This guide takes you from zero to your first natural-language SQL query in under 10 minutes using a **free** LLM provider.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker | 24+ | Docker Desktop or Docker Engine |
| Docker Compose | v2 | Bundled with Docker Desktop |
| RAM | 4 GB | 8 GB recommended for Ollama |
| Internet | Required | To pull Docker images and call LLM APIs |

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

You need at least one LLM provider configured before you can query data. You can also add providers later through the UI — no restart required.

### Option A — Groq (Recommended)

Groq offers the most generous free tier with the fastest inference.

1. Sign up at **https://console.groq.com** (no credit card required)
2. Go to **API Keys → Create API Key**
3. Add to `.env`:
   ```
   GROQ_API_KEY=gsk_...
   ```

### Option B — Google Gemini

1. Sign up at **https://aistudio.google.com** (requires a Google account)
2. Click **Get API Key → Create API key**
3. Add to `.env`:
   ```
   GEMINI_API_KEY=AIza...
   ```

### Option C — Anthropic Claude (Paid)

```
ANTHROPIC_API_KEY=sk-ant-...
```

### Option D — OpenAI (Paid)

```
OPENAI_API_KEY=sk-...
```

> You can add more providers later through the **Settings** page in the UI without restarting.

---

## Step 5 — Set Volume Permissions (First Run Only)

On Linux and WSL, run the init container once to create the volume directories with the correct ownership:

```bash
docker compose run --rm init-permissions
```

This step is safe to skip on macOS and Windows Docker Desktop.

---

## Step 6 — Generate TLS Certificates (Required)

Nginx serves the frontend over HTTPS and will not start without a certificate. Use [mkcert](https://github.com/FiloSottile/mkcert) to generate locally-trusted certs in one command.

### Install mkcert

**macOS**
```bash
brew install mkcert
```

**Windows (PowerShell)**
```powershell
winget install FiloSottile.mkcert
```

**Debian / Ubuntu / WSL**
```bash
sudo apt install libnss3-tools
curl -Lo mkcert "$(curl -s https://api.github.com/repos/FiloSottile/mkcert/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d '"' -f 4)"
chmod +x mkcert && sudo mv mkcert /usr/local/bin/
```

**RHEL / Fedora / CentOS**
```bash
sudo dnf install nss-tools
curl -Lo mkcert "$(curl -s https://api.github.com/repos/FiloSottile/mkcert/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d '"' -f 4)"
chmod +x mkcert && sudo mv mkcert /usr/local/bin/
```

### Install the local CA (once per machine)

```bash
mkcert -install
```

> **WSL users:** the command above installs the CA into the Linux certificate store, which is enough for `curl` and server-side tools. To make Chrome/Edge/Firefox on *Windows* trust the cert without a warning, you also need to run `mkcert -install` once from a **Windows** Command Prompt or PowerShell (after installing mkcert for Windows via `winget`).

### Generate the certificates

```bash
mkdir -p volumes/certs
cd volumes/certs
mkcert localhost 127.0.0.1
cd ../..
```

This produces `localhost+1.pem` and `localhost+1-key.pem` — exactly the filenames Nginx expects.

**Accessing from a custom hostname** (e.g. a corporate server or another machine on your network): add the hostname to the mkcert command:

```bash
mkcert localhost 127.0.0.1 your-hostname.example.com
```

Then update `CORS_ORIGINS` in `.env` to include that origin:

```bash
CORS_ORIGINS=["https://localhost:3000","https://your-hostname.example.com:3000"]
```

---

## Step 7 — Start the Stack

```bash
docker compose up --build
```

The first build downloads Docker base images and installs Python/Node packages — expect 3–5 minutes. Subsequent starts take about 10–20 seconds.

> **Speed tip:** The build downloads the fastembed ONNX embedding model from HuggingFace. Anonymous downloads are rate-limited and can slow or fail the build. Setting a free HuggingFace token in `.env` avoids the limit:
> ```
> HF_TOKEN=hf_...   # huggingface.co → Settings → Access Tokens (read-only token)
> ```
> The token is only used at build time and is never included in the final image.

Wait until you see all services healthy:

```
✔ Container savvina-backend-1    Healthy
✔ Container savvina-frontend-1   Started
```

---

## Step 8 — Create Your Admin Account

Navigate to **https://localhost:3000** (or `https://<your-hostname>:<APP_PORT>` if you changed `APP_PORT` or are accessing from a custom hostname).

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

## Step 9 — Configure Your LLM Provider

If you skipped the setup wizard, or want to add more providers:

1. Click **Settings** in the left sidebar
2. Click **Providers → Add Provider**
3. Select your provider type (e.g., **Groq**)
4. Enter your API key and select a model (e.g., `llama-3.3-70b-versatile`)
5. Click **Test** to verify connectivity
6. Click **Save**

See [LLM Providers](../user-guide/06_llm-providers.md) for a full list of supported providers and recommended models.

---

## Step 10 — Connect to a Database

If you skipped the setup wizard, or want to add more connections:

1. Click **Connections** in the left sidebar
2. Click **New Connection**
3. Select your data source type (e.g., **PostgreSQL**)
4. Fill in the connection form

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

## Step 11 — Ask Your First Question

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

## Step 12 — Generate a Semantic Model (Optional but Recommended)

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
