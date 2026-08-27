#!/usr/bin/env python3
"""
scotus_docket_watcher.py

Watches the Supreme Court's public docket search
(https://www.supremecourt.gov/docket/docket.aspx) for a list of search
terms and emails a report of newly-appearing dockets since the last run.

USAGE
    python3 scotus_docket_watcher.py
    python3 scotus_docket_watcher.py --terms-file config/terms.json
    python3 scotus_docket_watcher.py --dry-run
    python3 scotus_docket_watcher.py --always-email

Meant to be run on a schedule (GitHub Actions, cron, etc). Each run:
  1. Reads a list of search terms from a JSON config file.
  2. For each term, submits the search form on supremecourt.gov exactly as
     a browser would (this is a classic ASP.NET WebForms postback -- no
     public API exists), and pages through the results.
  3. Compares the docket numbers found against a small JSON "seen" file
     stored per search term under --state-dir.
  4. Collects everything new across all terms and, if anything is new
     (and it isn't a term's first run), emails a single combined report
     via the Resend API.
  5. Updates the seen file for every term, regardless of whether email
     was sent.

REQUIREMENTS
    pip install requests beautifulsoup4

ENVIRONMENT VARIABLES (for email)
    RESEND_API_KEY   Resend API key (required unless --dry-run)
    EMAIL_FROM       "From" address, e.g. "SCOTUS Docket Watcher <alerts@example.com>"
    EMAIL_TO         Destination address (comma-separated for multiple recipients)

KNOWN SITE LIMITATION -- read this before relying on it for a broad term
---------------------------------------------------------------------------
The docket search's own pagination is unreliable beyond roughly its first
2-3 pages (about 10-15 results): the page will keep reporting a larger
"N items found. Page X of Y" total, but the underlying rows for higher
page numbers come back empty. This reproduces identically whether you hit
it with plain HTTP requests (as this script does) or drive a real headless
browser through the same clicks, with a delay between requests, and with
full browser-style headers -- so it's a quirk/bug in the Court's own search
backend, not something this script (or bot-detection) is doing. Practically:
  - Narrow, specific search terms (a case name, a party, a narrow phrase)
    usually return few enough matches that this doesn't matter.
  - For a broad term with dozens of matches, this script will reliably see
    only the first couple of pages' worth of results each run, so a newly
    filed docket that doesn't rank in the top ~10-15 hits for your term may
    not be caught. Narrowing the term is the most reliable fix.
This script stops paging as soon as a page comes back with no new results,
so it never wastes requests spinning through the dead pages.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.supremecourt.gov/docket/docket.aspx"
RESEND_API_URL = "https://api.resend.com/emails"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 scotus-docket-watcher/1.0"
)

# Field names on the ASP.NET WebForm (as of Aug 2026 -- inspect the live
# page's HTML if this ever stops working; ASP.NET control IDs are stable
# but not guaranteed forever).
FIELD_QUERY = "ctl00$ctl00$MainEditable$mainContent$txtQuery"
FIELD_SEARCH_BTN = "ctl00$ctl00$MainEditable$mainContent$cmdSearch"
FIELD_NEXT = "ctl00$ctl00$MainEditable$mainContent$cmdNext"
NEXT_BUTTON_ID = "ctl00_ctl00_MainEditable_mainContent_cmdNext"

# NOTE: the site appears to inject small amounts of extra/random markup
# (e.g. stray <cc>...</cc> wrapper tags) between elements on some responses
# -- looks like a basic anti-scraping measure, since the exact junk varies
# request to request. The .*? gaps below tolerate that instead of requiring
# an exact literal match, which otherwise caused silent, intermittent
# under-counting.
RESULT_ROW_RE = re.compile(
    r'<a href="(https://www\.supremecourt\.gov/search\.aspx\?filename=/docket/docketfiles/html/public/[^"]+)">'
    r'.*?Docket for ([^<]+)</a>.*?Title:</td>\s*<td>\s*(.*?)\s*</td>.*?<td width="200">(.*?)</fieldset>',
    re.DOTALL,
)


class DocketSearchError(RuntimeError):
    pass


def _strip_tags(html_fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    return " ".join(text.split())


def _extract_hidden_fields(soup: BeautifulSoup) -> dict:
    """Pull every <input type=hidden> field on the page (ASP.NET viewstate
    plus the site's own tracking field) so each request looks like a real
    postback instead of a stale/hardcoded one."""
    fields = {}
    for inp in soup.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def _next_button_disabled(html: str) -> bool:
    m = re.search(r'id="%s"[^>]*class="([^"]*)"' % re.escape(NEXT_BUTTON_ID), html)
    if m:
        return "aspNetDisabled" in m.group(1)
    # If we can't find the control at all, be conservative and stop.
    return NEXT_BUTTON_ID not in html


def _parse_results_page(html: str) -> list[dict]:
    """Parse one page of docket search results into structured records."""
    results = []
    for url, docket_no, title_html, snippet_html in RESULT_ROW_RE.findall(html):
        results.append(
            {
                "docket_no": docket_no.strip(),
                "title": _strip_tags(title_html),
                "snippet": _strip_tags(snippet_html),
                "url": url,
            }
        )
    return results


def search_all_pages(query: str, delay: float = 1.5, max_pages: int = 20) -> list[dict]:
    """Run the docket search for `query` and page through results, stopping
    when the "Next" control is disabled or a page adds nothing new (see the
    KNOWN SITE LIMITATION note at the top of this file). Returns the
    combined, de-duplicated list of docket records."""
    query = query.strip()
    if len(query) < 3:
        raise DocketSearchError(f"Search term {query!r} must be at least 3 characters.")
    if "&" in query:
        raise DocketSearchError(f"Ampersand (&) is not allowed in search term {query!r}.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL,
            "Origin": "https://www.supremecourt.gov",
        }
    )

    resp = session.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    fields = _extract_hidden_fields(soup)
    fields[FIELD_QUERY] = query
    fields[FIELD_SEARCH_BTN] = "Search"

    all_results: list[dict] = []
    seen_docket_nos: set[str] = set()

    for page_num in range(1, max_pages + 1):
        resp = session.post(BASE_URL, data=fields, timeout=30)
        resp.raise_for_status()

        page_results = _parse_results_page(resp.text)
        new_count = 0
        for r in page_results:
            if r["docket_no"] not in seen_docket_nos:
                seen_docket_nos.add(r["docket_no"])
                all_results.append(r)
                new_count += 1

        if new_count == 0:
            break  # site's pager has run out of real data -- see module docstring
        if _next_button_disabled(resp.text):
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        fields = _extract_hidden_fields(soup)
        fields["__EVENTTARGET"] = FIELD_NEXT
        fields["__EVENTARGUMENT"] = ""
        fields[FIELD_QUERY] = query
        time.sleep(delay)  # be polite to a government server

    return all_results


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"docket_nos": [], "last_checked": None}


def save_state(state_path: Path, docket_nos: list[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"docket_nos": sorted(set(docket_nos)), "last_checked": datetime.now(timezone.utc).isoformat()},
            indent=2,
        )
    )


def slugify(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", term.lower()).strip("_") or "query"


def load_terms(terms_path: Path) -> list[str]:
    if not terms_path.exists():
        raise DocketSearchError(f"Terms file not found: {terms_path}")
    data = json.loads(terms_path.read_text())
    if not isinstance(data, list) or not all(isinstance(t, str) for t in data):
        raise DocketSearchError(f"Terms file {terms_path} must be a JSON array of strings.")
    terms = [t.strip() for t in data if t.strip()]
    if not terms:
        raise DocketSearchError(f"Terms file {terms_path} contains no usable search terms.")
    return terms


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_email(new_by_term: dict[str, list[dict]]) -> tuple[str, str, str]:
    """Build (subject, html_body, text_body) for the combined report."""
    total_new = sum(len(v) for v in new_by_term.values())
    num_terms = len(new_by_term)
    if num_terms == 1:
        term = next(iter(new_by_term))
        subject = f"SCOTUS Docket Watcher: {total_new} new docket(s) for “{term}”"
    else:
        subject = f"SCOTUS Docket Watcher: {total_new} new docket(s) across {num_terms} term(s)"

    html_parts = [f"<h2>{total_new} new docket(s) found</h2>"]
    text_parts = [f"{total_new} new docket(s) found\n"]
    for term, results in new_by_term.items():
        html_parts.append(f"<h3>Term: {_html_escape(term)} ({len(results)} new)</h3><ul>")
        text_parts.append(f"\nTerm: {term} ({len(results)} new)")
        for r in results:
            html_parts.append(
                f'<li><strong>{_html_escape(r["docket_no"])}</strong>: '
                f'{_html_escape(r["title"])}<br>'
                f'<a href="{_html_escape(r["url"])}">{_html_escape(r["url"])}</a>'
                f'<br><span style="color:#555">{_html_escape(r["snippet"])}</span></li>'
            )
            text_parts.append(f'  - {r["docket_no"]}: {r["title"]}\n    {r["url"]}')
        html_parts.append("</ul>")

    html_body = "\n".join(html_parts)
    text_body = "\n".join(text_parts)
    return subject, html_body, text_body


def send_email(subject: str, html_body: str, text_body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    email_from = os.environ.get("EMAIL_FROM")
    email_to = os.environ.get("EMAIL_TO")
    missing = [
        name
        for name, val in [("RESEND_API_KEY", api_key), ("EMAIL_FROM", email_from), ("EMAIL_TO", email_to)]
        if not val
    ]
    if missing:
        raise DocketSearchError(
            f"Cannot send email -- missing environment variable(s): {', '.join(missing)}"
        )

    to_addrs = [addr.strip() for addr in email_to.split(",") if addr.strip()]
    resp = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": email_from,
            "to": to_addrs,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise DocketSearchError(f"Resend API error {resp.status_code}: {resp.text}")


def main():
    parser = argparse.ArgumentParser(
        description="Watch the Supreme Court's docket search for a list of terms and email a report of new dockets since the last run."
    )
    parser.add_argument(
        "--terms-file",
        default="config/terms.json",
        help="Path to a JSON file containing an array of search terms (default: config/terms.json)",
    )
    parser.add_argument(
        "--state-dir",
        default="./state",
        help="Directory to store per-term 'seen dockets' state files (default: ./state)",
    )
    parser.add_argument(
        "--results-dir",
        help="Optional directory to dump each term's full current result set as JSON (for debugging)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress normal output; only print on error")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except actually send the email (prints what would be sent)",
    )
    parser.add_argument(
        "--always-email",
        action="store_true",
        help="Send an email even if nothing new was found (useful for a 'still alive' heartbeat)",
    )
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between paginated requests")
    parser.add_argument(
        "--term-delay", type=float, default=2.0, help="Seconds to wait between search terms"
    )
    args = parser.parse_args()

    try:
        terms = load_terms(Path(args.terms_file))
    except DocketSearchError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    state_dir = Path(args.state_dir)
    new_by_term: dict[str, list[dict]] = {}
    any_first_run = False
    had_error = False

    for i, term in enumerate(terms):
        state_path = state_dir / f"{slugify(term)}.json"
        state = load_state(state_path)
        previously_seen = set(state["docket_nos"])
        is_first_run = state["last_checked"] is None

        if not args.quiet:
            print(f"[{term!r}] searching...")

        try:
            results = search_all_pages(term, delay=args.delay)
        except DocketSearchError as e:
            print(f"[{term!r}] Error: {e}", file=sys.stderr)
            had_error = True
            continue
        except requests.RequestException as e:
            print(f"[{term!r}] Network error contacting supremecourt.gov: {e}", file=sys.stderr)
            had_error = True
            continue

        current_docket_nos = [r["docket_no"] for r in results]
        new_results = [r for r in results if r["docket_no"] not in previously_seen]

        if args.results_dir:
            out_path = Path(args.results_dir) / f"{slugify(term)}.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=2))

        if not args.quiet:
            print(f"[{term!r}] {len(results)} matching docket(s) retrieved this run.")
            if is_first_run:
                print(f"[{term!r}] first run -- baseline of {len(results)} established, nothing reported as new.")
            elif new_results:
                print(f"[{term!r}] {len(new_results)} new docket(s):")
                for r in new_results:
                    print(f"  - {r['docket_no']}: {r['title']}")
            else:
                print(f"[{term!r}] no new dockets.")

        if is_first_run:
            any_first_run = True
        elif new_results:
            new_by_term[term] = new_results

        save_state(state_path, current_docket_nos)

        if i < len(terms) - 1:
            time.sleep(args.term_delay)

    total_new = sum(len(v) for v in new_by_term.values())

    if new_by_term or args.always_email:
        if new_by_term:
            subject, html_body, text_body = build_email(new_by_term)
        else:
            subject = "SCOTUS Docket Watcher: no new dockets"
            html_body = "<p>No new dockets since last check.</p>"
            text_body = "No new dockets since last check."

        if args.dry_run:
            print("\n--- DRY RUN: would send email ---")
            print(f"Subject: {subject}")
            print(text_body)
        else:
            try:
                send_email(subject, html_body, text_body)
                if not args.quiet:
                    print(f"\nEmail sent: {subject}")
            except DocketSearchError as e:
                print(f"Error sending email: {e}", file=sys.stderr)
                had_error = True
    elif not args.quiet:
        if total_new == 0:
            print("\nNo new dockets across any term. No email sent.")
        if any_first_run:
            print("(Some terms had their first run this time -- baseline established, not emailed.)")

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
