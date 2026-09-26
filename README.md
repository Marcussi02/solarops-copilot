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
  -d '{"question": "Which farms in Queensland are underperforming?"}' "$API/v1/ask"
```

```json
{
  "answer": "2 farm(s) in QLD1 below 60% of expected: ...",
  "tool": "underperformers",
  "args": {"threshold": 0.6, "limit": 10, "region": "QLD1"},
  "data": {"farms": [ ... rows from a fixed SQL query ... ]},
  "provider": "rules",
  "fallback_reason": null,
  "latency_ms": 41
}
```

> **Status:** Phase 1 (ingestion) ✅ · Phase 2 (API, copilot, evals, AWS deploy) ✅ · Next: RAG over equipment manuals. See the [Roadmap](#roadmap).

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
 EventBridge daily ─► RetentionFunction ─┘          solar_units · scada_readings · weather_obs
                                                    ingested_files · facility_performance (view)
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

- **Tool calling over a fixed query catalogue, not text-to-SQL.** The model picks one of four tools (`underperformers`, `facility_performance`, `fleet_summary`, `find_facilities`) and proposes arguments. It never writes SQL, so it can't touch tables it shouldn't or run an expensive query.
- **Every argument is validated** by a pydantic model with hard bounds (hours 1–168, known regions only, unknown fields rejected). A bad proposal is recorded and handed to the fallback, never executed.
- **Answers are grounded.** The summarising model only sees the tool's JSON result and is told to use only those numbers. The response returns the tool, arguments and raw data too, so every answer can be audited.
- **Pluggable providers.** `LLM_PROVIDER=none` (default) uses a deterministic router with template answers: free, offline, and the floor that CI tests. `bedrock` uses the Amazon Bedrock Converse API with native tool use (Nova Lite by default). `openai` uses Chat Completions. Switching is one parameter.
- **Graceful degradation.** A provider outage, timeout or invalid tool call falls back to the router for that step. `provider` and `fallback_reason` in the response keep this visible.
- **The database session is read-only** with a 5-second statement timeout. That's defence in depth under the fixed-query design.

### Evals

[`evals/golden.json`](evals/golden.json) holds 23 real-world phrasings, each mapped to the tool call it should produce: the right farm, window, region and threshold. Routing is where copilots fail in practice (the wrong farm, or a region silently dropped), and it can be scored without a database.

```bash
PYTHONPATH=src python -m solarops.evals                     # router: CI gate, must be 100%
PYTHONPATH=src python -m solarops.evals --provider bedrock  # score a real model (accuracy, p50/p95)
```

CI fails if the router's score drops below 100%. The same harness scores any LLM provider, so model changes are measured, not guessed.

## API

OpenAPI docs are served at `/docs`. The `/v1` routes need an `x-api-key` header.

| Route | What it returns |
|---|---|
| `GET /health` | Liveness and data freshness (`data_lag_minutes`). Public. |
| `GET /v1/status` | Pipeline counters: units, files, readings, latest interval |
| `GET /v1/facilities?region=` | Solar farms with capacity and location |
| `GET /v1/facilities/{code}?hours=24` | Energy, peak, performance index and hourly profile for one farm |
| `GET /v1/underperformers?threshold=0.6&region=` | Farms below expected output at the latest interval |
| `GET /v1/fleet?hours=24&region=` | Energy and average performance per NEM region |
| `POST /v1/ask` | Natural-language question → grounded answer with trace |

## Design decisions

| Decision | Why |
|---|---|
| **Poller → queue → worker** instead of one big function | Decouples discovery from processing. Failed files retry on their own, and throughput scales with worker concurrency. |
| **Idempotent ingest**: `ingested_files` ledger plus `ON CONFLICT` upserts in one transaction | SQS delivers *at least once*, so a redelivered or duplicate file can never double-count output. |
| **Partial batch failure** (`ReportBatchItemFailures`), **DLQ after 5 attempts**, alarms to email | One bad file doesn't re-run its batch. Poison messages are isolated, and backlogs become visible. |
| **Refuses to ingest while the registry is empty** | Otherwise every reading is filtered out and the file is wrongly marked done (a real bug found during development). |
| **SQS `MaximumConcurrency: 2`** instead of reserved concurrency | Caps workers to protect Postgres connections without reserving account concurrency, which new AWS accounts have very little of. |
| **Raw zip archived to S3** (Infrequent Access at 30 days, deleted at 365). **Postgres keeps 30 days.** | Replay or backfill from S3. A daily retention job keeps the database within the free tier. |
| **Explainable performance model**: `expected = capacity × GHI/1000 × 0.8`, ignored below 5% of capacity | Simple enough to reason about in an incident. Suppresses dawn, dusk and night noise. |
| **Tool calling with validated arguments**, deterministic fallback, golden-set evals in CI | The AI layer is testable, auditable and still works when the model doesn't. |
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

Tests: 61 unit and integration tests. The integration tests need a Postgres, and CI provides one.

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

**Expected cost: about US$0/month.** Around 20k Lambda invocations a month, SQS, EventBridge Scheduler, SSM and three alarms all fit the AWS free tiers, and Supabase's free Postgres holds 30 days of telemetry. Bedrock, if you turn it on, costs fractions of a cent per question.

## Roadmap

- [x] **Phase 1: data platform.** Event-driven ingestion, idempotency, DLQ, archive, performance view.
- [x] **Phase 2: API and copilot.** FastAPI on Lambda, API-key auth, throttling, caching, OpenAPI; a tool-calling copilot with pluggable models and a fallback; golden-set evals as a CI gate; keyless deploys with OIDC.
- [ ] **Phase 3: RAG.** Inverter manuals and datasheets in pgvector, hybrid search, citations.
- [ ] **Phase 4: MCP server.** The same tool catalogue exposed over the Model Context Protocol.
- [ ] **Phase 5: observability.** Tracing, and cost and latency dashboards for the copilot.

## Data sources and terms

- AEMO market data (NEMWeb), © AEMO, used under AEMO's copyright permissions for market data.
- [Open Electricity](https://openelectricity.org.au) facility data, used under their terms of use.
- [Open-Meteo](https://open-meteo.com) weather data (CC BY 4.0).

This is an independent portfolio project, not affiliated with any of these organisations.

## License

[MIT](LICENSE) © 2026 Marcus Mah
