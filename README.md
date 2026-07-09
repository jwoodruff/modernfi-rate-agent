# ModernFi Rate Agent

## Table of Contents

- [Live Demo](#live-demo)
- [1. Overview](#1-overview)
- [2. Architecture](#2-architecture)
- [3. Decisions & Tradeoffs](#3-decisions--tradeoffs)
- [4. Running Locally](#4-running-locally)
- [5. Deploying to AWS](#5-deploying-to-aws)
- [6. Continuous Deployment (GitHub Actions)](#6-continuous-deployment-github-actions)
- [7. API Reference](#7-api-reference)
- [8. Manual Sanity Testing](#8-manual-sanity-testing)
- [9. What I'd Do With More Time](#9-what-id-do-with-more-time)

## Live Demo

Running on AWS ECS Fargate:

**http://modernfi-alb-1883adc-1363646707.us-west-2.elb.amazonaws.com**

```bash
curl http://modernfi-alb-1883adc-1363646707.us-west-2.elb.amazonaws.com/health

curl -X POST http://modernfi-alb-1883adc-1363646707.us-west-2.elb.amazonaws.com/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the current 30-year mortgage rate?"}'

curl "http://modernfi-alb-1883adc-1363646707.us-west-2.elb.amazonaws.com/history?limit=5"
```

This runs on a personal AWS account and may get torn down (`pulumi destroy`)
after review. See [Deploying to AWS](#5-deploying-to-aws) to stand it back up
if the link is dead.

The deployed instance uses my own Anthropic and FRED credentials, so usage
against this URL draws on my own quota and billing. Auto-reload isn't
enabled. If credits run out, `/ask` returns a `503` with a "temporarily
unavailable" message instead of failing silently. `/health` still reports
healthy in that case, since it only checks the database connection. Be
considerate with request volume, or run the app locally with your own keys.

## 1. Overview

An agentic service that answers natural-language questions about U.S.
interest rates and economic indicators, using live FRED (Federal Reserve
Economic Data) data. Built with FastAPI and Claude's tool-use API. Deployed
on AWS ECS Fargate behind an Application Load Balancer, with Postgres (RDS)
persisting a history of every question and answer.

## 2. Architecture

```
                    ┌─────────────┐
   User / curl ───▶ │     ALB     │  (public, port 80)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Fargate   │  (private subnet)
                    │  FastAPI    │
                    │   /ask      │
                    │   /history  │
                    │   /health   │
                    └──┬───────┬──┘
                       │       │
              ┌────────▼─┐   ┌─▼──────────┐
              │  Claude   │   │  Postgres  │
              │ (tool use)│   │   (RDS)    │
              └────┬──────┘   └────────────┘
                   │
            ┌──────▼──────┐
            │   FRED API   │
            │ search/fetch │
            └──────────────┘
```

**Request flow for `POST /ask`:**

1. The user's question goes to Claude along with two tool definitions:
   `search_fred_series` and `get_fred_data`.
2. For almost any in-scope question, current or historical, Claude calls
   `search_fred_series` first to resolve a plain-language question
   ("30-year mortgage rate") to a FRED series ID (`MORTGAGE30US`). The
   system prompt tells it to prefer live data over its own training
   knowledge, since rates change often. Tool calls get skipped only for
   questions clearly outside scope (the system prompt directs Claude to
   decline and redirect those) or input too unclear to act on.
3. Claude then calls `get_fred_data` with that series ID to fetch the
   actual value: the latest observation, or a historical range if the
   question references a specific date or period.
4. This repeats in a loop until Claude has enough information to answer in
   plain text. It can chain multiple tool calls, sometimes several in
   parallel in one turn.
5. The exchange (question, answer, status, and operational metadata:
   iteration count, tool calls made, duration) gets written to Postgres.
6. The answer goes back to the user.

**Components:**

- **ALB (Application Load Balancer):** the only publicly reachable piece.
  Terminates inbound HTTP and forwards to whichever Fargate task(s) are
  healthy, using `/health` as the target group's health check path.
- **ECS Fargate:** runs the FastAPI app as a container. No EC2 servers to
  patch or manage. Lives in a private subnet; the only way in is through
  the ALB.
- **Claude (Anthropic API):** the agentic reasoning layer. Decides which
  tools to call, in what order, and synthesizes the final natural-language
  answer.
- **FRED API:** the actual data source for rates and economic indicators.
- **RDS Postgres:** persists every `/ask` exchange for the `/history`
  endpoint, and for operational visibility: which questions are slow,
  which fail, which tools get used most.

**Project structure:**

```
modernfi-rate-agent/
├── app/
│   ├── main.py       # FastAPI app + routes only (/health, /history, /ask)
│   ├── agent.py       # Claude client, system prompt, the tool-use loop
│   ├── tools.py        # tool JSON schemas + call_tool dispatcher
│   ├── fred.py          # FRED API client (search_fred_series, get_fred_data)
│   ├── db.py             # connection pool lifecycle, save_query, fetch_history
│   └── models.py          # Pydantic request/response models
├── infra/
│   └── __main__.py         # Pulumi program (VPC, RDS, ECS/Fargate, ALB, secrets)
├── .github/workflows/
│   ├── pulumi-preview.yml  # pulumi preview on every PR
│   └── pulumi-deploy.yml   # pulumi up on merge to main
├── init_db/
│   └── 001_create_tables.sql
├── test_agent.py           # manual smoke-test script (see §8)
├── test_error_handling.py  # manual API-failure drill (see §8)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

Split by concern rather than left as one file. `main.py` is pure HTTP
routing and delegates everything else out. `agent.py` owns the Claude
interaction and tool-use loop. `tools.py` is the schema "contract" Claude
sees. `fred.py` is a plain HTTP client with zero framework dependencies,
easy to unit test in isolation. `db.py` owns the Postgres connection
lifecycle and queries. Each piece is independently readable and testable
without loading the rest of the app.

## 3. Decisions & Tradeoffs

**Python / FastAPI.** Matches ModernFi's existing stack. This should feel
like something a teammate already wrote, not a foreign artifact. FastAPI
is async-native, which matters here: the agent loop spends most of its
wall-clock time waiting on network I/O (Claude, FRED, Postgres), and
`async`/`await` lets the event loop handle other requests during those
waits instead of blocking a thread per request. Concretely, `agent.py`
uses Anthropic's async client (`AsyncAnthropic`) and `fred.py` uses
`httpx.AsyncClient`. A slow Claude or FRED call never blocks other
in-flight requests on the same worker: a concurrent `/ask`, say, or an ALB
`/health` poll. Auto-generated OpenAPI docs at `/docs` are a free
byproduct, useful for anyone exploring the API without reading this
README first.

**FRED API over scraping treasury.gov or the Fed's site.** FRED is a
clean, documented, stable REST API built for exactly this kind of
programmatic access: a search endpoint, an observations endpoint,
consistent JSON, no HTML to parse or break when a page redesign ships.
Scraping would be more brittle (it breaks on any front-end change), slower
to build correctly, and arguably against the spirit of what those sites
are for. FRED exists precisely so this problem doesn't need scraping.

**Claude tool use as the agentic layer, with a two-tool (search → fetch)
design.** A single "get me rate X" tool would require the caller, or a
hardcoded mapping, to already know FRED's exact series IDs, defeating the
point of a natural-language interface. Splitting into `search_fred_series`
(resolve a plain-language question to a series ID) and `get_fred_data`
(fetch the actual values, current or historical) mirrors how a person
would do this by hand: look it up, then pull the number. It also lets
Claude chain the two tools autonomously, including in parallel when a
question needs several series at once: "what are current interest rates"
fans out to mortgage, fed funds, and treasury yields in one turn.

**Claude Haiku 4.5 as the model, injected via config rather than
hardcoded.** This service's actual reasoning load is narrow: parse a
question, decide which of two tools to call, turn a numeric FRED
observation into a plain-English sentence. That's tool selection and
light synthesis, not open-ended reasoning. A smaller, faster, cheaper
model handles it reliably once the tool descriptions and system prompt do
the heavy lifting. It matters more here than usual too: each `/ask` call
can cost 2 to 3 sequential Claude round trips (search, fetch, answer), and
Haiku's lower per-token cost and latency compound across that whole loop.
Given the 5 to 12 second response times this service actually sees,
that's a real difference, not a rounding error.

The model string is never hardcoded. `agent.py` reads it from
`os.environ["ANTHROPIC_MODEL"]` once, at import time, into a module-level
constant, not per request. Locally it's set via `.env`; in production, via
a plain Pulumi config value (`anthropicModel`) injected as a
task-definition environment variable, not a secret, since a model name
isn't sensitive. Reading it once at startup rather than on every call
means a missing or misconfigured `ANTHROPIC_MODEL` fails loudly when the
container starts, instead of surfacing as an unhandled `500` on whichever
request happens to hit `/ask` first. Swapping models (a newer Haiku point
release, or dialing up to Sonnet if harder questions start needing it) is
still just a `pulumi config set anthropicModel <model>` and a `pulumi up`.
No code change, no new Docker image.

**Postgres over SQLite.** SQLite would have been faster to stand up
locally, and technically sufficient for a single-table take-home. But
Postgres is what you'd actually reach for in production: proper
concurrent write handling, a real network-accessible service RDS can host
managed and highly available, native `JSONB` and `TIMESTAMPTZ` types that
fit this schema cleanly. Using SQLite here would have meant re-deciding
this the moment the project went from take-home to real service. Postgres
front-loads that decision.

**`asyncpg` directly over an ORM (SQLAlchemy, etc.).** This service has
exactly one table and a small, fixed set of queries: one insert, one
paginated select. An ORM's value is managing complexity across many
tables, relationships, and evolving query patterns, none of which apply
here. Raw SQL via `asyncpg` is faster (no ORM translation layer), more
transparent (the query you read is the query that runs), and there's less
machinery to misconfigure for a schema this small. This is a deliberate
trade, not a default. A service with more than a couple of tables, or any
real joins, would push me back toward an ORM or query builder.

**ECS Fargate over EKS (or raw EC2).** A single containerized service with
one moving part to run. Fargate is the right-sized tool for that.
Kubernetes/EKS brings custom scheduling, complex multi-service
topologies, a large ecosystem, plus cluster upgrades, node group
management, and a control plane to reason about: none of it needed yet.
Fargate gives "run this container, don't make me manage servers" with a
much smaller surface area. Migrating to EKS later, if the platform grows
into needing it, is tractable. This decision doesn't foreclose it.

**Pulumi over Terraform/CloudFormation/hand-written YAML.** Infrastructure
as actual code (Python, here) means control flow, functions, and a type
checker in the loop instead of a separate declarative dialect the editor
can't reason about. It also means this infra shares tooling and
conventions with the application code. The tradeoff: Pulumi's Python SDK
surface is less exhaustively documented than Terraform's in places, and
its `Output`/`apply` model for handling values not known until deploy
time takes some getting used to. Worth it here for writing infra in the
same language as the app.

**Secrets Manager for API keys and the database URL, not plaintext
environment variables.** ECS task definitions are visible in the console
and API to anyone with read access to the account. A plaintext
`environment` block puts secrets in that blast radius. The task
definition's `secrets` field instead stores an ARN pointing at Secrets
Manager, and ECS resolves the actual value only at container start. The
secret itself never appears in the task definition JSON. Locally, the
same secrets live in a gitignored `.env` file rather than in code or
Docker Compose directly, for the same reason at smaller scale.

**No prompt caching (for now).** Anthropic's prompt caching keys off a
prefix (`tools` → `system` → `messages`), and would in principle fit
`agent.py`'s tool-use loop, which resends the same system prompt and tool
schemas on every iteration. It's skipped here because the combined
`SYSTEM_PROMPT` and tool schemas come to roughly 600 to 700 tokens, well
under Haiku 4.5's 2048-token minimum cacheable prefix. A `cache_control`
marker below that minimum is a silent no-op: no error,
`cache_read_input_tokens` just stays at 0. So adding it today wouldn't do
anything. Worth revisiting if the tool set or system prompt grows, or if
the model changes to one with a lower minimum (Sonnet's is 1024 tokens).

## 4. Running Locally

**Prerequisites:**
- Python 3.12
- Docker Desktop (for Postgres via Docker Compose)
- An Anthropic API key
- A FRED API key ([free registration](https://fred.stlouisfed.org/docs/api/api_key.html))

**Steps:**

1. Clone the repo and move into it:
   ```bash
   git clone <repo-url>
   cd modernfi-rate-agent
   ```

2. Copy the example env file and fill in your own keys:
   ```bash
   cp .env.example .env
   ```
   `.env` should contain:
   ```
   ANTHROPIC_API_KEY=your_key_here
   ANTHROPIC_MODEL=claude-haiku-4-5-20251001
   FRED_API_KEY=your_key_here
   DATABASE_URL=postgresql://modernfi:modernfi_dev_password@localhost:5432/modernfi_rate_agent
   ```

3. Start Postgres and the app together:
   ```bash
   docker compose up --build
   ```
   This starts Postgres (with the `queries` table created automatically on
   first boot) and the FastAPI app, wired to talk to each other over the
   Compose network.

4. Confirm it's running:
   ```bash
   curl http://localhost:8000/health
   ```
   Expected: `{"status": "healthy", "database": "connected"}`

**Example requests:**

```bash
# Ask a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the current 30-year mortgage rate?"}'

# View recent history
curl "http://localhost:8000/history?limit=5"

# Historical data
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What was the unemployment rate in January 1960?"}'
```

## 5. Deploying to AWS

**Prerequisites:**
- [Pulumi CLI](https://www.pulumi.com/docs/install/) installed
- AWS credentials configured (`aws configure`) with permissions for ECR,
  ECS, RDS, EC2/VPC, IAM, and Secrets Manager
- Docker running locally (Pulumi builds and pushes the image from your
  machine as part of `pulumi up`)

**Steps:**

1. Move into the infra directory and select/create a stack:
   ```bash
   cd infra
   pulumi stack select dev   # or: pulumi stack init dev
   ```

2. Set the required secrets and config (these are encrypted at rest in the
   stack config file):
   ```bash
   pulumi config set --secret fredApiKey YOUR_FRED_KEY
   pulumi config set --secret anthropicApiKey YOUR_ANTHROPIC_KEY
   pulumi config set --secret dbPassword YOUR_DB_PASSWORD
   pulumi config set anthropicModel claude-haiku-4-5-20251001
   ```

3. Preview the plan. No changes are made; this just shows what *would* be
   created:
   ```bash
   pulumi preview
   ```

4. Deploy:
   ```bash
   pulumi up
   ```
   This provisions, in order: a VPC, an ECS cluster, an ECR repo (and
   builds/pushes the app image into it), RDS Postgres, Secrets Manager
   entries for each credential, an ALB, and the Fargate service itself.
   Expect this to take several minutes. RDS and the ALB are the slowest
   pieces to come up.

5. Get the public URL:
   ```bash
   pulumi stack output alb_url
   ```
   Then verify:
   ```bash
   curl $(pulumi stack output alb_url)/health
   ```

To tear everything down (stop paying for it):
```bash
pulumi destroy
```

## 6. Continuous Deployment (GitHub Actions)

Two workflows in `.github/workflows/` automate what Section 5 describes
doing by hand. Both are live on this repo now, not just aspirational.

`pulumi-preview.yml` runs `pulumi preview` on every PR against `main` and
comments the plan directly on the PR. It uses a read-only IAM role
(`ReadOnlyAccess`-equivalent) assumable from any branch or PR in this
repo. Safe to run against an untrusted contribution, since it can plan
but never apply anything. A branch protection rule on `main` makes this
job's status a required check: a PR can't merge while `pulumi preview` is
failing.

`pulumi-deploy.yml` runs `pulumi up` automatically on every push to
`main`, every merge. It uses a full-access IAM role, and the job declares
`environment: production`, which pauses it for a manual approval click if
a required reviewer is configured on that environment. Required reviewers
on environments needs a public repo, or GitHub Team/Enterprise for a
private one. This repo is public specifically so that gate works.

Both roles are assumed through GitHub's OIDC identity federation rather
than long-lived AWS access keys sitting in GitHub secrets. GitHub mints a
short-lived, per-workflow-run token; AWS trusts it based on claims baked
into that token (which repo, which branch or environment) instead of a
static credential that has to be rotated and can leak. The preview role's
trust condition matches on the repo and branch; the deploy role's matches
on the repo and the `production` environment name specifically, since
that job declares `environment: production` and GitHub scopes the token's
claims to the environment once a job attaches one.

### One-time setup

This is what it took to bootstrap this repo's pipeline, kept here as the
runbook for doing it again: from a fork, or a fresh AWS account. A few of
these steps need real AWS/GitHub credentials and can't be done by
`pulumi up` alone, since CI can't create the very trust role it needs to
authenticate in the first place.

1. **Move the Pulumi backend off your laptop.** A local file backend
   (`file://~`) isn't reachable from a GitHub Actions runner. Sign up at
   [app.pulumi.com](https://app.pulumi.com), then:
   ```bash
   cd infra
   pulumi stack export --file /tmp/dev-state.json   # back up current state first
   pulumi login https://api.pulumi.com               # explicit backend URL
   pulumi stack init dev                              # or select if it already exists
   pulumi stack import --file /tmp/dev-state.json
   ```
2. **Switch the secrets provider to Pulumi Cloud's managed encryption.**
   A passphrase-encrypted stack (`PULUMI_CONFIG_PASSPHRASE`) would need
   that passphrase as a CI secret too. Converting removes that credential
   entirely:
   ```bash
   pulumi stack change-secrets-provider "default"   # prompts for the passphrase once
   ```
3. **Apply the new OIDC provider and IAM roles.** With your existing local
   AWS credentials still configured, run one more `pulumi up` to create
   `github_oidc_provider` and the two roles added in `infra/__main__.py`:
   ```bash
   pulumi up
   ```
4. **Collect the values GitHub Actions needs:**
   ```bash
   pulumi stack output github_actions_preview_role_arn
   pulumi stack output github_actions_deploy_role_arn
   pulumi whoami                       # your Pulumi Cloud org/username
   ```
   Generate a Pulumi access token from Pulumi Cloud under **Settings →
   Access Tokens**.
5. **Configure the GitHub repo** (Settings → Secrets and variables →
   Actions):
   - **Secrets:** `PULUMI_ACCESS_TOKEN`
   - **Variables:** `AWS_PREVIEW_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN` (GitHub
     rejects any secret/variable name starting with `GITHUB_`, reserved
     for GitHub's own built-in variables), and `PULUMI_STACK` (e.g.
     `your-pulumi-org/modernfi-rate-agent/dev`)
6. **Create the approval gate** (Settings → Environments → New
   environment, name it `production`, matching the `environment:
   production` key on the deploy job, then **Required reviewers** → add
   yourself → Save protection rules). Needs a public repo, or GitHub
   Team/Enterprise for a private one.
7. **Require the preview check before merging.** GitHub only lets you
   select a status check once it's actually run at least once, so first
   open any PR and let `pulumi-preview.yml` run on it. Then: Settings →
   Branches → Add branch protection rule → branch name pattern `main` →
   **Require status checks to pass before merging** → search for and
   select `preview` (the job name in `pulumi-preview.yml`) → Save.

End state: every PR against `main` needs a passing `pulumi preview` before
it can merge, and every merge to `main` kicks off `pulumi up`, pausing
for an approval click first, with no AWS keys ever stored in GitHub.

## 7. API Reference

### `POST /ask`
Ask a natural-language question about interest rates or economic indicators.

**Request body:**
```json
{ "question": "What's the current federal funds rate?" }
```

**Response:**
```json
{ "answer": "The most recent effective federal funds rate is 3.63%, as of June 2026..." }
```

Validation: `question` must be non-empty (whitespace-only strings are
rejected with a `422`).

### `GET /history`
Returns past `/ask` exchanges, most recent first.

**Query parameters:**
| Param    | Type | Default | Notes                      |
|----------|------|---------|----------------------------|
| `limit`  | int  | 20      | 1 to 100                   |
| `offset` | int  | 0       | for pagination             |

**Response:** array of records, each containing `id`, `question`, `answer`,
`status` (`success` / `max_iterations` / `error`), `created_at`,
`iterations`, `tool_calls`, `duration_ms`, and `tools_used`.

### `GET /health`
Liveness/readiness check used by the ALB target group. Runs an actual query
against Postgres rather than just confirming the process is up.

**Healthy:** HTTP `200`, `{"status": "healthy", "database": "connected"}`
**Unhealthy:** HTTP `503`, `{"status": "unhealthy", ...}`

## 8. Manual Sanity Testing

`test_agent.py` (project root) is a small manual smoke-test script, not a
unit test suite, just a quick way to eyeball that the whole stack is
behaving after a change. Run it with the app up (`docker compose up` or a
live deploy, pointing `BASE_URL` at whichever):

```bash
python test_agent.py
```

It runs, in order:

1. **`/health`**: confirms the app and its Postgres connection are
   actually up before bothering to test anything else.
2. **`/ask`**: a fixed set of test questions covering specific rates
   ("30-year mortgage rate"), vague/broad questions ("are rates high right
   now"), and edge cases (empty string, nonsense input, an off-topic
   question, a historical date lookup). Prints status code, timing, and
   the response body for each.
3. **`/history`**: three checks.
   - **Basic fetch:** prints the most recent rows so you can eyeball that
     a real `/ask` call actually got persisted with sane fields.
   - **Pagination check:** confirms `offset=0` and `offset=5` return
     non-overlapping `id`s.
   - **Row-count check:** confirms the total row count is at least the
     number of questions this run just sent. If it's lower, that's a
     signal `save_query` may be silently failing to persist (its
     try/except swallows DB errors on purpose so a logging failure never
     breaks a user's `/ask` response, which means this script is the
     backstop that would actually catch that failure mode).
   - **Validation check:** confirms `/history?limit=0` and
     `/history?limit=101` both return `422`, not `200` or `500`.
4. **Summary**: a compact ✅/❌ table of every `/ask` test case with
   status code and timing, plus a flag for anything that took over 10
   seconds (worth checking server logs for the per-iteration/per-tool
   breakdown on any flagged question).

This isn't a substitute for the real unit/integration tests listed below.
It doesn't run in CI, and it doesn't assert anything strictly. It prints
warnings; it doesn't fail loudly. It's a fast way to catch the failure
modes that matter most here: a multi-tool-call turn wired incorrectly,
edge-case input (empty strings, off-topic questions, reversed date
ranges) producing a `500` instead of a clean response.

`test_error_handling.py` (project root) exercises the one failure path
`test_agent.py` can't reach without manual intervention: what happens when
the Anthropic API call itself fails (invalid/expired key, outage, etc.),
which should surface as a `503` from `/ask` with `status='error'` written
to history, while `/health` stays `200` since it only checks Postgres.
Rather than asking you to hand-edit `.env` and restart the container
yourself, the script automates the whole drill:

```bash
python test_error_handling.py
```

It backs up your real `.env`, swaps in an obviously-invalid
`ANTHROPIC_API_KEY`, force-recreates the `app` container via `docker
compose` so it picks up the bad key, waits for `/health` to come back up,
then runs the same checks as `test_agent.py --error-handling` (confirms
`/ask` returns `503`, confirms the most recent `/history` row shows
`status='error'`, confirms `/health` is still `200`). A `finally` block
always restores your real `.env` and recreates the container again
afterward, even on a failed assertion or a Ctrl+C partway through, so a
run can't accidentally leave your local app stuck on a fake key. Like
`test_agent.py`, this requires the app already running via `docker compose`
(it assumes a service named `app` in `docker-compose.yml` and a `.env` in
the current directory) and isn't wired into CI.

## 9. What I'd Do With More Time

- **Tests.** Unit tests for `call_tool`'s dispatch/validation logic and the
  FRED functions' error paths (timeout, bad series ID, empty results); an
  integration test that runs the full `/ask` loop against a mocked Claude
  response to verify the multi-tool-call and `tool_result` wiring without
  needing live API credentials in CI. The `pulumi-preview.yml` workflow
  (see §6) has nowhere to run these yet, since none exist. Once written,
  they'd slot in as a step before `pulumi preview` in that same workflow.
- **HTTPS.** ACM certificate plus a Route 53 domain in front of the ALB,
  with an HTTPS listener redirecting from port 80. Currently plain HTTP,
  which is fine for a take-home demo but not for anything real.
- **Rate limiting.** Nothing currently stops one client from hammering
  `/ask` (each call costs real money in Claude + FRED API usage). Even a
  simple token-bucket per-IP limiter would close that gap.
- **Structured logging / observability.** Current logging is
  human-readable `logger.info` lines. Real production use would want
  structured JSON logs shipped to CloudWatch (or Datadog), plus a couple of
  dashboards: request latency percentiles, tool-call counts, error rates by
  status. The per-request timing breakdown already built into `/ask`'s
  logs is a natural foundation for this.
- **Caching frequent FRED queries.** Rates like the federal funds rate
  don't change every minute; a short-TTL cache (in-process or Redis) in
  front of `get_fred_data`/`search_fred_series` would cut latency and FRED
  API usage for repeat questions: a real improvement given the observed
  5 to 12 second response times for common queries.
- **Real migration tooling (Alembic).** The current `CREATE TABLE IF NOT
  EXISTS` approach is intentionally simple and self-bootstrapping, but it
  can't evolve an existing schema. Adding a column later would require a
  manual `ALTER TABLE` outside this code path. It also isn't fully
  race-safe under concurrent startups: two ECS tasks launching at the same
  instant (e.g. during a `desired_count > 1` deploy) could both attempt the
  DDL simultaneously. Neither is a problem yet at `desired_count=1`, but
  Alembic (or similar) would give a proper versioned migration history and
  remove both issues before scaling out.
- **Multi-environment support.** Right now there's a single Pulumi stack
  (`dev`). A `staging`/`prod` split, via `Pulumi.staging.yaml` /
  `Pulumi.prod.yaml` with per-stack config (instance sizes, secrets),
  would be the natural next step before this served real traffic.
- **ALFRED (vintage/point-in-time data) support.** FRED only ever returns
  the latest revised value for a series, but many economic indicators
  (unemployment, GDP, CPI) get revised after their initial release. FRED's
  sibling API, ALFRED (ArchivaL Federal Reserve Economic Data), exposes the
  data *as it was originally published* at any given point in time. This
  matters for historical questions specifically: "what was the unemployment
  rate in July 1960" is ambiguous between "the originally reported figure"
  and "the figure as revised today," and right now `get_fred_data` always
  returns the latter. Adding a third tool (or an optional parameter on
  `get_fred_data`) backed by ALFRED's `vintage_dates` parameter would let
  Claude distinguish between these when a question calls for it, and would
  be a natural, on-brand extension for a service already built around FRED.
- **Hardcoded shortcuts for common series.** `search_fred_series` costs a
  full Claude + HTTP round trip even for well-known, static IDs.
  "What's the fed funds rate" doesn't need a search when `FEDFUNDS` never
  changes. A small map of roughly 10 to 20 common terms → series IDs, with
  `search_fred_series` as the fallback for anything not in the map, would
  cut a full round trip off the most common questions without giving up
  the general-purpose case.
- **Concurrent tool execution within a turn.** When Claude requests
  multiple tools in one turn (e.g. "what are current interest rates"
  fanning out to several series), `agent.py` still `await`s them one at a
  time in a plain `for` loop. Since every result gets batched into a single
  message regardless of completion order, `asyncio.gather` would let them
  run concurrently with no other change to the loop's structure.
- **`is_error` on failed tool results.** The Messages API supports flagging
  a `tool_result` as a failure explicitly (`is_error: true`); this app
  relies entirely on Claude inferring failure from the shape of the JSON
  (`{"error": "..."}`). Setting `is_error=True` whenever a tool call
  returns an error dict would use the mechanism built for this instead of
  leaving it to inference.
- **Persisting `response.stop_reason`.** Today, `max_tokens` (a truncated
  answer) and `refusal` (a declined answer) both fall through the same
  code path as a clean `end_turn` and get recorded as `status="success"`.
  There's no way to tell them apart later via `/history`. Storing the
  actual `stop_reason` on the final iteration would make that distinction
  queryable.
- **ECS redundancy.** `desired_count=1` and a single NAT gateway
  (`NatGatewayStrategy.SINGLE`) are both real single points of failure that
  keep the AWS bill down but aren't called out anywhere as tradeoffs.
  Bumping `desired_count` (with target-tracking autoscaling on top) and
  moving to a per-AZ NAT strategy would be the next steps before this
  served real traffic.
- **SSM Parameter Store instead of Secrets Manager.** None of the three
  secrets here use Secrets Manager's automatic rotation, its main
  differentiator and cost driver over the alternative. SSM Parameter Store
  `SecureString` parameters give the same "never in plaintext in the task
  definition" property at no additional per-secret cost for anything that
  isn't being rotated.
- **Auth on `/ask` itself.** There's currently no authentication in front
  of `/ask`. Anyone who can reach the ALB can trigger a real, billed
  Claude + FRED call. An API key (even a simple shared-secret header
  checked in `main.py`) would close this gap before rate limiting alone
  would: rate limiting slows down abuse, it doesn't require the caller to
  be authorized at all.
- **Streaming responses.** Claude's Messages API supports server-sent-event
  streaming. Given the observed 5 to 12 second response times, streaming
  partial text back to the caller as it's generated, rather than waiting
  for the entire multi-iteration loop to finish, would improve perceived
  latency even though total time wouldn't change.
- **Multi-turn conversations.** Every `/ask` today starts a brand-new,
  memoryless conversation. There's no way to ask a natural follow-up
  ("what about the 15-year rate?") without repeating the whole question.
  Supporting this would mean accepting an optional conversation/session ID,
  persisting the growing `messages` list per session in Postgres instead of
  discarding it at the end of `run_agent`, and replaying it on the next
  request in that session.
- **Retry/backoff on FRED calls.** `fred.py` gives up after a single
  attempt on a timeout or 5xx and returns an error straight to Claude. A
  transient network blip currently degrades all the way to "I couldn't
  retrieve that" instead of a quick, cheap retry. A short exponential
  backoff (2 to 3 attempts) before falling back to the current
  error-as-data behavior would absorb most of those blips.
