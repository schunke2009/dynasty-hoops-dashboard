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
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MENU_URL = (
    os.environ.get("MONITOR_URL")
    or "https://www.thecheesecakefactory.com/locations/natick-ma/menu"
)
# Any of these matching on the page counts as "the item is on the menu".
ITEM_PATTERNS = os.environ.get(
    "MONITOR_ITEM_PATTERNS",
    r"choc[\s\-]?a[\s\-]?lot|brownie[\s\-]?crunch",
)
STATE_PATH = os.environ.get("MONITOR_STATE_PATH") or "tools/brownie_state.json"
# The page must prove it is the right restaurant before any result is trusted.
LOCATION_TOKEN = os.environ.get("MONITOR_LOCATION") or "Natick"

# Ad/session junk that does not affect the page but does leak who you are.
TRACKING_PARAMS = (
    "gclid", "gclsrc", "gbraid", "wbraid", "kclickid", "fbclid", "msclkid",
    "web_consumer_id", "ignore_splash_experience", "_gl", "irclickid",
)
TRACKING_PREFIXES = ("utm_", "gad_")

# Visible words meaning "on the menu, but you cannot have it".
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
# A greyed-out item usually says nothing at all — it carries a disabled class,
# an ARIA attribute, or a false availability flag in an embedded JSON payload.
# These are matched against raw HTML near the item, not the flattened text.
MARKUP_SOLD_OUT_MARKERS = [
    'aria-disabled="true"',
    "aria-disabled='true'",
    "sold-out",
    "sold_out",
    "soldout",
    "out-of-stock",
    "out_of_stock",
    "outofstock",
    "is-disabled",
    "is-unavailable",
    "isunavailable",
    "not-available",
    '"available":false',
    '"isavailable":false',
    '"is_available":false',
    '"instock":false',
    '"in_stock":false',
    '"soldout":true',
    '"sold_out":true',
    '"outofstock":true',
    '"disabled":true',
    'data-available="false"',
    'data-disabled="true"',
    # Confirmed live on this site: the card carries
    # class="c-product-card menu product112094280 disabled" while the item is
    # out. Whitespace is stripped before matching, so this hits a class list
    # ending in "disabled" without matching aria-/data-disabled="...".
    'disabled"',
]
# Tune these from a real page dump without editing code:
#   MONITOR_SOLDOUT_MARKERS="css-1x2y3z,greyed-item"
MARKUP_SOLD_OUT_MARKERS += [
    marker.strip().lower()
    for marker in (os.environ.get("MONITOR_SOLDOUT_MARKERS") or "").split(",")
    if marker.strip()
]
# Markup is dense, so look in a tighter window than the prose scan uses.
MARKUP_PROXIMITY_CHARS = 400

# A greyed-out card on this site is also not clickable, which is a far more
# stable signal than any class name. To avoid reading "no link" as "sold out"
# on a page that simply has no links (a JS app, a bot wall), we calibrate
# against a control item that is always orderable: if the control is clickable
# and the target is not, the target is out. If the control is not clickable
# either, the heuristic abstains and the marker passes decide.
CONTROL_PATTERN = os.environ.get("MONITOR_CONTROL_PATTERN") or r"salted[\s\-]?caramel"
LINK_RE = re.compile(r"<a\b[^>]*\bhref\s*=", re.IGNORECASE)
# How much text around an item mention to inspect for those markers.
PROXIMITY_CHARS = 300
# Consecutive runs that fail to find the item before we assume the page moved.
NOT_FOUND_PATIENCE = 6
# An unbroken sold-out streak this long is suspicious enough to ask a human.
STUCK_DAYS = int(os.environ.get("MONITOR_STUCK_DAYS") or 10)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def clean_url(url):
    """Drop ad-click and session identifiers before we fetch or store a URL."""
    parts = urllib.parse.urlsplit(url)
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key not in TRACKING_PARAMS
        and not key.startswith(TRACKING_PREFIXES)
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(kept), "")
    )


