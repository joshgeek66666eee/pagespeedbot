#!/usr/bin/env python3
"""
Google Search Console indexing + rich-results monitor.

For each URL in the list, call the GSC URL Inspection API and read:
  * index status  — is the page cleanly on Google?
  * rich results  — is the page's structured data valid (no ERROR-level issues)?
If a URL is not cleanly indexed (verdict != PASS) or has a structured-data
error (or the inspection itself fails), post an alert to Slack.

Note: Mobile Usability is intentionally NOT checked — Google removed that
report and the corresponding API field on 2023-12-01. Mobile page quality is
covered by the separate PageSpeed monitor instead.

Auth: a Google service account (JSON key in GSC_SERVICE_ACCOUNT_JSON) that has
been added as a Full user / Owner of the Search Console property.

Deps: google-auth  (pip install google-auth)

Env:
  GSC_SERVICE_ACCOUNT_JSON  (required)  full service-account JSON key, as a string
  GSC_SITE_URL              (required)  property, e.g. "sc-domain:stripo.email"
                                        or "https://stripo.email/"
  SLACK_BOT_TOKEN           (required)  Slack bot token, xoxb-...
  SLACK_CHANNEL_ID          (optional)  channel id, e.g. C0XXXXXXX; overrides default
  SLACK_CHANNEL             (optional)  channel name or id, default #seo-team
  GSC_URLS_FILE             (optional)  path to url list, default .github/pagespeed-urls.txt
  ALWAYS_REPORT             (optional)  "true" -> post a digest even if all clean
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import google.auth.transport.requests
from google.oauth2 import service_account

INSPECT_ENDPOINT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
SLACK_ENDPOINT = "https://slack.com/api/chat.postMessage"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


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


def get_access_token(sa_json):
    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: GSC_SERVICE_ACCOUNT_JSON is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def inspect_url(url, site_url, token, tries=4):
    payload = json.dumps(
        {"inspectionUrl": url, "siteUrl": site_url, "languageCode": "en-US"}
    ).encode("utf-8")
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                INSPECT_ENDPOINT,
                data=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last_err = f"HTTP {e.code}: {body[:200]}"
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"__error__": last_err}
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"__error__": last_err}
    return {"__error__": last_err or "unknown error"}


def evaluate(url, data):
    if "__error__" in data:
        return {"url": url, "error": data["__error__"]}
    result = data.get("inspectionResult", {})
    idx = result.get("indexStatusResult")
    if not idx:
        return {"url": url, "error": "unexpected API response (no indexStatusResult)"}
    g_canon = idx.get("googleCanonical", "")
    u_canon = idx.get("userCanonical", "")

    # --- Rich results (structured data) ---
    # richResultsResult is absent when Google detected no structured data.
    # We flag only ERROR-severity issues; WARNINGs are surfaced as a note.
    rich = result.get("richResultsResult") or {}
    rich_types, rich_errors, rich_warnings = [], [], []
    for det in rich.get("detectedItems", []):
        rtype = det.get("richResultType", "?")
        rich_types.append(rtype)
        for item in det.get("items", []):
            for issue in item.get("issues", []):
                msg = f"{rtype}: {issue.get('issueMessage', '?')}"
                if issue.get("severity") == "ERROR":
                    rich_errors.append(msg)
                elif issue.get("severity") == "WARNING":
                    rich_warnings.append(msg)

    ok_index = idx.get("verdict") == "PASS"
    ok_rich = not rich_errors
    return {
        "url": url,
        "verdict": idx.get("verdict", "VERDICT_UNSPECIFIED"),
        "coverage": idx.get("coverageState", "?"),
        "fetch": idx.get("pageFetchState", "?"),
        "robots": idx.get("robotsTxtState", "?"),
        "last_crawl": idx.get("lastCrawlTime", "never"),
        "canon_mismatch": bool(g_canon and u_canon and g_canon != u_canon),
        "google_canonical": g_canon,
        "rich_verdict": rich.get("verdict", "N/A"),
        "rich_types": rich_types,
        "rich_errors": rich_errors,
        "rich_warnings": rich_warnings,
        "ok_index": ok_index,
        "ok_rich": ok_rich,
        "ok": ok_index and ok_rich,
    }


def short(url):
    p = urllib.parse.urlparse(url)
    path = p.path or "/"
    return path if path != "/" else "/ (homepage)"


def problem_lines(r):
    """Render one problem page as one or more Slack mrkdwn lines."""
    lines = [f"• <{r['url']}|{short(r['url'])}>"]
    if not r["ok_index"]:
        extra = " · :arrows_counterclockwise: canonical mismatch" if r["canon_mismatch"] else ""
        lines.append(
            f"    ↳ *index:* {r['coverage']} · verdict `{r['verdict']}` "
            f"· fetch `{r['fetch']}` · last crawl {r['last_crawl']}{extra}"
        )
    if not r["ok_rich"]:
        for err in r["rich_errors"]:
            lines.append(f"    ↳ *rich result error:* `{err}`")
    return "\n".join(lines)


def build_blocks(problems, errors, results, always_report):
    n = len(problems)
    if n:
        header = f":mag: GSC — {n} page(s) with indexing / rich-result issues"
    else:
        header = f":white_check_mark: GSC — all {len(results)} pages indexed, structured data clean"

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]

    if problems:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "\n".join(problem_lines(r) for r in problems)}})

    if always_report and not problems and results:
        rich_pages = sum(1 for r in results if r.get("rich_types"))
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"All {len(results)} pages are on Google (verdict PASS); "
                     f"{rich_pages} carry valid structured data."}}
        )

    # Non-blocking structured-data warnings (page still counts as OK).
    warned = [r for r in results if r.get("rich_warnings") and r.get("ok")]
    if warned:
        wlines = []
        for r in warned:
            for w in r["rich_warnings"]:
                wlines.append(f"• <{r['url']}|{short(r['url'])}> — `{w}`")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ":large_yellow_circle: *Rich-result warnings (non-blocking):*\n"
                               + "\n".join(wlines)}})

    if errors:
        elines = [f"• <{e['url']}|{short(e['url'])}> — `{e['error']}`" for e in errors]
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ":warning: *Could not inspect:*\n" + "\n".join(elines)}})

    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "Source: GSC URL Inspection API (index status + rich results) · "
                 "edit list in `.github/pagespeed-urls.txt`"}]}
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
    sa_json = env("GSC_SERVICE_ACCOUNT_JSON", required=True)
    site_url = env("GSC_SITE_URL", required=True)
    slack_token = env("SLACK_BOT_TOKEN", required=True)
    # Alerts go to #seo-team by default; a SLACK_CHANNEL_ID secret overrides it.
    slack_channel = env("SLACK_CHANNEL_ID") or env("SLACK_CHANNEL", "#seo-team")
    urls_file = env("GSC_URLS_FILE", ".github/pagespeed-urls.txt")
    always_report = env("ALWAYS_REPORT", "false").lower() == "true"

    urls = load_urls(urls_file)
    if not urls:
        print(f"No URLs found in {urls_file}", file=sys.stderr)
        sys.exit(1)

    token = get_access_token(sa_json)
    print(f"Inspecting {len(urls)} URL(s) against {site_url}")

    results, problems, errors = [], [], []
    for url in urls:
        r = evaluate(url, inspect_url(url, site_url, token))
        results.append(r)
        if "error" in r:
            errors.append(r)
            print(f"  ERR   {url} -> {r['error']}")
        elif not r["ok"]:
            problems.append(r)
            tags = []
            if not r["ok_index"]:
                tags.append(f"index={r['verdict']}")
            if not r["ok_rich"]:
                tags.append(f"rich={len(r['rich_errors'])} err")
            print(f"  ISSUE {url} -> {', '.join(tags)}")
        else:
            note = f" (+{len(r['rich_warnings'])} rich warn)" if r["rich_warnings"] else ""
            print(f"  ok    {url}{note}")
        time.sleep(0.5)  # be gentle with the API (quota: 2000/day, 600/min)

    should_post = bool(problems) or bool(errors) or always_report
    if should_post:
        header, blocks = build_blocks(problems, errors, results, always_report)
        post_to_slack(slack_token, slack_channel, header, blocks)
    else:
        print("All pages clean (indexed + valid structured data); nothing to post.")


if __name__ == "__main__":
    main()
