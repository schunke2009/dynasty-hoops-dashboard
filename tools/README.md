# Brownie Choc-A-Lot stock monitor

Hourly watch on the Brownie Crunch Choc-A-Lot Cheesecake at The Cheesecake
Factory in Natick, MA. When the page stops reading as sold out, the workflow
opens a GitHub issue — which is what turns into an email and a phone push if
you have the GitHub mobile app installed.

- Workflow: [`.github/workflows/brownie-stock-monitor.yml`](../.github/workflows/brownie-stock-monitor.yml)
- Checker: [`brownie_monitor.py`](brownie_monitor.py) (stdlib only, no deps)
- State: `brownie_state.json`, committed by the workflow after each run

## Two things to know

1. **Scheduled runs only fire from the default branch,** and even then GitHub
   treats `cron` as best-effort. Of this workflow's first five slots, one ran,
   90 minutes late. That is normal for Actions: schedules are the lowest
   priority queue and slots get delayed or dropped under load. The workflow
   asks twice an hour so a dropped slot costs half an hour of coverage rather
   than a whole one. If you need guaranteed timing, drive it from an external
   scheduler via `repository_dispatch` instead. `workflow_dispatch` ("Run
   workflow") works from any branch and fires immediately.
2. **The menu page may not publish per-restaurant availability.** Marketing
   menus usually list the whole national menu regardless of what any one
   kitchen has left. The first run says so out loud if it sees the item as
   available while you know it is sold out — if that happens, repoint
   `MONITOR_URL` at a page that carries real stock state (the Cheesecake
   Factory ordering site, DoorDash, or Uber Eats for the Natick store).

## What the live page actually looks like

Confirmed from a rendered run on 2026-09-05. The menu is an **Angular app** —
a plain fetch returns a 23 KB shell with no menu in it at all — so the checker
drives headless Chromium and waits for the item to appear. While the item is
out, its card renders as:

```html
<div class="c-product-card menu product112094280 disabled">
  <span class="c-product-card__name">
    Brownie Crunch Choc-a-Lot Cheesecake (Temporarily Unavailable)
  </span>
  <span class="c-product-card__cost">$13.50</span>
```

Two independent signals, either of which is enough: the `disabled` class on the
card, and the `(Temporarily Unavailable)` suffix on the name. When it comes
back both disappear and the card becomes a link.

## How "sold out" is detected

The item does not have to say the words. Detection runs two passes:

1. **Markup pass** (raw HTML within 400 chars of the item): disabled classes
   and attributes — `is-disabled`, `sold-out`, `out-of-stock`, `aria-disabled`,
   `data-available="false"` — plus false availability flags in embedded JSON
   (`"available":false`, `"inStock":false`, …). This is what a *greyed out*
   item actually looks like in the DOM.
2. **Text pass** (flattened prose within 300 chars): "sold out", "currently
   unavailable", "86'd", and friends.

3. **Clickability pass**: on this site a greyed-out card is also not a link.
   The item counts as unavailable when its name does **not** sit inside an
   `<a href>` while a control item that is always orderable
   (`MONITOR_CONTROL_PATTERN`, default Salted Caramel Cheesecake) does. If the
   control is not a link either, the check abstains — that is what a
   JavaScript shell or a bot wall looks like, and guessing there would be
   worse than saying nothing.

Earlier passes win. Only when all three find nothing does the item count as
available. Clickability is checked by walking back from the item name to the
nearest anchor boundary, not by scanning a window — adjacent menu cards sit
close enough that a neighbour's link otherwise reads as our own.

### If the real page uses a class I did not guess

Run the workflow with **debug** ticked. It dumps the fetched HTML as a
downloadable artifact and prints the markup around each item mention to the job
summary. Find the class the greyed-out item carries, then add it as a repo
**variable** `MONITOR_SOLDOUT_MARKERS` (comma-separated) — no code change
needed.

Because the item is known to be sold out right now, the very first run is a
free test: **it should report `SOLD_OUT`.** If it reports `AVAILABLE`, the
detector is missing the greyed-out signal and needs a marker from a debug dump.
A monitor that thinks a sold-out item is available will never tell you anything
useful, so treat that first result as the real acceptance test.

If a sold-out streak runs past `MONITOR_STUCK_DAYS` (default 10) with no
change, it opens an issue asking you to eyeball the page — an eternal
`SOLD_OUT` is more often a broken selector than a cursed dessert.

## The URL has to pin one restaurant

The monitor refuses to report stock from a page that cannot prove which
restaurant it is describing. Every fetched page must contain `Natick`
(`MONITOR_LOCATION`); if it does not, the run records `LOCATION_UNCONFIRMED`
and stays silent rather than reporting availability from the wrong store.

That rules out two URLs people naturally reach for:

| URL shape | Verdict |
| --- | --- |
| `doordash.com/business/the-cheesecake-factory-105/` | ❌ brand landing page — resolves to whatever store matches the *visitor's* address |
| `thecheesecakefactory.com/locations/natick-ma/menu` | ⚠️ marketing menu — lists the national menu, likely never says "sold out" |
| `doordash.com/store/the-cheesecake-factory-natick-<id>/` | ✅ one store, real availability |
| `ubereats.com/store/.../<uuid>` | ✅ one store, real availability |

To get the right one: open the DoorDash or Uber Eats **app**, set your address,
search Cheesecake Factory, pick the **Natick** result, open the Brownie
Choc-A-Lot item, then share → copy link. The URL must contain `/store/`.

Ad-click and session parameters (`utm_*`, `gclid`, `gad_*`, `web_consumer_id`,
…) are stripped before the page is fetched and before the URL is written to the
state file, so a link copied out of a Google ad does not commit your DoorDash
session id to the repo.

## Getting it loud, on your phone

The GitHub issue is the backstop. For an actual buzz in your pocket, pick one
(or both) and add it under **Settings → Secrets and variables → Actions**.

### ntfy.sh — free, no account, 60 seconds

1. Install the **ntfy** app (iOS / Android).
2. Subscribe to a topic nobody else will guess, e.g. `brownie-natick-bd511720`.
   Anyone who knows a topic name can read and post to it, so do not use
   something like `brownie`.
3. Add repo secret `ALERT_WEBHOOK` = `https://ntfy.sh/brownie-natick-bd511720`

Sends at `max` priority, which pings loudly and pops over Do Not Disturb on
Android. Override with the `NTFY_PRIORITY` env var if that is too much.

### Email and SMS, without relying on push at all

ntfy can deliver the same alert as an email, server-side. Add a repo secret:

| Name | Value |
| --- | --- |
| `ALERT_EMAIL` | where to send it |

Point it at a normal inbox, or at a carrier email-to-SMS gateway to receive an
actual text message:

| Carrier | Gateway |
| --- | --- |
| Verizon | `5085551234@vtext.com` |
| AT&T | `5085551234@txt.att.net` |
| T-Mobile | `5085551234@tmomail.net` |
| Google Fi | `5085551234@msg.fi.google.com` |

This path never touches APNs, so it works even when the phone refuses to show
a push banner. ntfy.sh rate-limits outbound email for anonymous users, which is
irrelevant here — this alert fires roughly never.

### Pushover — ~$5 one-time, the loudest option

1. Install Pushover, create an application, note the user key and API token.
2. Add repo secrets `PUSHOVER_USER` and `PUSHOVER_TOKEN`.
3. Optional: add repo **variable** `PUSHOVER_PRIORITY` = `2`. That is emergency
   priority — it re-alerts every 60 seconds for an hour until you tap it, and
   it ignores your phone's silent switch. Genuinely obnoxious, which is the
   point when there is cheesecake on the line.

`ALERT_WEBHOOK` also detects Slack and Discord webhook URLs and posts the
right JSON shape for those instead.

### Prove it works before you need it

Actions → **Brownie Choc-A-Lot stock monitor** → **Run workflow** → tick
**"Send a test notification instead of checking stock"**. Your phone should
buzz within a few seconds. If it does not, fix that now rather than finding
out during the one hour the cheesecake exists.

Every normal run also prints a warning to the job summary when no push channel
is configured, so a silent monitor cannot masquerade as a working one.

## Hourly status reports

By default the monitor posts the result of **every** run, not just changes:

- `🚫 Still sold out` — the usual case
- `✅ Still available` — it is orderable and you already know
- `❓ Not on the menu` / `⚠️` variants — something is off with the page

A restock still sends the distinct `🍫 Brownie Choc-A-Lot is BACK` alert, and
that run skips the routine heartbeat so you never get two messages at once.

To go back to alert-only silence, add a repo **variable** (not a secret)
`REPORT_EVERY_RUN` = `false`.

## Turning it off

Actions tab → the workflow → **⋯** → **Disable workflow**. Or delete
`.github/workflows/brownie-stock-monitor.yml`. The state file is harmless
either way.

## Knobs

All optional, all environment variables:

| Variable | Default | What it does |
| --- | --- | --- |
| `MONITOR_URL` | Natick menu page | Page to check |
| `MONITOR_ITEM_PATTERNS` | `choc[\s\-]?a[\s\-]?lot\|brownie[\s\-]?crunch` | Regex identifying the item |
| `MONITOR_STATE_PATH` | `tools/brownie_state.json` | Where last-seen state lives |
| `ALERT_WEBHOOK` | unset | ntfy.sh topic URL (or Slack / Discord webhook) |
| `NTFY_PRIORITY` | `max` | ntfy priority |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | unset | Pushover credentials |
| `PUSHOVER_PRIORITY` | `1` | `2` = re-alert until acknowledged |
| `TEST_PUSH` | unset | `true` sends a test alert and exits without checking stock |

## Run it locally

```sh
MONITOR_STATE_PATH=/tmp/brownie.json python3 tools/brownie_monitor.py
```

Without `GITHUB_TOKEN` it just prints the state transition instead of opening
an issue.

## Failure behavior

The script exits 0 for every expected outcome, including the site being down,
so a red run in the Actions tab always means the monitor itself is broken. If
the item disappears from the page for six consecutive checks it opens a
"can't find the item" issue rather than silently reporting sold out forever.
