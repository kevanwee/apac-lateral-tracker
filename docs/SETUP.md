# Setup

Everything here is free. The only step that costs money is enabling the LLM
extractor, which is optional and off by default.

## 1. Postgres (Neon free tier)

Neon's free tier is well inside what this needs: 0.5 GB storage against a
dataset measured in thousands of rows.

1. Go to **https://neon.com** and sign up (GitHub sign-in works; no card).
2. **Create a project.** Name it `apac-lateral-tracker`. For region, pick
   **Singapore (ap-southeast-1)** if offered — it is closest and the latency
   shows up on every migration.
3. On the project dashboard, open **Connection string**. Copy the
   **pooled** URI — it looks like:

   ```
   postgresql://USER:PASSWORD@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

4. Also copy the **direct** (unpooled) URI — same host without `-pooler`.
   Migrations need it, because pooled connections do not reliably support
   session-level DDL.

### Supabase instead

Works the same way. Project → **Settings → Database → Connection string → URI**.
Use the **Session pooler** string for `DATABASE_URL` and the **direct** one for
`DATABASE_URL_DIRECT`. Note Supabase pauses a free project after a week idle;
Neon resumes on connect, which suits a pipeline run every few months better.

## 2. Local configuration

Create `.env` in the repo root. **It is gitignored; never commit it.**

```bash
DATABASE_URL=postgresql://USER:PASSWORD@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
DATABASE_URL_DIRECT=postgresql://USER:PASSWORD@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require

# Required. Must carry a reachable contact address or the pipeline refuses to
# start — this is how an outlet reaches you if collection causes a problem.
TRACKER_USER_AGENT=apac-lateral-tracker/0.1 (+https://github.com/kevanwee/apac-lateral-tracker; contact: kevanwee@gmail.com)
```

That is the complete set for the free path. `ANTHROPIC_API_KEY` is **not**
needed unless you run `--extractor llm` or `--extractor auto`.

## 3. Create the schema and seed it

```bash
python -m pip install -e .

tracker db migrate      # 10 migrations, ~22 tables
tracker db status       # every migration should read "applied"
tracker firms --sync    # 146 firms and their aliases
tracker sources sync    # the source register from config/sources.yaml
```

`tracker firms --sync` matters more than it looks. Without a seeded gazetteer
every extracted firm is unknown, gets created as `other`, and sends its move to
the review queue — so an unseeded database routes effectively everything to a
human.

## 4. Load history

```bash
tracker backfill --since 2019-01-01     # archives; hours, resumable
tracker extract --extractor rules       # free, deterministic
tracker status                          # runs, queue depth, quiet sources
```

Both stages are idempotent — `raw_items.url` is unique and extraction is keyed
on a per-(item, person) fingerprint — so an interrupted run is continued by
re-running the same command. Nothing is duplicated.

The backfill is slow on purpose: one request per source every 10 seconds, and
the Australasian Lawyer archive needs one request per article to reach the
names. Budget a few hours. It only happens once.

## 5. Keeping it current

```bash
tracker catch-up        # every few months; feeds + archives + extraction
```

Defaults to the window since the last successful run, with two weeks of
deliberate overlap because outlets publish late.

In GitHub Actions this is **Actions → Catch up → Run workflow**. Set these
repository secrets first (Settings → Secrets and variables → Actions):

| Secret | Needed for |
|---|---|
| `DATABASE_URL`, `DATABASE_URL_DIRECT` | always |
| `ANTHROPIC_API_KEY` | only `--extractor llm` / `auto` |
| `IMAP_USERNAME`, `IMAP_PASSWORD` | only the ALB newsletter source |

and one **variable**: `TRACKER_USER_AGENT`.

The quarterly schedule only opens a reminder issue — it never runs collection
unattended.

## 6. Optional: the ALB newsletter

Asian Legal Business blocks automated clients outright (403 on every path,
including `robots.txt`), so it is not crawled. It does send its newsletter to
subscribers, and reading mail addressed to you is not circumvention.

1. Subscribe at legalbusinessonline.com with an address you control.
2. Create a mail folder and filter the newsletter into it. The adapter reads
   **only** that folder, **read-only**, filtered to a sender allowlist — it
   never touches the rest of the mailbox and has no code path that writes.
3. Set `IMAP_USERNAME` and `IMAP_PASSWORD` in `.env`. Use an **app-specific
   password**, not your account password.
4. In `config/sources.yaml`, set `active: true` on `alb-newsletter` and check
   `options.folder` matches the folder you made.

## Running the tests

The suite **drops and recreates the `public` schema**, so it needs a throwaway
database, never the one holding your data. It refuses to run against a
non-local host:

```
DestructiveTestGuard: Refusing to run destructive tests against
'ep-xxxx-pooler.ap-southeast-1.aws.neon.tech'.
```

That guard exists because it has already gone wrong once: with a production
`DATABASE_URL` in `.env`, `pytest` dropped the live schema.

To run the database tests locally, point them at a disposable Postgres:

```bash
docker run -d -e POSTGRES_PASSWORD=pg -p 5432:5432 postgres:16
TRACKER_TEST_DATABASE_URL=postgresql://postgres:pg@localhost/postgres pytest
```

Without that, the database tests error out and the rest still run. CI uses a
`localhost` service container, which the guard allows.

## Without a database

`tracker collect --since 2019-01-01 --fetch-articles --out collected.json`
runs the identical fetch → gate → extract path entirely in memory and writes
JSON. Useful for seeing a window's real yield before committing to anything.
It is what produced the current dataset.
