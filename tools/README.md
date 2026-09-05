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

## Knobs

All optional, all environment variables:

| Variable | Default | What it does |
| --- | --- | --- |
| `MONITOR_URL` | Natick menu page | Page to check |
| `MONITOR_ITEM_PATTERNS` | `choc[\s\-]?a[\s\-]?lot\|brownie[\s\-]?crunch` | Regex identifying the item |
| `MONITOR_STATE_PATH` | `tools/brownie_state.json` | Where last-seen state lives |
| `ALERT_WEBHOOK` | unset | Extra push channel (ntfy.sh topic, Slack webhook). Set as a repo secret. |

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
