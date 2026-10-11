# Alankar Hotel Analytics Platform

A natural-language analytics chatbot over the Alankar restaurant's PostgreSQL warehouse. Type a
question in plain English. The system decides whether the data can answer it, writes SQL,
validates and runs that SQL, and returns a summary, details, insights, caveats, the result table
and the SQL it ran.

`schema.md` is the single source of truth about the database. The validation and SQL agents read
it at request time, so editing it changes their behaviour with no code edit. Do not copy schema
facts into Python.

## How it works

```
user query
  [1] route      (LLM, claude-haiku-5-5, no schema.md)   general / out_of_scope -> reply, done
  [2] validate   (LLM, claude-opus-5-5, schema.md)       answerable=false -> reason, done
  [3] write SQL  (LLM, claude-opus-5-5, schema.md)
  [4] check + execute (deterministic: sqlglot static checks, EXPLAIN cost guard, read-only run)
        fail -> back to [3] with the error, at most 2 retries
  [5] compose    (LLM, claude-opus-5-5, rows only)
  -> summary + details + insights + caveats + table + sql
```

The happy path makes four LLM calls. The stages are plain functions in `backend/agents/` and
`backend/tools/`, and `backend/orchestrator.py` calls them in order.

**Structured output.** Every LLM stage returns JSON that is validated against a Pydantic model in
`backend/models.py`. The same model generates the JSON schema sent to the API, so each schema is
defined once. By default the schema is sent through the Messages API structured-output parameter
(`output_config.format`, via `client.beta.messages.parse`). The forced tool call
(`tool_choice={"type":"tool"}`) is the fallback. It is used when `STRUCTURED_MODE=forced_tool`,
or automatically if the API rejects structured output with a 400. Claude Opus 5.5 rejects forced
tool calls, so the forced-tool fallback only works on models that accept them, such as Claude
Haiku 5.5. A result that fails Pydantic validation is a hard error and is never patched up.

**Charts.** The composer also suggests a chart (`FinalAnswer.chart`): its type (`bar`,
`horizontal_bar`, `line`, `area`, `grouped_bar`, `stacked_bar`, `pie`, `stat` or `none`), which
result columns go on x and y, an optional series column, the unit, a title, and a one-line reason.
`tools/chart_check.py` then checks the suggestion against the rows actually returned. It never
fails the answer. Instead it downgrades suggestions that can't be drawn honestly (a single row
becomes stat figures, a pie with more than 6 slices becomes a bar chart, an unknown column drops
the chart) and records the change in `meta.chart_warnings`. The UI draws the chart with Recharts
and offers a switcher between the types that fit the data's shape; the suggested type is marked
with a dot. Colours come from a fixed 8-colour categorical palette, checked for colour-blind
separation against the white card. Each colour stays with its series when you switch type, and
series beyond 8 fold into "Other". Every y column must share one unit, so there is never a second
y-axis.

**Refusal fallback.** Opus-family calls send the server-side refusal fallback beta
(`fallbacks: "default"`). Set `REFUSAL_FALLBACKS=false` to turn it off.

**Prompt caching.** `schema.md` (about 12k tokens) goes in a cached system block for the
validation and SQL stages, so repeat questions read it from the cache.

## Setup

Requirements: Python 3.11+, Node 18+, PostgreSQL 14+ with the warehouse loaded (see
`../BACKEND/warehouse_setup.sql` and `../BACKEND/fact_sales.csv`).

### 1. Read-only database role

Run this as the database owner. It creates `alankar_reader`, which can only `SELECT` from
`dim_menu` and `fact_sales`. Every session for this role starts read-only with a statement
timeout, and the role cannot create tables or temp tables.

```bash
psql "postgresql://<owner>@<host>:5432/<db>" -v reader_password="'choose-a-password'" \
     -f backend/sql/readonly_role.sql
```

The full script is in [backend/sql/readonly_role.sql](backend/sql/readonly_role.sql). In short:

```sql
CREATE ROLE alankar_reader LOGIN PASSWORD '...';
ALTER ROLE alankar_reader SET default_transaction_read_only = on;
ALTER ROLE alankar_reader SET statement_timeout = '15s';
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM alankar_reader;
REVOKE CREATE ON SCHEMA public FROM alankar_reader;
REVOKE TEMPORARY ON DATABASE <db> FROM PUBLIC;
GRANT CONNECT ON DATABASE <db> TO alankar_reader;
GRANT USAGE ON SCHEMA public TO alankar_reader;
GRANT SELECT ON dim_menu, fact_sales TO alankar_reader;
```

`GET /api/health` reports `role_read_only: false` if the connected role can write anything.

### 2. Environment

```bash
cp .env.example .env      # set ANTHROPIC_API_KEY and DATABASE_URL (the alankar_reader role)
```

The model and effort level for each stage, retries, row cap, cost limit and timeout are all
environment variables. See `.env.example` and `backend/config.py`.

### 3. Backend

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8000
```

- `GET  /api/health`: DB connectivity, the connected role, whether it is read-only, and the
  model for each stage
- `POST /api/chat`: `{"message": "...", "history": [{"role": "user"|"assistant", "content": "..."}]}`,
  returns a `ChatResponse`
- `POST /api/chat/stream`: same request body. Returns Server-Sent Events: `start`, one `stage`
  event per stage, then `result` carrying the `ChatResponse`

### 4. Frontend

```bash
cd frontend
npm install
npm run dev               # http://localhost:5173, proxies /api to http://127.0.0.1:8000
```

If the backend runs on another port: `API_TARGET=http://127.0.0.1:8010 npm run dev`.

## Debugging

Every stage's input and output is written at DEBUG level to `logs/alankar.jsonl`, one JSON object
per line, tagged with a `request_id`. The same id comes back in `meta.request_id`. Each response's
`meta` also carries per-stage timings, the model used, token usage, every SQL attempt with its
errors, the retry count, and the validation result. On failure, `meta.error` holds the real cause
(`stage`, `type`, `detail`) and the user sees a readable version.

## Safety layers

1. **Database role** (`alankar_reader`): SELECT on two tables only, with read-only sessions by
   default. This is the layer that actually enforces read-only.
2. **Connection**: every pooled transaction is `READ ONLY` and runs with
   `SET LOCAL statement_timeout`.
3. **Static check** (`tools/sql_check.py`): exactly one `SELECT`. It rejects write, DDL and
   locking nodes anywhere in the tree, tables or columns not in the introspected catalog,
   `SELECT *`, `dim_menu`/`fact_sales` joins on anything other than `menu_sk`, and dangerous
   functions. It adds `LIMIT 1000` when no LIMIT is given.
4. **Cost guard**: `EXPLAIN` runs before execution, and the query is rejected if the estimated
   cost is above `MAX_QUERY_COST`.
5. **Routing**: requests to change data or run user-written SQL are declined before any SQL is
   generated.

The user's text never reaches the database. Only the generated query does, after it passes the
static check.
