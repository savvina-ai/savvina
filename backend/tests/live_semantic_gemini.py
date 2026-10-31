# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Live Gemini run of the semantic layer and the chat prompt, inside the backend container.

Opt-in, costs real Gemini tokens, writes to the app database (scratch connections named
``livetest-pg`` / ``livetest-my``, soft-deleted at the end). Covers: the value-mapping guard
(Gemini's proposals are dropped with Include sample values off, kept with it on), the metric
filter lint on a guessed literal, the RELATIONSHIPS header and value mappings in the chat
prompt, the compression warnings under a forced small context window, and Apply Drift.
Every LLM request is recorded.

Setup (repo root, compose stack and the ``test-dbs`` profile up). The scratch databases are
copies, so the script's DDL never touches the samples::

    docker exec sample-postgres psql -U savvina -d postgres -c "CREATE DATABASE livetest_sem"
    docker exec sample-postgres sh -c "pg_dump -U savvina -d savvina_test -n ecommerce -n hr \\
        --no-owner --no-privileges | psql -q -U savvina -d livetest_sem"
    # MySQL: dump sample_delivery into a new livetest_delivery, GRANT ALL on it to savvina

Run (``pg`` and/or ``my``; the ids come from the app database)::

    docker exec -i -e LIVE_GEMINI_PROVIDER_ID=<id> -e LIVE_USER_ID=<id> savvina-backend-1 \\
        sh -c 'set -a; . /app/data/secrets.env; set +a; cd /app && python - pg my' \\
        < backend/tests/live_semantic_gemini.py

Prints one PASS / FAIL / INFO line per check and a final ``@@JSON@@`` document. Drop the
scratch databases afterwards. Rerun from fresh copies: the script adds columns and a table.
"""

# ruff: noqa: E501, E731, E741, B007, N803, N806, S608 — an opt-in live script, not a unit test.

import asyncio
import json
import logging
import os
import sys

import httpx
import openai.resources.chat.completions as oc

GEMINI = os.environ["LIVE_GEMINI_PROVIDER_ID"]  # provider_configs.id of a Gemini provider
USER_ID = os.environ["LIVE_USER_ID"]  # users.id the token is minted for
ONLY = set(sys.argv[1:])  # optional scenario filter: pg my

captured: list[dict] = []
_orig = oc.AsyncCompletions.create


async def _create(self, *a, **kw):
    captured.append({"model": kw.get("model"), "messages": kw.get("messages")})
    return await _orig(self, *a, **kw)


oc.AsyncCompletions.create = _create

log_lines: list[str] = []


class _H(logging.Handler):
    def emit(self, record):
        log_lines.append(record.getMessage())


logging.getLogger("app.semantic.generator").addHandler(_H())
logging.getLogger("app.semantic.generator").setLevel(logging.INFO)

report: list[dict] = []
artifacts: dict = {}


def check(scn, name, ok, detail=""):
    report.append({"scenario": scn, "check": name, "ok": ok, "detail": str(detail)[:600]})
    tag = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  [{tag}] {name}" + (f" — {str(detail)[:260]}" if detail != "" else ""), flush=True)


def scenario(name):
    print(f"\n=== {name}", flush=True)
    return name


class Api:
    def __init__(self, client, token):
        self.c, self.h = client, {"Authorization": "Bearer " + token}

    async def req(self, method, path, body=None, retries=4):
        for i in range(retries):
            start = len(captured)
            r = await self.c.request(method, "/api/v1" + path, json=body, headers=self.h)
            if r.status_code in (429, 503, 504) and i < retries - 1:
                print(f"  {method} {path}: {r.status_code}, retry in 30s", flush=True)
                await asyncio.sleep(30)
                continue
            break
        events = [
            json.loads(l[5:])
            for l in r.text.splitlines()
            if l.startswith("data:") and l[5:].strip().startswith("{")
        ]
        try:
            data = None if events else (r.json() if r.text else None)
        except Exception:
            data = r.text[:2000]
        return {"status": r.status_code, "body": data, "events": events, "calls": captured[start:]}


def payload_text(calls):
    return json.dumps(calls, ensure_ascii=False)


async def generate(api, conn, only=None):
    base = f"/connections/{conn}/semantic"
    init = await api.req("POST", f"{base}/generate/init?provider={GEMINI}")
    assert init["status"] == 200, init
    b = init["body"]
    keys = (
        b["pending_table_keys"] if not only else [k for k in b["pending_table_keys"] if k in only]
    )
    calls, batches = (
        [],
        [keys[i : i + b["batch_size"]] for i in range(0, len(keys), b["batch_size"])],
    )
    for attempt in range(3):
        nxt = []
        for x in batches:
            r = await api.req("POST", f"{base}/generate/batch?provider={GEMINI}", {"table_keys": x})
            calls += r["calls"]
            if r["status"] != 200:
                print("  batch", r["status"], str(r["body"])[:200], flush=True)
                nxt.append(x)
            elif r["body"].get("omitted_table_keys"):
                nxt.append(r["body"]["omitted_table_keys"])
        batches = nxt
        if not batches:
            break
    glob = await api.req("POST", f"{base}/generate/globals?provider={GEMINI}")
    calls += glob["calls"]
    return {"keys": keys, "calls": calls, "globals": glob, "left": batches}


async def chat(api, conn, question, bypass=True):
    r = await api.req(
        "POST",
        "/chat",
        {
            "connection_id": conn,
            "message": question,
            "provider": GEMINI,
            "options": {"bypass_cache": bypass},
        },
    )
    ev = r["events"]
    done = next((e for e in ev if e.get("type") == "done"), {})
    rows = next((e for e in ev if e.get("type") == "row_batch"), None)
    r["sql"] = next((e.get("query") or e.get("sql") for e in ev if e.get("type") == "sql"), None)
    r["done"] = done
    r["rows"] = rows and rows.get("rows")
    r["errors"] = [e for e in ev if e.get("type") == "error"]
    return r


def n_mappings(model):
    return sum(
        len(c.get("value_mappings") or [])
        for t in model["tables"].values()
        for c in t["columns"].values()
    )


async def truth(adapter_cache, source_type, config, sql):
    from app.datasources.registry import create_datasource

    key = (source_type, config["database"])
    if key not in adapter_cache:
        a = create_datasource(source_type)
        await a.connect(config)
        adapter_cache[key] = a
    res = await adapter_cache[key].execute_query(sql, timeout=30, max_rows=100)
    return res.rows


async def main():
    from app.auth.tokens import create_access_token
    from app.datasources.models import PrivacySettings
    from app.main import app
    from app.providers.gemini_provider import GeminiProvider

    token = create_access_token(USER_ID)
    import dataclasses

    base_privacy = dataclasses.asdict(PrivacySettings())
    PG_CFG = {
        "host": "sample-postgres",
        "port": 5432,
        "database": "livetest_sem",
        "username": "savvina",
        "password": "savvina_demo",
        "ssl_mode": "disable",
    }
    MY_CFG = {
        "host": "sample-mysql",
        "port": 3306,
        "database": "livetest_delivery",
        "username": "savvina",
        "password": "savvina_demo",
    }
    cases = {
        "pg": dict(
            src="postgresql",
            cfg=PG_CFG,
            orders="ecommerce.orders",
            col="status",
            excl_schemas=["hr"],
            q="How many orders are delivered?",
            lit="delivered",
            cnt="SELECT COUNT(*) FROM ecommerce.orders WHERE status = 'delivered'",
            values="SELECT DISTINCT status FROM ecommerce.orders",
        ),
        "my": dict(
            src="mysql",
            cfg=MY_CFG,
            orders="livetest_delivery.orders",
            col="status",
            excl_schemas=["sample_delivery"],
            q="How many orders are delivered?",
            lit="delivered",
            cnt="SELECT COUNT(*) FROM livetest_delivery.orders WHERE status = 'delivered'",
            values="SELECT DISTINCT status FROM livetest_delivery.orders",
        ),
    }
    created = []
    adapters: dict = {}
    async with app.router.lifespan_context(app):
        tr = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t", timeout=900) as c:
            api = Api(c, token)
            try:
                for name, k in cases.items():
                    if ONLY and name not in ONLY:
                        continue
                    await run_case(api, name, k, base_privacy, created, adapters, GeminiProvider)
            finally:
                for cid in created:
                    r = await api.req("DELETE", f"/connections/{cid}")
                    print(f"  cleanup: delete connection {cid} -> {r['status']}", flush=True)
                for a in adapters.values():
                    await a.disconnect()
    print("@@JSON@@" + json.dumps({"report": report, "artifacts": artifacts}, default=str))


async def run_case(api, name, k, base_privacy, created, adapters, GeminiProvider):
    S = f"[{name}] "
    privacy = lambda **kw: {
        **base_privacy,
        "excluded_schemas": k["excl_schemas"],
        "include_column_comments": True,
        **kw,
    }
    r = await api.req(
        "POST",
        "/connections",
        {
            "name": f"livetest-{name}",
            "source_type": k["src"],
            "config": k["cfg"],
            "privacy_settings": privacy(include_sample_values=False),
            "execution_mode": "auto_execute",
        },
    )
    assert r["status"] in (200, 201), r
    conn = r["body"]["id"]
    created.append(conn)
    base = f"/connections/{conn}/semantic"
    refresh = await api.req("POST", f"/connections/{conn}/schema/refresh")
    assert refresh["status"] == 200, refresh
    if name == "pg":
        import asyncpg

        cx = await asyncpg.connect(
            host="sample-postgres",
            port=5432,
            user="savvina",
            password="savvina_demo",
            database="livetest_sem",
        )
        try:
            await cx.execute("ALTER TABLE ecommerce.orders ADD COLUMN fulfilment_code char(1)")
            await cx.execute(
                "UPDATE ecommerce.orders SET fulfilment_code = (ARRAY['P','S','D'])[1 + id % 3]"
            )
            await cx.execute(
                "COMMENT ON COLUMN ecommerce.orders.fulfilment_code IS"
                " 'Fulfilment stage code: P = Packed, S = Shipped, D = Delivered'"
            )
        finally:
            await cx.close()
        await api.req("POST", f"/connections/{conn}/schema/refresh")
    enum_members = []
    if name == "my":
        row = await truth(
            adapters,
            k["src"],
            k["cfg"],
            "SELECT COLUMN_TYPE FROM information_schema.columns WHERE table_schema='livetest_delivery' AND table_name='orders' AND column_name='status'",
        )
        import re as _r0

        enum_members = _r0.findall(r"'([^']*)'", str(row[0][0]))
    real_values = [str(x[0]) for x in await truth(adapters, k["src"], k["cfg"], k["values"])]

    # ── 1. generation with sample values OFF ──────────────────────────────────
    s = scenario(S + "G1 generation, Include sample values OFF")
    log_lines.clear()
    g = await generate(api, conn)
    model = (await api.req("GET", base))["body"]
    artifacts[name + "_g1_model"] = model
    check(
        s,
        "every table annotated, no tables left",
        not g["left"] and len(model["tables"]) > 5,
        f"{len(model['tables'])} tables, {len(g['calls'])} LLM calls",
    )
    proposed = [l for l in log_lines if "dropped" in l and "value mapping" in l]
    check(
        s,
        "no value mapping survives with sample values off",
        n_mappings(model) == 0,
        f"{n_mappings(model)} kept; guard dropped on {len(proposed)} column(s) e.g. {proposed[:2]}",
    )
    fc = model["tables"].get(k["orders"], {}).get("columns", {}).get("fulfilment_code")
    if fc is not None:
        check(
            s,
            "coded column (values only in its comment) has no mapping with sample values off",
            not fc.get("value_mappings"),
            fc.get("value_mappings"),
        )
    prop = payload_text([x for x in g["calls"] if "fulfilment_code" in payload_text([x])])
    check(
        s,
        "the comment reached the LLM (so Gemini could propose a mapping)",
        "P = Packed" in prop if name == "pg" else None,
        "",
    )
    check(s, "model stores no common joins", model["common_joins"] == [], model["common_joins"])
    glob_calls = [
        x
        for x in g["calls"]
        if "business_metrics" in payload_text([x]) and "KPIs" in payload_text([x])
    ]
    gt = payload_text(glob_calls)
    check(
        s,
        "globals prompt sent, asks for no common joins",
        bool(glob_calls) and "common_joins" not in gt and "join pattern" not in gt.lower(),
        f"{len(glob_calls)} globals call(s)",
    )
    check(
        s,
        "relationship edges exist with the new required-FK wording",
        model["relationships"]
        and not any(
            "safe in either direction" in (e.get("description") or "")
            for e in model["relationships"]
        ),
        f"{len(model['relationships'])} edges, "
        f"{sum(1 for e in model['relationships'] if e['is_required'])} required",
    )
    check(s, "generation_warnings", None, model["generation_warnings"])

    # ── 2. generation with sample values ON ───────────────────────────────────
    s = scenario(S + "G2 regenerate, Include sample values ON")
    pr = await api.req("PUT", f"/connections/{conn}/privacy", privacy(include_sample_values=True))
    check(s, "privacy updated", pr["status"] == 200, pr["status"])
    await api.req("POST", f"/connections/{conn}/schema/refresh")
    log_lines.clear()
    g = await generate(api, conn)
    model = (await api.req("GET", base))["body"]
    artifacts[name + "_g2_model"] = model
    kept = {
        f"{tk}.{cn}": [m["raw_value"] for m in col["value_mappings"]]
        for tk, t in model["tables"].items()
        for cn, col in t["columns"].items()
        if col.get("value_mappings")
    }
    check(
        s,
        "value mappings now kept for some columns",
        None,
        f"{n_mappings(model)} mappings on {len(kept)} column(s): {list(kept)[:6]}",
    )
    if k["orders"] + "." + k["col"] in kept:
        raws = kept[k["orders"] + "." + k["col"]]
        check(
            s,
            f"{k['orders']}.{k['col']} mapping raw values all exist in the database",
            set(raws) <= set(real_values) | set(enum_members),
            f"mapping {raws} vs db values {real_values} + enum members {enum_members}",
        )
    dropped_after = [l for l in log_lines if "dropped" in l and "value mapping" in l]
    check(s, "guard log lines (columns whose proposal was dropped)", None, len(dropped_after))
    fc = model["tables"].get(k["orders"], {}).get("columns", {}).get("fulfilment_code")
    if fc is not None:
        raws = [m["raw_value"] for m in fc.get("value_mappings", [])]
        check(
            s,
            "coded column: mapping kept now sample values are on (INFO if Gemini proposed none)",
            None,
            raws,
        )
        check(
            s,
            "coded column: every kept raw value exists in the database",
            set(raws) <= {"P", "S", "D"},
            raws,
        )
    check(s, "generation_warnings", None, model["generation_warnings"])

    # ── 3. metric filter lint against the real model ──────────────────────────
    s = scenario(S + "L1 metric filter lint")
    ordkey = k["orders"]
    mappings = [{"raw_value": v, "display_value": v.replace("_", " ").title()} for v in real_values]
    put = await api.req(
        "PUT",
        base,
        {
            "tables": {ordkey: {"columns": {k["col"]: {"value_mappings": mappings}}}},
            "business_metrics": [
                {
                    "name": "Completed orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "aggregation": "count",
                    "description": "hand-written, wrong literal",
                    "filters": [f"orders.{k['col']} = 'completed'"],
                    "related_tables": [ordkey],
                }
            ],
        },
    )
    check(
        s,
        "PUT hand-written mappings and a metric with a guessed literal",
        put["status"] == 200,
        str(put["body"])[:200],
    )
    g = await generate(api, conn)
    model = (await api.req("GET", base))["body"]
    check(s, "globals call returned 200", g["globals"]["status"] == 200, g["globals"]["status"])
    artifacts[name + "_l1_debug"] = {
        "warnings": model["generation_warnings"],
        "mappings": [
            m["raw_value"] for m in model["tables"][ordkey]["columns"][k["col"]]["value_mappings"]
        ],
        "metrics": [
            (m["name"], m.get("filters"), m.get("related_tables"))
            for m in model["business_metrics"]
        ],
    }
    print("  L1 debug:", json.dumps(artifacts[name + "_l1_debug"])[:900], flush=True)
    warns = [w for w in model["generation_warnings"] if "Completed orders" in w]
    check(
        s,
        "lint reports the guessed 'completed' (not a real value)",
        len(warns) == 1 and "'completed'" in warns[0],
        warns,
    )
    n = (
        await truth(
            adapters,
            k["src"],
            k["cfg"],
            f"SELECT COUNT(*) FROM {ordkey} WHERE {k['col']} = 'completed'",
        )
    )[0][0]
    check(s, "the database confirms that value matches no row", int(n) == 0, n)
    check(
        s,
        "the hand-written metric survived the regenerate",
        any(m["name"] == "Completed orders" for m in model["business_metrics"]),
    )
    llm_filters = [f for m in model["business_metrics"] for f in m.get("filters", [])]
    check(s, "metric filters Gemini wrote (INFO)", None, llm_filters[:8])
    # fix it by hand; the stale warning persists until the next regenerate (known limit)
    await api.req(
        "PUT",
        base,
        {
            "business_metrics": [
                {
                    "name": "Completed orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "aggregation": "count",
                    "description": "fixed",
                    "filters": [f"orders.{k['col']} = '{k['lit']}'"],
                    "related_tables": [ordkey],
                }
            ]
        },
    )
    g = await generate(api, conn)
    model = (await api.req("GET", base))["body"]
    check(
        s,
        "after fixing the literal the lint is silent for that metric",
        not [w for w in model["generation_warnings"] if "Completed orders" in w],
        model["generation_warnings"],
    )

    # ── 4. chat prompt ────────────────────────────────────────────────────────
    s = scenario(S + "C1 chat prompt and answer")
    r = await chat(api, conn, k["q"])
    sys_prompt = r["calls"][0]["messages"][0]["content"] if r["calls"] else ""
    artifacts[name + "_c1_prompt"] = sys_prompt
    check(
        s,
        "answered",
        r["done"].get("status") == "executed" and not r["errors"],
        (r["sql"], r["errors"]),
    )
    check(
        s,
        "prompt has the RELATIONSHIPS header with the LEFT JOIN advice",
        "RELATIONSHIPS (INNER JOIN marks a NOT NULL foreign key" in sys_prompt
        and "put that filter in the ON clause" in sys_prompt,
    )
    import re as _re

    check(
        s,
        "prompt carries the user's value mapping as Label='raw' [SQL values: ...]",
        bool(_re.search(rf"{k['lit'].title()}='{k['lit']}'.*\[SQL values:", sys_prompt)),
        [l[:200] for l in sys_prompt.splitlines() if "[SQL values" in l][:2],
    )
    check(s, "prompt never renders common joins", "COMMON JOINS" not in sys_prompt.upper())
    check(
        s,
        "no compression warning on an uncompressed prompt",
        r["done"].get("warning") is None,
        r["done"].get("warning"),
    )
    t = (await truth(adapters, k["src"], k["cfg"], k["cnt"]))[0][0]
    got = r["rows"][0][0] if r["rows"] else None
    check(
        s,
        "Gemini's count equals the database's",
        got is not None and int(got) == int(t),
        f"{got} vs {t}; sql={r['sql']}",
    )
    artifacts[name + "_c1_prompt_len"] = len(sys_prompt)

    # ── 5. compression warnings ───────────────────────────────────────────────
    s = scenario(S + "W1 compression warnings with a forced small context window")
    full_len = len(sys_prompt)
    max_out = 4096  # the provider config's max_tokens
    outcomes = {}
    original = GeminiProvider.context_window
    try:
        for frac in (0.9, 0.6, 0.4, 0.25, 0.1):
            # budget_chars = (ctx - max_out) * chars_per_token(4) * safety; solve for ctx
            from app.services.pipeline import _CONTEXT_BUDGET_SAFETY_FACTOR as F

            target = int(full_len * frac)
            GeminiProvider.context_window = int(target / (4.0 * F)) + max_out
            r = await chat(api, conn, f"{k['q']} (variant {frac})")
            sp = r["calls"][0]["messages"][0]["content"] if r["calls"] else ""
            w = r["done"].get("warning")
            kind = (
                "dropped"
                if w and "left out of it" in w
                else "compacted"
                if w and "compacted" in w
                else "none"
            )
            outcomes[frac] = (kind, len(sp), r["done"].get("status"))
            print(
                f"  budget {frac:.2f} of full prompt -> warning={kind}, prompt {len(sp)} chars, "
                f"status={r['done'].get('status')}",
                flush=True,
            )
            artifacts[f"{name}_w1_{frac}"] = {"warning": w, "sql": r["sql"], "prompt_len": len(sp)}
    finally:
        GeminiProvider.context_window = original
    kinds = [v[0] for v in outcomes.values()]
    check(
        s,
        "the loosest budget needs no (or only a compaction) warning",
        kinds[0] in ("none", "compacted"),
        kinds,
    )
    check(s, "a mid budget shows the 'compacted' warning", "compacted" in kinds, kinds)
    check(s, "a tight budget shows the 'left out' warning", "dropped" in kinds, kinds)
    check(
        s,
        "the warning only ever appears together with a shorter prompt",
        all(v[1] <= full_len for v in outcomes.values() if v[0] != "none"),
        outcomes,
    )
    check(
        s,
        "the chat still answers under compression",
        all(v[2] in ("executed", "generated") for v in outcomes.values()),
        outcomes,
    )

    # ── 6. drift: apply a new column and a new table (PG only: scratch DDL) ──
    if name == "pg":
        s = scenario(S + "D1 drift + apply with Gemini")
        import asyncpg

        dsn = dict(
            host="sample-postgres",
            port=5432,
            user="savvina",
            password="savvina_demo",
            database="livetest_sem",
        )
        cx = await asyncpg.connect(**dsn)
        try:
            await cx.execute(
                "ALTER TABLE ecommerce.orders ADD COLUMN fulfilment_state varchar(20)"
                " DEFAULT 'queued'"
            )
            await cx.execute(
                "UPDATE ecommerce.orders SET fulfilment_state ="
                " CASE WHEN id % 2 = 0 THEN 'queued' ELSE 'packed' END"
            )
            await cx.execute(
                "CREATE TABLE ecommerce.loyalty_members (id serial PRIMARY KEY,"
                " customer_id int REFERENCES ecommerce.customers(id) NOT NULL,"
                " tier varchar(20) NOT NULL, joined_at date)"
            )
            await cx.execute(
                "INSERT INTO ecommerce.loyalty_members (customer_id, tier, joined_at)"
                " SELECT id, CASE WHEN id % 3 = 0 THEN 'gold' ELSE 'silver' END,"
                " CURRENT_DATE FROM ecommerce.customers LIMIT 60"
            )
        finally:
            await cx.close()
        await api.req("POST", f"/connections/{conn}/schema/refresh")
        d = await api.req("GET", f"{base}/drift")
        artifacts["pg_d1_drift"] = d["body"]
        warns = d["body"].get("warnings") if isinstance(d["body"], dict) else None
        check(s, "drift reports the new table and the new column", bool(warns), warns)
        # sample values are ON: a drift apply may keep mappings for the new column
        before = len((await api.req("GET", base))["body"]["common_joins"])
        ap = await api.req("POST", f"{base}/apply-drift?provider={GEMINI}")
        check(s, "apply-drift succeeded", ap["status"] == 200, str(ap["body"])[:300])
        if ap["status"] == 200:
            m = ap["body"]["semantic_model"]
            artifacts["pg_d1_apply"] = ap["body"]["summary"]
            check(s, "new table annotated", "ecommerce.loyalty_members" in m["tables"])
            col = m["tables"]["ecommerce.orders"]["columns"].get("fulfilment_state")
            check(s, "new column annotated", col is not None, col and col["display_name"])
            check(
                s,
                "apply-drift added no common joins",
                len(m["common_joins"]) == before,
                (before, len(m["common_joins"])),
            )
            check(s, "apply-drift summary", None, ap["body"]["summary"])
        # and again with sample values OFF for the second new column
        await api.req("PUT", f"/connections/{conn}/privacy", privacy(include_sample_values=False))
        cx = await asyncpg.connect(**dsn)
        try:
            await cx.execute(
                "ALTER TABLE ecommerce.orders ADD COLUMN priority_band varchar(10) DEFAULT 'normal'"
            )
        finally:
            await cx.close()
        await api.req("POST", f"/connections/{conn}/schema/refresh")
        ap = await api.req("POST", f"{base}/apply-drift?provider={GEMINI}")
        check(
            s,
            "second apply-drift (sample values off) succeeded",
            ap["status"] == 200,
            str(ap["body"])[:300],
        )
        if ap["status"] == 200:
            col = ap["body"]["semantic_model"]["tables"]["ecommerce.orders"]["columns"].get(
                "priority_band"
            )
            check(
                s,
                "new column annotated, no value mappings with sample values off",
                col is not None and not col.get("value_mappings"),
                col,
            )


asyncio.run(main())
