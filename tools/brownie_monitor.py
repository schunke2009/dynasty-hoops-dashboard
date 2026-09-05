#!/usr/bin/env python3
"""Watch a restaurant menu page and shout when a sold-out item comes back.

Built for the Brownie Crunch Choc-A-Lot Cheesecake at The Cheesecake Factory
in Natick, MA, but everything worth changing is an environment variable.

Exit code is 0 for every *expected* outcome (item found, not found, sold out,
site down). A non-zero exit means the script itself broke, so a red run in the
Actions tab always means "fix me", never "still sold out".
"""

import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

MENU_URL = os.environ.get(
    "MONITOR_URL",
    "https://www.thecheesecakefactory.com/locations/natick-ma/menu",
)
# Any of these matching on the page counts as "the item is on the menu".
ITEM_PATTERNS = os.environ.get(
    "MONITOR_ITEM_PATTERNS",
    r"choc[\s\-]?a[\s\-]?lot|brownie[\s\-]?crunch",
)
STATE_PATH = os.environ.get("MONITOR_STATE_PATH", "tools/brownie_state.json")

# Words that mean "yes it's on the menu, no you cannot have it".
SOLD_OUT_MARKERS = [
    "sold out",
    "soldout",
    "out of stock",
    "currently unavailable",
    "temporarily unavailable",
    "not available",
    "unavailable",
    "86'd",
]
# How much text around an item mention to inspect for those markers.
PROXIMITY_CHARS = 300
# Consecutive runs that fail to find the item before we assume the page moved.
NOT_FOUND_PATIENCE = 6

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url):
    """Return page text, or None if the site would not talk to us."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"fetch failed: {err}", file=sys.stderr)
        return None


def to_text(html):
    """Flatten HTML to searchable text, keeping embedded JSON payloads."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    text = re.sub(r"&[a-z]+;", " ", text)
    # Menus increasingly ship as JSON inside the HTML; keep that searchable too.
    blobs = re.findall(r"(?is)<script[^>]*type=[\"']application/[^\"']*json[\"'][^>]*>(.*?)</script>", html)
    if blobs:
        text = text + " " + " ".join(blobs)
    return re.sub(r"\s+", " ", text)


def classify(text):
    """AVAILABLE / SOLD_OUT / NOT_FOUND, plus the snippet that decided it."""
    matches = list(re.finditer(ITEM_PATTERNS, text, re.IGNORECASE))
    if not matches:
        return "NOT_FOUND", ""

    snippets = []
    for match in matches:
        start = max(0, match.start() - PROXIMITY_CHARS)
        end = min(len(text), match.end() + PROXIMITY_CHARS)
        snippets.append(text[start:end])

    # Available if *any* mention of the item is free of a sold-out marker.
    for snippet in snippets:
        lowered = snippet.lower()
        if not any(marker in lowered for marker in SOLD_OUT_MARKERS):
            return "AVAILABLE", snippet.strip()
    return "SOLD_OUT", snippets[0].strip()


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def open_issue(title, body):
    """File a GitHub issue. That is what turns into an email / phone push."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY, skipping issue", file=sys.stderr)
        return
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "brownie-monitor",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"opened issue: {json.loads(response.read())['html_url']}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"issue creation failed: {err}", file=sys.stderr)


def send_webhook(message):
    """Optional extra push channel (ntfy.sh, Pushover relay, Slack, whatever)."""
    url = os.environ.get("ALERT_WEBHOOK")
    if not url:
        return
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={"Title": "Brownie Choc-A-Lot is back", "Priority": "high"},
    )
    try:
        urllib.request.urlopen(request, timeout=30).read()
        print("webhook sent")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"webhook failed: {err}", file=sys.stderr)


def summarize(line):
    print(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main():
    state = load_state()
    previous = state.get("state", "UNKNOWN")

    html = fetch(MENU_URL)
    if html is None:
        state["last_checked"] = now()
        state["consecutive_fetch_failures"] = state.get("consecutive_fetch_failures", 0) + 1
        save_state(state)
        summarize(
            f"could not reach {MENU_URL} "
            f"({state['consecutive_fetch_failures']} in a row) — state left at {previous}"
        )
        return 0

    state["consecutive_fetch_failures"] = 0
    current, snippet = classify(to_text(html))

    if current == "NOT_FOUND":
        state["consecutive_not_found"] = state.get("consecutive_not_found", 0) + 1
    else:
        state["consecutive_not_found"] = 0

    if current != previous:
        state["changed_at"] = now()
    state["state"] = current
    state["last_checked"] = now()
    state["url"] = MENU_URL
    state["snippet"] = snippet[:500]

    if current == "AVAILABLE" and previous == "UNKNOWN":
        # First ever run. We were told it is sold out, so a page that already
        # reads "available" is not a restock — it is a page that does not
        # publish per-location stock at all. Say so instead of crying wolf.
        summarize(
            "baseline run: the page shows the item as available even though it "
            "is supposed to be sold out. This page likely does not expose "
            "per-restaurant availability — point MONITOR_URL at an ordering "
            "page (DoorDash / Uber Eats / the Cheesecake Factory order site) "
            "that does."
        )
    elif current == "AVAILABLE" and previous in ("SOLD_OUT", "NOT_FOUND"):
        body = (
            f"The Brownie Choc-A-Lot is showing as available again.\n\n"
            f"- Page: {MENU_URL}\n"
            f"- Previous state: `{previous}`\n"
            f"- Detected: {state['last_checked']}\n\n"
            f"Matched context:\n\n> {snippet[:500]}\n\n"
            f"Go get it before someone else does."
        )
        open_issue("🍫 Brownie Choc-A-Lot is back in stock (Natick)", body)
        send_webhook(f"Brownie Choc-A-Lot is back at Cheesecake Factory Natick: {MENU_URL}")

    # Silence is not success: if the item vanishes from the page entirely for
    # long enough, the page changed and this monitor is watching nothing.
    if state["consecutive_not_found"] == NOT_FOUND_PATIENCE and not state.get("staleness_reported"):
        open_issue(
            "⚠️ Brownie monitor can't find the item on the page",
            f"`{MENU_URL}` has not mentioned anything matching "
            f"`{ITEM_PATTERNS}` for {NOT_FOUND_PATIENCE} consecutive checks.\n\n"
            "Either the item was pulled from the menu for good, or the page now "
            "renders its menu via JavaScript / a different URL and this monitor "
            "is watching an empty room. Point `MONITOR_URL` at an ordering page "
            "(the one that actually shows sold-out state) and reopen the watch.",
        )
        state["staleness_reported"] = True
    if state["consecutive_not_found"] == 0:
        state["staleness_reported"] = False

    save_state(state)
    summarize(f"{state['last_checked']} — {previous} -> {current} @ {MENU_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