def url_warning(url):
    """Catch URLs that cannot possibly pin a single restaurant."""
    path = urllib.parse.urlsplit(url).path
    if "doordash.com" in url and path.startswith("/business/"):
        return ("this is a DoorDash *brand* page, not a store page. It resolves "
                "to whatever store matches the visitor's address, which from a "
                "CI runner is not Natick. Use a /store/... URL.")
    if "ubereats.com" in url and "/store/" not in path:
        return "this Uber Eats URL is not a /store/... page, so it pins no restaurant."
    return ""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


LAST_RESPONSE = {}
# The menu is an Angular app, so a plain fetch returns an empty shell. Render
# unless explicitly told not to (MONITOR_RENDER=plain for the raw HTML).
RENDER = (os.environ.get("MONITOR_RENDER") or "browser") != "plain"


def fetch_rendered(url):
    """Load the page in a real browser and return the DOM after it settles."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            page = browser.new_page(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1280, "height": 2400},
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            LAST_RESPONSE.update(
                status=response.status if response else None,
                final_url=page.url,
                content_type="rendered",
            )
            # The menu arrives from an XHR after load, so wait for the item
            # itself; falling back to network idle if the name never shows.
            try:
                page.wait_for_function(
                    "() => /choc[\\s-]?a[\\s-]?lot|brownie[\\s-]?crunch/i"
                    ".test(document.body.innerText)",
                    timeout=30000,
                )
            except Exception:
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
            return page.content()
        finally:
            browser.close()


def fetch(url):
    """Return page text, or None if the site would not talk to us."""
    if RENDER:
        try:
            return fetch_rendered(url)
        except Exception as err:  # noqa: BLE001 - any browser failure falls back
            print(f"render failed ({err}); falling back to a plain fetch",
                  file=sys.stderr)
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
            LAST_RESPONSE.update(
                status=response.status,
                final_url=response.geturl(),
                content_type=response.headers.get("Content-Type", ""),
            )
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


def mentions(haystack, pattern=None):
    """Every match of the pattern (the item, by default)."""
    return list(re.finditer(pattern or ITEM_PATTERNS, haystack, re.IGNORECASE))


def windows(haystack, radius, pattern=None):
    """Text around every mention of the pattern."""
    return [
        haystack[max(0, m.start() - radius):min(len(haystack), m.end() + radius)]
        for m in mentions(haystack, pattern)
    ]


def inside_link(html, position):
    """Is this offset inside an <a href=...> ... </a>?

    Walks backwards for the nearest anchor boundary. A window-based check is
    not good enough here: adjacent menu cards sit close together, so a
    neighbouring item's link lands inside the window and reads as our own.
    """
    before = html[:position]
    opened = -1
    for match in LINK_RE.finditer(before):
        opened = match.start()
    return opened > before.rfind("</a>")


def clickable(html, pattern=None):
    """True if any mention of the pattern sits inside a link."""
    return any(inside_link(html, m.start()) for m in mentions(html, pattern))


def classify(text, html=""):
    """AVAILABLE / SOLD_OUT / NOT_FOUND, plus the snippet that decided it.

    Three passes, because "sold out" and "greyed out" look nothing alike:
    disabled markers in the raw markup, sold-out wording in the prose, and
    finally whether the item is clickable at all.
    """
    prose = windows(text, PROXIMITY_CHARS)
    markup = windows(html, MARKUP_PROXIMITY_CHARS) if html else []
    if not prose and not markup:
        return "NOT_FOUND", ""

    # A disabled control is the strongest signal on the page, so it wins.
    for snippet in markup:
        lowered = re.sub(r"\s+", "", snippet.lower())
        hit = next((m for m in MARKUP_SOLD_OUT_MARKERS
                    if m.replace(" ", "") in lowered), None)
        if hit:
            return "SOLD_OUT", f"[markup marker: {hit}] {snippet.strip()}"

    for snippet in prose:
        lowered = snippet.lower()
        hit = next((m for m in SOLD_OUT_MARKERS if m in lowered), None)
        if hit:
            return "SOLD_OUT", f"[text marker: {hit}] {snippet.strip()}"

    # Greyed out and unclickable: no marker to match, so compare against an
    # item known to be orderable. Abstains when the control is not a link
    # either, which is what a JavaScript shell or a bot wall looks like.
    if html and CONTROL_PATTERN and clickable(html, CONTROL_PATTERN):
        if not clickable(html):
            return "SOLD_OUT", (
                "[item is not a link, while the control item is] "
                + (markup[0].strip() if markup else "")
            )

    # Nothing anywhere says you cannot have it.
    return "AVAILABLE", (prose or markup)[0].strip()


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def signature(state):
    """The parts of the state worth a commit. Excludes the clock."""
    return {k: v for k, v in state.items() if k not in ("last_checked", "snippet")}


def report_change(changed):
    """Tell the workflow whether this run is worth committing."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")


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


