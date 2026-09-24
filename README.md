# SolarOps Copilot

[![CI](https://github.com/Marcussi02/solarops-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Marcussi02/solarops-copilot/actions/workflows/ci.yml)

An event-driven data platform, and later an AI copilot, for **utility-scale solar farm operations**, built on **live public data**:

- **AEMO NEMWeb**: 5-minute output (MW) of every generating unit in Australia's National Electricity Market, including more than 100 solar farms
- **Open Electricity**: the facility registry (farm names, regions, coordinates, capacity)
- **Open-Meteo**: current irradiance and weather at each farm

It answers the core operations question: **is each farm producing what the weather says it should?**

> **Status:** Phase 1 (ingestion platform) ✅. Next: API, RAG over equipment manuals, a tool-calling agent, an MCP server and evals. See the [Roadmap](#roadmap).

## Architecture (Phase 1)

```
                   ┌──────────────────────┐
 EventBridge 5 min │ PollFunction         │ lists NEMWeb, skips files already ingested
 ─────────────────►│                      ├──────────────┐
                   └──────────────────────┘              ▼
                                                  ┌─────────────┐  5 failures  ┌─────┐
                                                  │ SQS queue   ├─────────────►│ DLQ │─► CloudWatch alarm
                                                  └──────┬──────┘              └─────┘
                                                         ▼  batch of 5, partial-failure reporting
                   ┌──────────────────────┐       ┌──────────────────────┐
 NEMWeb (zip) ────►│ S3 raw archive       │◄──────┤ IngestFunction       │ max 2 concurrent
                   │ date=YYYY-MM-DD/     │       │ download → parse →   │
                   └──────────────────────┘       │ upsert (1 txn)       │
                                                  └──────────┬───────────┘
 EventBridge daily ─► RegistryFunction ──┐                   ▼
 EventBridge 15 min ► WeatherFunction ───┴────────► PostgreSQL
                                                    solar_units · scada_readings ·
                                                    weather_obs · ingested_files ·
                                                    facility_performance (view)
```

## Design decisions

| Decision | Why |
|---|---|
| **Poller → queue → worker** instead of one big function | Decouples discovery from processing. Failed files retry on their own, and throughput scales by raising worker concurrency. |
| **Idempotent ingest**: `ingested_files` ledger plus `ON CONFLICT` upserts in one transaction | SQS delivers *at least once*, so a redelivered or duplicate file can never double-count output. |
| **Partial batch failure** (`ReportBatchItemFailures`) | One bad file doesn't force the other four in its batch to re-run. |
| **DLQ after 5 attempts, plus alarms** on DLQ depth and queue age | Poison messages are isolated, and silent backlogs become visible. |
| **Refuses to ingest while the registry is empty** | Otherwise every reading is filtered out and the file is wrongly marked done (a real bug found during development). |
| **Reserved concurrency of 2** on the worker | Serverless fan-out can exhaust Postgres connections. This caps it. |
| **Raw zip archived to S3** with a date partition and lifecycle (Infrequent Access at 30 days, deleted at 365) | Replay or backfill without re-downloading. Storage cost is bounded. |
| **Store solar units only** (about 150 of about 520) | Around 70% less data. Retention is a design choice, not an afterthought. |
| **Explainable performance model**: `expected = capacity × GHI/1000 × 0.8`, ignored below 5% of capacity | Simple enough to reason about in an incident. Suppresses dawn, dusk and night noise. |
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
python -m solarops.cli status
python -m solarops.cli underperformers      # farms below 60% of expected output right now
```

Run the tests. Integration tests need a Postgres, and CI provides one automatically:

```bash
export TEST_DATABASE_URL=postgresql://solarops:solarops@localhost:5432/solarops
pytest -v
```

## Deploy to AWS

```bash
aws ssm put-parameter --name /solarops/database-url --type SecureString --value "postgresql://..."
aws ssm put-parameter --name /solarops/openelectricity-api-key --type SecureString --value "..."
sam build && sam deploy --guided            # e.g. region ap-southeast-2 (Sydney)
sam remote invoke RegistryFunction          # load the registry once
```

Any Postgres works. A free Supabase or Neon database keeps it close to zero cost.

## Roadmap

- [x] **Phase 1: data platform.** Event-driven ingestion, idempotency, DLQ, archive, performance view.
- [ ] **Phase 2: API.** FastAPI with auth, rate limiting, caching and OpenAPI.
- [ ] **Phase 3: RAG.** Inverter manuals and datasheets in pgvector, hybrid search, citations.
- [ ] **Phase 4: agent and MCP.** A tool-calling agent (`query_performance`, `get_weather`, `search_manuals`) exposed as an MCP server.
- [ ] **Phase 5: evals and observability.** A golden question set as a CI gate, tracing, and cost and latency dashboards.

## Data sources and terms

- AEMO market data (NEMWeb), © AEMO, used under AEMO's copyright permissions for market data.
- [Open Electricity](https://openelectricity.org.au) facility data, used under their terms of use.
- [Open-Meteo](https://open-meteo.com) weather data (CC BY 4.0).

This is an independent portfolio project, not affiliated with any of these organisations.
