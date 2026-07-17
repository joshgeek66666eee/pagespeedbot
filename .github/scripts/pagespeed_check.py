#!/usr/bin/env python3
"""
Biweekly PageSpeed monitor.

For each URL in .github/pagespeed-urls.txt, query the PageSpeed Insights API
(mobile) and read the Lighthouse Performance score. If any page scores below
THRESHOLD, post an alert to Slack via chat.postMessage.

Stdlib only — no pip install needed.

Env:
  PSI_API_KEY       (required)  Google PageSpeed Insights API key
  SLACK_BOT_TOKEN   (required)  Slack bot token, xoxb-...
  SLACK_CHANNEL_ID  (optional)  Target channel id, e.g. C0XXXXXXX; overrides default
  SLACK_CHANNEL     (optional)  Channel name or id, default #seo-team
  THRESHOLD         (optional)  Performance score cutoff, default 80
  STRATEGY          (optional)  mobile | desktop, default mobile
  ALWAYS_REPORT     (optional)  "true" -> always post a digest, even if all pass
  URLS_FILE         (optional)  path to url list, default .github/pagespeed-urls.txt
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SLACK_ENDPOINT = "https://slack.com/api/chat.postMessage"


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return val


def load_urls(path):
    urls = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def http_get_json(url, tries=4):
    """GET with exponential backoff on 429/5xx."""
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stripo-pagespeed-monitor"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"__error__": f"{last_err}: {e.reason}"}
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"__error__": last_err}
    return {"__error__": last_err or "unknown error"}


def check_url(url, api_key, strategy):
    qs = urllib.parse.urlencode(
        {"url": url, "strategy": strategy, "category": "performance", "key": api_key}
    )
    data = http_get_json(f"{PSI_ENDPOINT}?{qs}")
    if "__error__" in data:
        return {"url": url, "error": data["__error__"]}
    try:
        lr = data["lighthouseResult"]
        score = round(lr["categories"]["performance"]["score"] * 100)
        audits = lr.get("audits", {})
        lcp = audits.get("largest-contentful-paint", {}).get("displayValue", "?")
        tbt = audits.get("total-blocking-time", {}).get("displayValue", "?")
        field = data.get("loadingExperience", {}).get("overall_category", "N/A")
        return {"url": url, "score": score, "lcp": lcp, "tbt": tbt, "field": field}
    except (KeyError, TypeError) as e:
        return {"url": url, "error": f"parse error: {e}"}


def short(url):
    p = urllib.parse.urlparse(url)
    path = p.path or "/"
    return path if path != "/" else "/ (homepage)"


def build_blocks(failures, errors, results, threshold, strategy, always_report):
    n_fail = len(failures)
    if n_fail:
        header = f":rotating_light: PageSpeed biweekly — {n_fail} page(s) below {threshold} ({strategy})"
    else:
        header = f":white_check_mark: PageSpeed biweekly — all {len(results)} pages ≥ {threshold} ({strategy})"

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]

    if failures:
        lines = []
        for r in sorted(failures, key=lambda x: x["score"]):
            lines.append(
                f"• *{r['score']}* — <{r['url']}|{short(r['url'])}>  "
                f"· LCP {r['lcp']} · TBT {r['tbt']} · field {r['field']}"
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if always_report and not failures and results:
        ok = [r for r in results if "score" in r]
        avg = round(sum(r["score"] for r in ok) / len(ok)) if ok else 0
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"All monitored pages passed. Average score: *{avg}*."}}
        )

    if errors:
        elines = [f"• <{e['url']}|{short(e['url'])}> — `{e['error']}`" for e in errors]
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ":warning: *Could not measure:*\n" + "\n".join(elines)}})

    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "Source: PageSpeed Insights API · edit list in `.github/pagespeed-urls.txt`"}]}
    )
    return header, blocks


def post_to_slack(token, channel, header, blocks):
    payload = json.dumps({"channel": channel, "text": header, "blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_ENDPOINT,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        print(f"ERROR: Slack API returned not-ok: {body.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(f"Posted to Slack channel {channel}.")


def main():
    api_key = env("PSI_API_KEY", required=True)
    slack_token = env("SLACK_BOT_TOKEN", required=True)
    # Alerts go to #seo-team by default; a SLACK_CHANNEL_ID secret
    # (channel id, e.g. C0XXXXXXX) overrides it when set.
    slack_channel = env("SLACK_CHANNEL_ID") or env("SLACK_CHANNEL", "#seo-team")
    threshold = int(env("THRESHOLD", "80"))
    strategy = env("STRATEGY", "mobile")
    always_report = env("ALWAYS_REPORT", "false").lower() == "true"
    urls_file = env("URLS_FILE", ".github/pagespeed-urls.txt")

    urls = load_urls(urls_file)
    if not urls:
        print(f"No URLs found in {urls_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Checking {len(urls)} URL(s), strategy={strategy}, threshold={threshold}")
    results, failures, errors = [], [], []
    for url in urls:
        r = check_url(url, api_key, strategy)
        results.append(r)
        if "error" in r:
            errors.append(r)
            print(f"  ERR  {url} -> {r['error']}")
        else:
            flag = "FAIL" if r["score"] < threshold else "ok  "
            if r["score"] < threshold:
                failures.append(r)
            print(f"  {flag} {r['score']:>3}  {url}")
        time.sleep(1)  # be gentle with the API

    should_post = bool(failures) or bool(errors) or always_report
    if should_post:
        header, blocks = build_blocks(failures, errors, results, threshold, strategy, always_report)
        post_to_slack(slack_token, slack_channel, header, blocks)
    else:
        print("All pages passed threshold; nothing to post (ALWAYS_REPORT is off).")


if __name__ == "__main__":
    main()