def post(url, data, headers, label):
    """POST and report success by name, so the log says which channel fired."""
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        urllib.request.urlopen(request, timeout=30).read()
        print(f"push sent via {label}")
        return True
    except urllib.error.HTTPError as err:
        try:
            detail = err.read().decode("utf-8", errors="replace")[:400].strip()
        except OSError:
            detail = ""
        print(f"push via {label} failed: {err} {detail}", file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError, UnicodeError, ValueError) as err:
        print(f"push via {label} failed: {err}", file=sys.stderr)
        return False


def push_pushover(title, message, link):
    """Pushover: custom alert sound, and optional retry-until-acknowledged."""
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return False
    # Priority 2 is the obnoxious one: it re-alerts until you tap it.
    priority = os.environ.get("PUSHOVER_PRIORITY") or "1"
    fields = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
        "url": link,
        "url_title": "Open the menu",
        "priority": priority,
        "sound": os.environ.get("PUSHOVER_SOUND") or "persistent",
    }
    if priority == "2":
        fields["retry"] = "60"      # re-alert every minute...
        fields["expire"] = "3600"   # ...for an hour, or until acknowledged.
    return post(
        "https://api.pushover.net/1/messages.json",
        urllib.parse.urlencode(fields).encode("utf-8"),
        {"Content-Type": "application/x-www-form-urlencoded"},
        "pushover",
    )


def push_ntfy(title, message, link):
    """ntfy.sh: free, no account, and honors max priority on the phone."""
    url = os.environ.get("ALERT_WEBHOOK")
    if not url:
        return False
    lowered = url.lower()
    if "hooks.slack.com" in lowered:
        return post(url, json.dumps({"text": f"{title} — {message}"}).encode("utf-8"),
                    {"Content-Type": "application/json"}, "slack")
    if "discord.com/api/webhooks" in lowered:
        return post(url, json.dumps({"content": f"**{title}** {message}"}).encode("utf-8"),
                    {"Content-Type": "application/json"}, "discord")
    # HTTP headers are latin-1 only, so the emoji rides in Tags, not Title.
    ascii_title = title.encode("ascii", "ignore").decode().strip() or "Stock alert"
    headers = {
        "Title": ascii_title,
        "Priority": os.environ.get("NTFY_PRIORITY") or "max",
        "Tags": "cake,tada",
        "Click": link,
    }
    body = f"{message}\n\n{link}".encode("utf-8")
    delivered = post(url, body, headers, "ntfy")

    # Email goes as a SEPARATE request. Bundling it cost us a whole alert
    # once: ntfy rejects the entire message if it dislikes the address, so a
    # bad recipient silently took the push down with it.
    recipient = os.environ.get("ALERT_EMAIL")
    if recipient:
        delivered |= post(
            url,
            body,
            {"Title": ascii_title, "Email": recipient.strip(), "Tags": "cake"},
            "ntfy-email",
        )
    return delivered


def push_alert(title, message, link):
    """Fire every configured push channel. Returns True if any got through."""
    channels = [push_pushover(title, message, link), push_ntfy(title, message, link)]
    if not any(channels):
        print(
            "no push channel configured or all failed — the GitHub issue is "
            "the only notification for this alert",
            file=sys.stderr,
        )
    return any(channels)


