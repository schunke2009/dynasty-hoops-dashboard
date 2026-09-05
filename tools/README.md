# Brownie Choc-A-Lot stock monitor

Hourly watch on the Brownie Crunch Choc-A-Lot Cheesecake at The Cheesecake
Factory in Natick, MA. When the page stops reading as sold out, the workflow
opens a GitHub issue — which is what turns into an email and a phone push if
you have the GitHub mobile app installed.

- Workflow: [`.github/workflows/brownie-stock-monitor.yml`](../.github/workflows/brownie-stock-monitor.yml)
- Checker: [`brownie_monitor.py`](brownie_monitor.py) (stdlib only, no deps)
- State: `brownie_state.json`, committed by the workflow after each run

## Two things to know

1. **Scheduled runs only fire from the default branch.** GitHub ignores `cron`
   on feature branches. Merge to `main` or the hourly check never happens.
   `workflow_dispatch` ("Run workflow") works from any branch, so use that to
   test first.
2. **The menu page may not publish per-restaurant availability.** Marketing
   menus usually list the whole national menu regardless of what any one
   kitchen has left. The first run says so out loud if it sees the item as
   available while you know it is sold out — if that happens, repoint
   `MONITOR_URL` at a page that carries real stock state (the Cheesecake
   Factory ordering site, DoorDash, or Uber Eats for the Natick store).

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
