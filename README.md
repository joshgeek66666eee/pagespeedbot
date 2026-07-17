# pagespeedbot

Biweekly mobile **PageSpeed** monitor for Stripo pages, with Slack alerts.
Moved out of the `stripo-wiki` repo — it doesn't belong in the shared
knowledge wiki, so it lives here on its own.

## What it does

Every other Thursday, for each URL in `pagespeed-urls.txt`, it queries the
Google PageSpeed Insights API (mobile) and reads the Lighthouse Performance
score. If any page scores **below the threshold (default 80)**, it posts an
alert to Slack. If everything passes, it stays quiet (unless you ask for a
digest).

## Layout

```
.github/
  workflows/pagespeed-monitor.yml   # cron (Thu) + biweekly gate + manual dispatch
  scripts/pagespeed_check.py        # PSI query + Slack chat.postMessage (stdlib only)
  pagespeed-urls.txt                # editable URL list (one per line, # comments ok)
```

## Setup

1. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `PSI_API_KEY` — Google PageSpeed Insights API key
   - `SLACK_BOT_TOKEN` — Slack bot token (`xoxb-…`), needs `chat:write`
   - `SLACK_CHANNEL_ID` — target channel id, e.g. `C0XXXXXXX`
2. Edit `.github/pagespeed-urls.txt` to the pages you want watched.
3. (Optional) tune `THRESHOLD` / `STRATEGY` in the workflow env.

## Schedule

Cron can't express "every 2 weeks", so the workflow fires **every Thursday**
06:00 UTC (~09:00 Europe/Kyiv) and a gate step runs the check only on **even
ISO weeks**. Scheduled Actions run only from the **default branch**, so this
must sit on `main` for the cron to fire.

## Run it manually

Actions tab → **PageSpeed biweekly monitor** → **Run workflow**. Manual runs
ignore the biweekly gate and always execute. Tick *"Post a digest even if all
pages pass"* to get a full report regardless of scores.