def summarize(line):
    print(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main():
    global MENU_URL
    MENU_URL = clean_url(MENU_URL)
    baseline = signature(load_state())

    if os.environ.get("TEST_PUSH") == "true":
        delivered = push_alert(
            "Brownie monitor test",
            "This is what a restock alert will look like. If you can read this "
            "on your phone, the loud part works.",
            MENU_URL,
        )
        summarize("test push delivered" if delivered else "test push reached nobody")
        return 0

    state = load_state()
    previous = state.get("state", "UNKNOWN")

    html = fetch(MENU_URL)
    if html is None:
        state["last_checked"] = now()
        state["consecutive_fetch_failures"] = state.get("consecutive_fetch_failures", 0) + 1
        save_state(state)
        report_change(signature(state) != baseline)
        summarize(
            f"could not reach {MENU_URL} "
            f"({state['consecutive_fetch_failures']} in a row) — state left at {previous}"
        )
        return 0

    state["consecutive_fetch_failures"] = 0
    text = to_text(html)

    if os.environ.get("DEBUG_DUMP") == "true":
        with open("debug_page.html", "w", encoding="utf-8") as handle:
            handle.write(html)
        # The artifact is not reachable from every environment, so put the
        # forensics in the log where they can always be read.
        summarize(f"debug: HTTP {LAST_RESPONSE.get('status')} "
                  f"{LAST_RESPONSE.get('content_type')}")
        summarize(f"debug: final URL after redirects: "
                  f"{LAST_RESPONSE.get('final_url')}")
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        summarize(f"debug: title: {title.group(1).strip() if title else '(none)'}")
        for framework in ("__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__",
                          "window.__INITIAL_STATE__", "data-reactroot"):
            if framework in html:
                summarize(f"debug: found {framework}")
        scripts = re.findall(r'(?i)<script[^>]+src=["\']([^"\']+)["\']', html)
        summarize(f"debug: {len(scripts)} script src(s): "
                  + ", ".join(scripts[:15]))
        endpoints = sorted(set(re.findall(
            r'(?i)["\'](/[a-z0-9_\-/]*(?:api|menu|location|graphql)[a-z0-9_\-/]*)["\']',
            html)))
        summarize(f"debug: candidate endpoints in page: {endpoints[:25]}")
        if os.environ.get("DISCOVER_API") == "true":
            # An Angular/React shell keeps its API base inside the JS bundle.
            # A JSON availability flag beats inferring stock from CSS, so it
            # is worth digging the endpoint out once.
            seen = set()
            for src in scripts:
                if not src.endswith(".js"):
                    continue
                base = re.search(r'(?i)<base[^>]+href=["\']([^"\']+)["\']', html)
                root = urllib.parse.urljoin(
                    LAST_RESPONSE.get("final_url") or MENU_URL,
                    base.group(1) if base else "/",
                )
                bundle_url = urllib.parse.urljoin(root, src)
                if "unpkg.com" in bundle_url or "cloudflareinsights" in bundle_url:
                    continue
                bundle = fetch(bundle_url)
                if not bundle:
                    summarize(f"debug: could not fetch {bundle_url}")
                    continue
                hits = set(re.findall(
                    r"https?://[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~:/?#@!$&*+,;=%-]*)?", bundle))
                hits |= set(re.findall(r'["\'](/(?:api|v\d)/[A-Za-z0-9._~/{}-]+)["\']', bundle))
                interesting = sorted(
                    h for h in hits
                    if re.search(r"api|menu|location|graphql|restaurant", h, re.I)
                    and not re.search(r"w3\.org|schema\.org|googleapis|gstatic|"
                                      r"doubleclick|facebook|adobe|typekit", h, re.I)
                )
                new = [h for h in interesting if h not in seen]
                seen.update(new)
                summarize(f"debug: {os.path.basename(bundle_url)} "
                          f"({len(bundle)} bytes) -> {len(new)} candidate(s)")
                for hit in new[:40]:
                    summarize(f"    {hit}")

        if os.environ.get("DEBUG_FULL") == "true":
            summarize("debug: ---- BEGIN PAGE ----")
            summarize(html[:60000])
            summarize("debug: ---- END PAGE ----")
        found = windows(html, MARKUP_PROXIMITY_CHARS)
        summarize(f"debug: fetched {len(html)} bytes, {len(found)} item mention(s)")
        summarize(f"debug: page mentions '{LOCATION_TOKEN}': "
                  f"{LOCATION_TOKEN.lower() in text.lower()}")
        for index, snippet in enumerate(found[:3], 1):
            summarize(f"\n<details><summary>markup around mention {index}"
                      f"</summary>\n\n```html\n{snippet[:1500]}\n```\n</details>")
        if not found:
            summarize("debug: item never appears in the HTML — the menu is "
                      "almost certainly rendered by JavaScript after load.")

    if LOCATION_TOKEN.lower() not in text.lower():
        # Wrong store, a geo-redirect, or a bot wall. Any of those make an
        # "available" reading meaningless, so refuse to report one at all.
        misses = state.get("consecutive_wrong_location", 0) + 1
        state.update({
            "consecutive_wrong_location": misses,
            "state": "LOCATION_UNCONFIRMED",
            "last_checked": now(),
            "url": MENU_URL,
        })
        save_state(state)
        hint = url_warning(MENU_URL)
        summarize(
            f"the page never mentions '{LOCATION_TOKEN}' ({misses} runs in a "
            f"row) — not reporting stock from a page that cannot prove which "
            f"restaurant it is describing." + (f" Likely cause: {hint}" if hint else "")
        )
        if misses == NOT_FOUND_PATIENCE and not state.get("location_reported"):
            open_issue(
                f"⚠️ Brownie monitor cannot confirm the {LOCATION_TOKEN} location",
                f"`{MENU_URL}` has not mentioned `{LOCATION_TOKEN}` in "
                f"{NOT_FOUND_PATIENCE} consecutive checks.\n\n"
                + (f"{hint}\n\n" if hint else "")
                + "Until the page proves it is the right restaurant, this "
                "monitor will not report stock, because an 'available' reading "
                "from the wrong store is worse than no reading at all."
            )
            state["location_reported"] = True
            save_state(state)
        report_change(signature(state) != baseline)
        return 0

    state["consecutive_wrong_location"] = 0
    state["location_reported"] = False
    current, snippet = classify(text, html)

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
        push_alert(
            "🍫 Brownie Choc-A-Lot is BACK",
            "Available again at Cheesecake Factory Natick. Go.",
            MENU_URL,
        )

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

    if current != previous:
        state["stuck_reported"] = False
    if current == "SOLD_OUT" and not state.get("stuck_reported"):
        try:
            streak = datetime.now(timezone.utc) - datetime.fromisoformat(state["changed_at"])
        except (KeyError, ValueError):
            streak = None
        if streak is not None and streak.days >= STUCK_DAYS:
            open_issue(
                "🤔 Brownie monitor has read sold out for "
                f"{streak.days} days straight",
                f"`{MENU_URL}` has reported `SOLD_OUT` without a break since "
                f"{state['changed_at']}.\n\nThat is possible, but it is also "
                "what a detector matching the wrong element looks like. Worth "
                "eyeballing the page once.\n\nMatched context:\n\n> "
                f"{state.get('snippet', '')[:500]}",
            )
            state["stuck_reported"] = True

    save_state(state)
    report_change(signature(state) != baseline)
    if not (os.environ.get("ALERT_WEBHOOK") or os.environ.get("PUSHOVER_TOKEN")):
        summarize(
            "warning: no phone push configured (set ALERT_WEBHOOK or "
            "PUSHOVER_TOKEN/PUSHOVER_USER) — a restock will only open a GitHub issue"
        )
    summarize(f"{state['last_checked']} — {previous} -> {current} @ {MENU_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
