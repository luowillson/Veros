# Semantic Scholar Crawl Import to Google Cloud Postgres

This guide shows how to crawl Semantic Scholar references locally and import the
result into the shared Google Cloud Postgres server over Tailscale.

For the full VM setup and migration guide, see `MIGRATION.md`. This file is the
short operational checklist for teammates who already have a Google Cloud VM,
Postgres, and Tailscale running.

## Prerequisites

- Tailscale is installed and connected on your machine.
- You have the VM's Tailscale IP, usually a `100.x.y.z` address.
- Postgres is running on the VM and listening on the Tailscale interface.
- You have the database user, password, database name, and port.
- `api/.env` has `SEMANTIC_SCHOLAR_API_KEY=...`.

Do not commit `api/.env` or paste real passwords into docs, issues, or PRs.

## 1. Allow Tailscale Clients in Postgres

On the Google Cloud VM, find the active `pg_hba.conf`:

```bash
sudo -u postgres psql -c "SHOW hba_file;"
```

Edit the printed file, often:

```bash
sudo nano /etc/postgresql/16/main/pg_hba.conf
```

For the shared app database/user, add a rule like:

```conf
host    veros    veros    100.64.0.0/10    scram-sha-256
```

`100.64.0.0/10` is the Tailscale address range. If you connect as
`postgres` to database `postgres`, you need a matching rule instead:

```conf
host    postgres    postgres    100.64.0.0/10    scram-sha-256
```

Reload Postgres after editing:

```bash
sudo systemctl reload postgresql
```

## 2. Point `api/.env` at Google Cloud

In `api/.env`, set `DATABASE_URL` to the Tailscale Postgres endpoint.

For the normal app database:

```env
DATABASE_URL=postgresql+psycopg://veros:YOUR_PASSWORD@VM_TAILSCALE_IP:5432/veros?sslmode=disable
```

Example shape:

```env
DATABASE_URL=postgresql+psycopg://veros:...@100.89.186.45:5432/veros?sslmode=disable
```

Use `?sslmode=disable` when connecting through Tailscale unless the server is
explicitly configured for SSL. Tailscale already encrypts the network hop.

## 3. Test the Database Connection

From the repo:

```bash
cd /Users/jonathanxue/GitHub/Argus/api

uv run python -c "from app.config import get_settings; print(get_settings().database_url)"
```

Confirm it prints the Tailscale/Google Cloud URL, not Supabase.

Then verify the DB identity:

```bash
uv run python -c "from app.db.session import get_engine; from sqlalchemy import text; c=get_engine().connect(); print(c.execute(text('select current_database(), current_user, inet_server_addr(), inet_server_port()')).one()); c.close()"
```

Expected output should look like:

```text
('veros', 'veros', IPv4Address('100.x.y.z'), 5432)
```

## 4. Apply Migrations

Run this once per database, and again whenever migrations change:

```bash
cd /Users/jonathanxue/GitHub/Argus/api
uv run alembic upgrade head
```

This creates/updates tables such as `papers` and `paper_edges`.

## 5. Crawl Semantic Scholar

From the repo root:

```bash
cd /Users/jonathanxue/GitHub/Argus

set -a
source api/.env
set +a

python scripts/crawl_citations.py 6ae4e49db20d02f3eb30a128266b551cbfb0e8c5 \
  --max-depth -1 \
  --max-papers -1 \
  --output latentmoe_ss_crawl.json \
  --resume
```

Press `Ctrl+C` when you want to stop. The crawler catches the interrupt and
writes the JSON gathered so far.

`--resume` reads the existing output JSON and skips papers whose full reference
lists were already fetched. This prevents repeatedly hitting Semantic Scholar
for the same data.

## 6. Import the Crawl into Google Cloud Postgres

From `api/`:

```bash
cd /Users/jonathanxue/GitHub/Argus/api
uv run python scripts/import_crawled_citations.py --source ../latentmoe_ss_crawl.json
```

The importer:

- Loads `papers` and `edges` from the crawl JSON.
- Upserts papers by ID.
- Upserts citation edges by `(src_paper_id, dst_paper_id, edge_type)`.
- Skips edges whose endpoint paper is missing from the JSON.

The import can take a while for hundreds of thousands of papers and millions of
edges.

## Troubleshooting

### Import Hits Supabase Instead of Google Cloud

Symptom:

```text
aws-1-us-east-2.pooler.supabase.com
```

Fix: `api/.env` still points to Supabase. Replace `DATABASE_URL` with the
Tailscale Google Cloud URL and rerun the connection test.

### `no pg_hba.conf entry`

Symptom:

```text
no pg_hba.conf entry for host "100.x.y.z", user "veros", database "veros"
```

Fix: add a matching Tailscale rule in `pg_hba.conf`, then reload Postgres:

```conf
host    veros    veros    100.64.0.0/10    scram-sha-256
```

```bash
sudo systemctl reload postgresql
```

### `Could not parse SQLAlchemy URL`

Symptom:

```text
sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy URL
```

Fix: the `DATABASE_URL` line is malformed. Use this shape:

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DB_NAME?sslmode=disable
```

Avoid spaces, angle brackets, and unescaped special characters in the password.

### Python One-Liner `with` Syntax Error

Do not use `with engine.connect()` after semicolons in `python -c`. Use the
connection test from this guide, which manually closes the connection.

### Supabase `MaxClientsInSessionMode`

Symptom:

```text
MaxClientsInSessionMode: max clients reached
```

Fix: this means you are still hitting Supabase's pooler, not Google Cloud. Check
`DATABASE_URL`.

## Useful Verification Queries

After import, you can count rows:

```bash
cd /Users/jonathanxue/GitHub/Argus/api

uv run python -c "from app.db.session import get_engine; from sqlalchemy import text; c=get_engine().connect(); print(c.execute(text('select count(*) from papers')).one()); print(c.execute(text('select count(*) from paper_edges')).one()); c.close()"
```

You can also inspect a specific root paper:

```bash
uv run python -c "from app.db.session import get_engine; from sqlalchemy import text; c=get_engine().connect(); print(c.execute(text(\"select id, title, references_count from papers where id = '6ae4e49db20d02f3eb30a128266b551cbfb0e8c5'\")).one_or_none()); c.close()"
```
