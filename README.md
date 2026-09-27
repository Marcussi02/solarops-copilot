# SolarOps Copilot

[![CI](https://github.com/Marcussi02/solarops-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Marcussi02/solarops-copilot/actions/workflows/ci.yml)
[![Deploy](https://github.com/Marcussi02/solarops-copilot/actions/workflows/deploy.yml/badge.svg)](https://github.com/Marcussi02/solarops-copilot/actions/workflows/deploy.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-Lambda%20·%20SQS%20·%20API%20Gateway-FF9900?logo=amazonwebservices&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)

A serverless data platform and **tool-calling AI copilot** for **utility-scale solar farm operations**, running on **live public data**:

- **AEMO NEMWeb**: 5-minute output (MW) of every generating unit in Australia's National Electricity Market, including more than 100 solar farms
- **Open Electricity**: the facility registry (farm names, regions, coordinates, capacity)
- **Open-Meteo**: current irradiance and weather at each farm

It answers the core operations question, **is each farm producing what the weather says it should?**, over a REST API or in plain English:

```bash
curl -s -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '{"question": "How did Darlington Point do in the last 8 hours?"}' "$API/v1/ask"
```

Real response from a run on live AEMO data (26 Sep 2026):

```json
{
  "answer": "Darlington Point (NSW1, 324.0 MW) produced 792.8 MWh in the last 8 h, peaking at 130.4 MW. Average performance index 95% (lowest 62%).",
  "tool": "facility_performance",
  "args": {"facility": "DARLSF", "hours": 8},
  "data": {"facility": {...}, "summary": {...}, "hourly": [...]},
  "provider": "rules",
  "fallback_reason": null,
  "latency_ms": 16
}
```

The same run answered *"How did the fleet do in the last 8 hours?"* with **37,907.7 MWh from 114 farms across NSW, QLD, VIC and SA**. It ingested 96 five-minute files (11,712 readings) in about 100 seconds.

> **Status:** Phase 1 (ingestion) ✅ · Phase 2 (API, copilot, evals, AWS deploy) ✅ · Phase 3 (RAG over an O&M knowledge base, with citations and retrieval evals) ✅ · Phase 4 (MCP server) ✅ · Curtailment-aware scoring ✅ · Phase 5 (observability) ✅ · Public live dashboard ✅. See the [Roadmap](#roadmap).

## Architecture

```
                   ┌──────────────────────┐
 EventBridge 5 min │ PollFunction         │ lists NEMWeb, skips files already ingested
 ─────────────────►│                      ├──────────────┐
                   └──────────────────────┘              ▼
                                                  ┌─────────────┐  5 failures  ┌─────┐
                                                  │ SQS queue   ├─────────────►│ DLQ │─► alarm ─► email
                                                  └──────┬──────┘              └─────┘
                                                         ▼  batch of 5, max 2 concurrent
                   ┌──────────────────────┐       ┌──────────────────────┐
 NEMWeb (zip) ────►│ S3 raw archive       │◄──────┤ IngestFunction       │
                   │ date=YYYY-MM-DD/     │       │ download → parse →   │
                   └──────────────────────┘       │ upsert (1 txn)       │
                                                  └──────────┬───────────┘
 EventBridge daily ─► RegistryFunction ──┐                   ▼
 EventBridge 15 min ► WeatherFunction ───┼────────► PostgreSQL (Supabase)
 EventBridge 6 h ───► DispatchFunction ──┤          solar_units · scada_readings · weather_obs
 EventBridge daily ─► RetentionFunction ─┘          region_prices · unit_dispatch · ingested_files
                                                    facility_performance (view, with status)
                                                         ▲ read-only session, 5 s timeout
 client ─► API Gateway (HTTP API, throttled) ─► ApiFunction (FastAPI + Mangum)
                                                   ├─ /v1/* read endpoints (60 s cache)
                                                   └─ /v1/ask ─► copilot ─► model provider
                                                                  (rules | Bedrock | OpenAI)
```

## The copilot

```
question ──► provider.choose_tool ──► validate (pydantic bounds) ──► fixed SQL ──► provider.summarise
                     │ error / invalid proposal                                        │ error
                     └──────────────────────► deterministic router ◄──────────────────┘
```

- **Tool calling over a fixed query catalogue, not text-to-SQL.** The model picks one of five tools (`underperformers`, `facility_performance`, `fleet_summary`, `find_facilities`, and `search_docs` for the [knowledge base](#knowledge-base-rag)) and proposes arguments. It never writes SQL, so it can't touch tables it shouldn't or run an expensive query.
- **Every argument is validated** by a pydantic model with hard bounds (hours 1–168, known regions only, unknown fields rejected). A bad proposal is recorded and handed to the fallback, never executed.
- **Answers are grounded.** The summarising model only sees the tool's JSON result and is told to use only those numbers. The response returns the tool, arguments and raw data too, so every answer can be audited.
- **Pluggable providers.** `LLM_PROVIDER=none` (default) uses a deterministic router with template answers: free, offline, and the floor that CI tests. `bedrock` uses the Amazon Bedrock Converse API with native tool use (Nova Lite by default). `openai` uses Chat Completions. Switching is one parameter.
- **Graceful degradation.** A provider outage, timeout or invalid tool call falls back to the router for that step. `provider` and `fallback_reason` in the response keep this visible.
- **The database session is read-only** with a 5-second statement timeout. That's defence in depth under the fixed-query design.

### Evals

[`evals/golden.json`](evals/golden.json) holds 34 real-world phrasings, each mapped to the tool call it should produce: the right farm, window, region and threshold. Routing is where copilots fail in practice (the wrong farm, or a region silently dropped), and it can be scored without a database.

```bash
PYTHONPATH=src python -m solarops.evals                     # router: CI gate, must be 100%
PYTHONPATH=src python -m solarops.evals --provider bedrock  # score a real model (accuracy, p50/p95)
```

CI fails if the router's score drops below 100%. The same harness scores any LLM provider, so model changes are measured, not guessed.

## Knowledge base (RAG)

Live numbers say *that* a farm is low; operators also need to know *why* and *what to check*. The copilot answers those questions from an operations knowledge base in [`src/solarops/knowledge`](src/solarops/knowledge): 10 documents covering curtailment and dispatch, inverter faults, trackers, soiling and degradation, clipping and export limits, irradiance, NEM data, how the performance index works, an alarm-triage runbook and maintenance planning.

```
question ──► router / model picks search_docs ──► BM25 (+ optional embeddings, fused with RRF)
         ──► top passages, numbered [1]..[k] ──► answer that must cite them ──► citation check
                                                         │ no citation / unknown [n]
                                                         └──► extractive answer from the passages
```

- **Section-level chunks with stable IDs** (`curtailment#confirming-curtailment`). A section is one topic, which is the right unit for a citation, and stable IDs let evals and answers point at exactly the same thing.
- **BM25 keyword retrieval by default**, in process and dependency-free: the index builds in about a millisecond at cold start and needs no vector database or API key. Operator shorthand (PR, POA, GHI, MLF, PID) is expanded at query time.
- **Optional hybrid retrieval.** `EMBEDDINGS_PROVIDER=bedrock` (Titan Text Embeddings v2) or `openai` adds dense vectors, fused with the keyword ranking by reciprocal rank fusion. At a few dozen chunks, brute-force cosine in memory beats running pgvector. If embeddings fail, search degrades to keywords and reports `"method": "bm25"`.
- **Citations are enforced, not requested.** A model answer that cites nothing, or cites a passage it wasn't given, is rejected and replaced by an extractive answer built from the passages, with `fallback_reason` explaining why.
- **Data plus guidance.** Diagnostic questions about live data, such as *"Why are farms in NSW underperforming right now?"*, run the data tool and attach the relevant runbook sections as sources.

Real answer from the offline router (no model, no API key):

```json
{
  "question": "Why would a solar farm be dispatched down when prices go negative?",
  "answer": "Solar farms typically offer energy at low or negative prices because their fuel is free, but many are not willing to generate at any price. When the regional price falls below the price at which a farm has offered its output, the farm is dispatched down and curtails itself. This is common around midday in South Australia and Victoria, when rooftop and utility solar exceed demand and prices go negative. [1]",
  "tool": "search_docs",
  "sources": [{"n": 1, "id": "curtailment#economic-curtailment-at-negative-prices", "title": "Curtailment and dispatch in the NEM", "section": "Economic curtailment at negative prices"}, "..."]
}
```

### Retrieval evals

[`evals/retrieval.json`](evals/retrieval.json) holds 36 operator questions, written before the retriever was tuned and phrased differently from the documents, each mapped to the sections that answer it.

```bash
PYTHONPATH=src python -m solarops.rag.evaluate                        # BM25: CI gate, hit@3 >= 0.9
PYTHONPATH=src python -m solarops.rag.evaluate --embeddings bedrock   # score hybrid retrieval
PYTHONPATH=src python -m solarops.cli docs "tracker rows stuck on a windy day"
```

| Retriever | hit@1 | hit@3 | MRR | p50 latency |
|---|---|---|---|---|
| BM25 (default) | 0.86 | 1.00 | 0.93 | < 0.1 ms |

The first untuned run scored hit@3 0.97. The one miss exposed a stemming bug ("prices" and "price" didn't match), and fixing it took hit@3 to 1.00. A test also checks that every question in the set routes to `search_docs`, so knowledge questions never fall through to a data query.

## MCP server

The same tool catalogue is exposed over the [Model Context Protocol](https://modelcontextprotocol.io), so any MCP-capable assistant or IDE can query live farm data and the knowledge base directly:

```json
{
  "mcpServers": {
    "solarops": {
      "command": "python",
      "args": ["-m", "solarops.mcp_server"],
      "env": {"PYTHONPATH": "src", "DATABASE_URL": "postgresql://..."}
    }
  }
}
```

- **One tool registry, three interfaces.** REST, the copilot and MCP all call `copilot/tools.py`, so bounds, validation and the read-only, 5-second-timeout session are identical everywhere.
- **Tools, not the copilot.** An MCP client is already a language model, so it gets the bounded tools and does its own reasoning, rather than a model talking to a model.
- **Two validation layers.** The MCP SDK checks arguments against each tool's JSON schema, then pydantic enforces the same bounds before any query runs. Invalid calls come back as errors and are never executed.
- `search_docs` needs no database, so the knowledge base works even when Postgres is unreachable.

## API

OpenAPI docs are served at `/docs`. The `/v1` routes need an `x-api-key` header. The `/public` routes and `/dashboard` don't: they serve aggregates only, from the same query catalogue with fixed limits. Responses are cached for 5 minutes on the server and by clients (`Cache-Control: max-age=300`), and API Gateway throttles them per route (burst 10, 5 requests/s).

Live deployment: [interactive docs](https://h12xi690he.execute-api.ap-southeast-2.amazonaws.com/docs) · [health check](https://h12xi690he.execute-api.ap-southeast-2.amazonaws.com/health)

![OpenAPI docs for the SolarOps Copilot API](docs/api-docs.png)

| Route | What it returns |
|---|---|
| `GET /health` | Liveness and data freshness (`data_lag_minutes`). Public. |
| `GET /dashboard` | Live fleet dashboard: one self-contained HTML page, no external requests. Public. |
| `GET /public/status` | Latest interval, data lag, farm and unit counts. Public. |
| `GET /public/fleet?hours=24` | Energy and average performance per NEM region. Public. |
| `GET /public/underperformers` | Up to 20 farms below expected output at the latest interval. Public. |
| `GET /public/curtailed` | Up to 20 farms held back by dispatch caps or negative prices. Public. |
| `GET /v1/status` | Pipeline counters: units, files, readings, latest interval |
| `GET /v1/facilities?region=` | Solar farms with capacity and location |
| `GET /v1/facilities/{code}?hours=24` | Energy, peak, performance index and hourly profile for one farm |
| `GET /v1/underperformers?threshold=0.6&region=` | Farms below expected output at the latest interval |
| `GET /v1/curtailed?region=` | Farms held back by dispatch caps (`curtailed`, MW lost) or negative prices (`likely_curtailed`) |
| `GET /v1/fleet?hours=24&region=` | Energy and average performance per NEM region |
| `GET /v1/docs` | Knowledge-base documents and their sections |
| `GET /v1/docs/search?q=&k=4` | Top passages for a query, numbered for citation, with the corpus version |
| `POST /v1/ask` | Natural-language question → grounded answer with trace and `sources` |

## Observability

Built on [AWS Lambda Powertools](https://docs.powertools.aws.dev/lambda/python/) ([`observability.py`](src/solarops/observability.py)), and inert outside Lambda, so the CLI and the MCP server's stdio stay clean.

- **Tracing.** X-Ray active tracing on every function, with subsegments for each catalogue query and write, each model call, and AWS SDK and HTTP calls. Copilot traces are annotated with `tool`, `provider` and `fallback`.
- **Logs.** JSON lines with the function request id plus a correlation id: the API Gateway request id for the API, and the SQS message id for ingestion. Events are never logged, and the formatter masks connection-string credentials, API keys and bearer tokens, even inside tracebacks.
- **Metrics** (namespace `SolarOps`, written as EMF log lines, so there are no `PutMetricData` calls):

| Metric | Source | Dimensions |
|---|---|---|
| `ApiLatencyMs` | every API request | `route` (the template, e.g. `/v1/facilities/{code}`) |
| `CopilotLatencyMs`, `LlmInputTokens`, `LlmOutputTokens`, `LlmCostUsd` | every copilot answer; tokens come from the Bedrock/OpenAI usage report and cost from list prices (`LLM_PRICE_*_PER_MTOK` to override), both 0 with `LLM_PROVIDER=none` | |
| `CopilotFallbacks` | whenever the copilot falls back to the rules router | `reason` (exception type) |
| `IngestLagMinutes`, `CurtailedFarms` | every poll (5 min) | |
| `RowsIngested` | every ingest batch | |

The stack deploys a CloudWatch dashboard (stack output `DashboardUrl`) with API requests, errors and p95 latency by route; copilot latency and fallbacks; LLM tokens and cost; ingest lag and rows; DLQ depth; curtailed farms; and Lambda errors and p95 duration per function. A **staleness alarm** fires when `IngestLagMinutes` stays above 30 for three 5-minute periods, or when the metric stops arriving, and notifies the same email topic as the other alarms.

## Design decisions

| Decision | Why |
|---|---|
| **Poller → queue → worker** instead of one big function | Decouples discovery from processing. Failed files retry on their own, and throughput scales with worker concurrency. |
| **Idempotent ingest**: `ingested_files` ledger plus `ON CONFLICT` upserts in one transaction | SQS delivers *at least once*, so a redelivered or duplicate file can never double-count output. |
| **Partial batch failure** (`ReportBatchItemFailures`), **DLQ after 5 attempts**, alarms to email | One bad file doesn't re-run its batch. Poison messages are isolated, and backlogs become visible. |
| **Refuses to ingest while the registry is empty** | Otherwise every reading is filtered out and the file is wrongly marked done (a real bug found during development). |
| **SQS `MaximumConcurrency: 2`** instead of reserved concurrency | Caps workers to protect Postgres connections without reserving account concurrency, which new AWS accounts have very little of. |
| **Raw zip archived to S3** (Infrequent Access at 30 days, deleted at 365). **Postgres keeps 30 days.** | Replay or backfill from S3. A daily retention job keeps the database within the free tier. |
| **Explainable performance model**: `expected = capacity × GHI/1000 × 0.8`, scored only when GHI ≥ 200 W/m² (IEC 61724-style filter) | Simple enough to reason about in an incident. The irradiance filter removed false dusk alerts seen in live data. |
| **Weather matched to the nearest observation (±30 min)**, with an hourly backfill stored at mid-hour | A missed poll no longer leaves intervals unscored, and a sunny-afternoon average is never applied at dusk. |
| **Tool calling with validated arguments**, deterministic fallback, golden-set evals in CI | The AI layer is testable, auditable and still works when the model doesn't. |
| **RAG with BM25 by default, embeddings optional**, section-level chunks, enforced citations, retrieval evals in CI | Small, curated corpus: keyword search is fast, free and hard to beat, and hybrid is one parameter away. Rejecting uncited answers keeps every claim traceable. |
| **Curtailment-aware status** (`ok` / `underperforming` / `likely_curtailed` / `curtailed`): negative prices every 5 minutes, AEMO next-day semi-dispatch caps once a day | Farms dispatched down by the market or network aren't faults. No single AEMO report is both live and complete, so the score is provisional in real time and corrected when the ground truth lands. |
| **Throttling at API Gateway**, in-process 60 s cache, `Cache-Control` headers | Data changes every 5 minutes, so rate limiting happens before any Lambda runs and repeated reads are cheap. |
| **Keyless deploys**: GitHub OIDC → short-lived IAM role scoped to this stack | No AWS access keys exist anywhere. The role can't edit its own permissions. |
| **Secrets in SSM Parameter Store**, least-privilege IAM for each function | No credentials in code or environment files. |

## Quick start (local, no AWS needed)

```bash
docker compose up -d                        # Postgres 16
cp .env.example .env                        # add your free Open Electricity API key
set -a; source .env; set +a
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export PYTHONPATH=src

python -m solarops.cli init                 # tables + solar farm registry
python -m solarops.cli ingest --files 12    # last hour of 5-min data + current weather
python -m solarops.cli underperformers      # farms below 60% of expected output right now
python -m solarops.cli ask "How did the fleet do in the last 6 hours?"
uvicorn solarops.api:app --reload           # API on http://localhost:8000/docs
```

Tests: 136 unit and integration tests. The integration tests need a Postgres, and CI provides one.

```bash
export TEST_DATABASE_URL=postgresql://solarops:solarops@localhost:5432/solarops
pytest -v
```

## Deploy to AWS

Pushes to `main` deploy automatically once CI passes. The one-time setup is about 15 minutes and is covered step by step in [docs/DEPLOY.md](docs/DEPLOY.md):

1. Create `infra/bootstrap.yaml` as a CloudFormation stack. It sets up the GitHub OIDC deploy role and a US$5 budget alert.
2. Store the database URL, Open Electricity key and API key in SSM Parameter Store.
3. Set the repository variable `AWS_DEPLOY_ROLE_ARN`, then run the **Deploy** workflow.

The workflow builds with SAM, deploys, loads the solar farm registry, and smoke-tests `/health` and `/v1/ask`.

**Expected cost: about US$0/month.** Around 20k Lambda invocations a month, SQS, EventBridge Scheduler, SSM, X-Ray traces, one dashboard and four alarms all fit the AWS free tiers, and Supabase's free Postgres holds 30 days of telemetry. Custom metrics are free for the first 10. Each extra `route` or `reason` series is billed pro rata for only the hours it receives data (US$0.30 per metric-month), so light API traffic adds cents. Bedrock, if you turn it on, costs fractions of a cent per question, and `LlmCostUsd` shows exactly how much.

## Known limitations

These are deliberate simplifications, found and measured against live data:

- **Curtailment is confirmed a day late.** AEMO publishes per-unit dispatch targets and caps only in the next-day report, so live scores use negative prices as a provisional hint (`likely_curtailed`) until the ground truth arrives.
- **GHI, not plane-of-array irradiance.** Single-axis trackers collect more than horizontal irradiance, so tracking farms can score above 100%. A per-farm calibrated ratio, or transposition to plane-of-array, would tighten this.
- **Hourly weather is coarse.** Fast-moving cloud shows up in the 5-minute output but not in hourly irradiance. Satellite irradiance would resolve it.

## Roadmap

- [x] **Phase 1: data platform.** Event-driven ingestion, idempotency, DLQ, archive, performance view.
- [x] **Phase 2: API and copilot.** FastAPI on Lambda, API-key auth, throttling, caching, OpenAPI; a tool-calling copilot with pluggable models and a fallback; golden-set evals as a CI gate; keyless deploys with OIDC.
- [x] **Curtailment-aware scoring.** Regional prices from DispatchIS every 5 minutes and per-unit semi-dispatch caps from Next_Day_Dispatch, so curtailment isn't reported as a fault.
- [x] **Phase 3: RAG.** O&M knowledge base, BM25 with optional hybrid embeddings, enforced citations, retrieval evals as a CI gate.
- [ ] **Manufacturer documents.** Ingest inverter and tracker manuals (PDF) for the specific equipment at each site, where licences allow.
- [x] **Phase 4: MCP server.** The same tool catalogue exposed over the Model Context Protocol, with tests over an in-memory MCP session.
- [x] **Phase 5: observability.** X-Ray tracing, JSON logs with correlation ids, EMF metrics for latency, fallbacks, tokens and cost, a CloudWatch dashboard and a staleness alarm.
- [x] **Public dashboard.** An unauthenticated, cached, throttled, read-only view of fleet performance at `/dashboard`.

## Data sources and terms

- AEMO market data (NEMWeb), © AEMO, used under AEMO's copyright permissions for market data.
- [Open Electricity](https://openelectricity.org.au) facility data, used under their terms of use.
- [Open-Meteo](https://open-meteo.com) weather data (CC BY 4.0).

This is an independent portfolio project, not affiliated with any of these organisations.

## License

[MIT](LICENSE) © 2026 Marcus Mah
