# SCOTUS Docket Watcher

Watches the Supreme Court's public docket search
(https://www.supremecourt.gov/docket/docket.aspx) for a list of search
terms and emails a report of newly-appearing dockets since the last run.

Runs on a GitHub Actions schedule (every 15 minutes by default) and emails
via [Resend](https://resend.com).

## How it works

1. `config/terms.json` lists the search terms to track.
2. On each run, the script submits the docket search form for every term
   (the site has no public API — this replicates the browser's postback)
   and pages through the results.
3. It compares docket numbers found against a per-term "seen" file in
   `state/`, which is committed back to the repo by the workflow after
   every run so history persists between runs.
4. Anything new (that isn't a term's first run) goes into one combined
   email sent via the Resend API.

A term's very first run establishes a baseline (nothing is emailed for
it) — otherwise every existing docket matching a newly-added term would
show up as "new" the first time it's checked.

## Setup

### 1. Add repository secrets

In this repo's **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `RESEND_API_KEY` | Your Resend API key |
| `EMAIL_FROM` | Sender address, e.g. `SCOTUS Docket Watcher <alerts@yourdomain.com>` (must be a verified Resend domain) |
| `EMAIL_TO` | Destination address(es), comma-separated for multiple recipients |

### 2. Edit the tracked terms

Edit `config/terms.json` — a JSON array of strings:

```json
[
  "Sherrod Brown"
]
```

Terms must be at least 3 characters and cannot contain `&` (site
limitation). Adding a new term will run once as a silent baseline before
it starts reporting new dockets.

### 3. Enable the workflow

The workflow at `.github/workflows/docket-watch.yml` runs automatically
on its schedule once merged to the default branch. You can also trigger
it manually from the **Actions** tab (`workflow_dispatch`).

## Running locally

```bash
pip install -r requirements.txt
export RESEND_API_KEY=...
export EMAIL_FROM="SCOTUS Docket Watcher <alerts@yourdomain.com>"
export EMAIL_TO=you@example.com
python3 scotus_docket_watcher.py --dry-run   # prints what it would email, sends nothing
python3 scotus_docket_watcher.py             # sends real email if anything new is found
```

Useful flags:

- `--terms-file PATH` — use a different terms file (default `config/terms.json`)
- `--state-dir PATH` — where per-term "seen docket" state lives (default `./state`)
- `--dry-run` — do everything except actually send the email
- `--always-email` — send an email every run, even with nothing new (heartbeat)
- `--results-dir PATH` — dump each term's full current result set as JSON for debugging
- `--quiet` — suppress normal stdout output

## Known site limitation

The docket search's own pagination is unreliable beyond roughly its first
2–3 pages (about 10–15 results): the page keeps reporting a larger "N
items found. Page X of Y" total, but rows for higher page numbers come
back empty. This reproduces identically over plain HTTP requests or a
real headless browser, with delays and full browser headers — it's a
quirk/bug in the Court's own search backend, not something this script
(or bot detection) is doing.

Practically:
- Narrow, specific terms (a case name, a party, a narrow phrase) usually
  return few enough matches that this doesn't matter.
- A broad term with dozens of matches will reliably only surface the
  first couple of pages' worth of results each run — a newly filed
  docket that doesn't rank in the top ~10–15 hits for that term may be
  missed. Narrowing the term is the most reliable fix.

The script stops paging as soon as a page returns no new results, so it
never wastes requests spinning through the dead pages.
