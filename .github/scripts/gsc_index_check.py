#!/usr/bin/env python3
"""
Google Search Console indexing + rich-results monitor.

URL sources (in priority order):
  1. GSC_SITEMAP_URL   — a sitemap index or a URL sitemap. When it is an index,
     each child sitemap is treated as one page TYPE, and the newest
     GSC_SAMPLE_PER_SITEMAP URLs (by <lastmod>) are sampled from each. Total is
     capped at GSC_MAX_URLS to protect the URL Inspection quota (2000/day).
  2. GSC_URLS_FILE     — a plain text list (one URL per line), used if no sitemap.

For each collected URL it calls the GSC URL Inspection API and reads:
  * index status  — is the page cleanly on Google?
  * rich results  — is the page's structured data valid (no ERROR-level issues)?
If a URL is not cleanly indexed (verdict != PASS) or has a structured-data
error (or the inspection itself fails), it posts an alert to Slack.

Note: Mobile Usability is intentionally NOT checked — Google removed that
report and the corresponding API field on 2023-12-01.

Auth: a Google service account (JSON key in GSC_SERVICE_ACCOUNT_JSON) added as a
Full user / Owner of the Search Console property.

Deps: google-auth, requests

Env:
  GSC_SERVICE_ACCOUNT_JSON  (required)  full service-account JSON key, as a string
  GSC_SITE_URL              (required)  property, e.g. "sc-domain:stripo.email"
  SLACK_BOT_TOKEN           (required)  Slack bot token, xoxb-...
  SLACK_CHANNEL_ID          (optional)  channel id; overrides default
  SLACK_CHANNEL             (optional)  channel name or id, default #seo-team
  GSC_SITEMAP_URL           (optional)  sitemap index or URL sitemap to sample from
  GSC_SAMPLE_PER_SITEMAP    (optional)  newest-N URLs per child sitemap, default 5
  GSC_MAX_URLS              (optional)  hard cap on total URLs, default 200
  GSC_SITEMAP_INCLUDE       (optional)  comma list; keep only child sitemaps whose
                                        URL contains one of these substrings
  GSC_SITEMAP_EXCLUDE       (optional)  comma list; drop child sitemaps matching any
                                        (default: image,video,webStory,videoGallery)
  GSC_URLS_FILE             (optional)  fallback list, default .github/gsc-urls.txt
  ALWAYS_REPORT             (optional)  "true" -> post a digest even if all clean
"""

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

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


# --------------------------------------------------------------------------- #
# URL sources
# --------------------------------------------------------------------------- #
def load_urls(path):
    urls = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def fetch_bytes(url, tries=3):
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stripo-gsc-monitor"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if data[:2] == b"\x1f\x8b":  # gzip magic bytes
                data = gzip.decompress(data)
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last_err


def parse_sitemap(data):
    """Return ('index', [child_urls]) or ('urlset', [(loc, lastmod), ...])."""
    root = ET.fromstring(data)
    if _localname(root.tag) == "sitemapindex":
        children = []
        for sm in root:
            for child in sm:
                if _localname(child.tag) == "loc" and child.text:
                    children.append(child.text.strip())
        return "index", children
    urls = []
    for u in root:
        loc = lastmod = None
        for child in u:
            ln = _localname(child.tag)
            if ln == "loc" and child.text:
                loc = child.text.strip()
            elif ln == "lastmod" and child.text:
                lastmod = child.text.strip()
        if loc:
            urls.append((loc, lastmod or ""))
    return "urlset", urls


def newest(urls, n):
    """Pick the n newest URLs by lastmod (desc); missing lastmod sorts last."""
    ordered = sorted(urls, key=lambda x: x[1], reverse=True)
    return [loc for loc, _ in ordered[:n]]


def collect_from_sitemap(root_url, sample_per, include, exclude, max_urls):
    kind, payload = parse_sitemap(fetch_bytes(root_url))
    if kind == "urlset":
        picked = newest(payload, max_urls)
        print(f"  {root_url}: {len(payload)} urls -> sampled {len(picked)}")
        return picked

    collected = []
    for child in payload:
        name = child.rsplit("/", 1)[-1]
        if include and not any(inc in child for inc in include):
            continue
        if exclude and any(exc in child for exc in exclude):
            print(f"  {name}: skipped (excluded)")
            continue
        try:
            ckind, cpayload = parse_sitemap(fetch_bytes(child))
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: skipped ({e})")
            continue
        if ckind == "urlset":
            if sample_per and sample_per > 0:
                picked = newest(cpayload, sample_per)
            else:
                picked = [loc for loc, _ in cpayload]  # 0/absent => take ALL
            print(f"  {name}: {len(cpayload)} urls -> take {len(picked)}")
            collected.extend(picked)
        if len(collected) >= max_urls:
            print(f"  reached GSC_MAX_URLS={max_urls}, stopping")
            break
    return collected[:max_urls]


def resolve_urls():
    max_urls = int(env("GSC_MAX_URLS", "1800"))
    # Explicit URLs always checked (e.g. homepage, templates home). Comma or newline separated.
    extra = [u.strip() for u in env("GSC_EXTRA_URLS", "").replace("\n", ",").split(",") if u.strip()]

    sitemap_url = env("GSC_SITEMAP_URL")
    if sitemap_url:
        sample_per = int(env("GSC_SAMPLE_PER_SITEMAP", "0"))  # 0 => all URLs per type
        include = [s.strip() for s in env("GSC_SITEMAP_INCLUDE", "").split(",") if s.strip()]
        exclude = [
            s.strip()
            for s in env("GSC_SITEMAP_EXCLUDE", "image,video,webStory,videoGallery").split(",")
            if s.strip()
        ]
        per = "all" if sample_per <= 0 else f"newest {sample_per}"
        print(f"Building URL list from sitemap {sitemap_url} ({per}/type, cap {max_urls})")
        body = collect_from_sitemap(sitemap_url, sample_per, include, exclude, max_urls)
    else:
        body = load_urls(env("GSC_URLS_FILE", ".github/gsc-urls.txt"))

    # extra first, then sitemap body; de-dupe preserving order; hard cap
    seen, out = set(), []
    for u in extra + body:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= max_urls:
            print(f"  reached GSC_MAX_URLS={max_urls}, truncating")
            break
    return out


# --------------------------------------------------------------------------- #
# GSC inspection
# --------------------------------------------------------------------------- #
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
    return {
        "url": url,
        "verdict": idx.get("verdict", "VERDICT_UNSPECIFIED"),
        "coverage": idx.get("coverageState", "?"),
        "fetch": idx.get("pageFetchState", "?"),
        "robots": idx.get("robotsTxtState", "?"),
        "last_crawl": idx.get("lastCrawlTime", "never"),
        "canon_mismatch": bool(g_canon and u_canon and g_canon != u_canon),
        "rich_errors": rich_errors,
        "rich_warnings": rich_warnings,
        "rich_types": rich_types,
        "ok_index": ok_index,
        "ok_rich": not rich_errors,
        "ok": ok_index and not rich_errors,
    }


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def short(url):
    p = urllib.parse.urlparse(url)
    path = p.path or "/"
    return path if path != "/" else "/ (homepage)"


SECTION_LIMIT = 2900   # Slack section text hard limit is 3000 chars
MAX_SHOWN = 40         # cap entries per list so we stay under Slack's 50-block cap


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def pack(lines):
    """Pack mrkdwn line-strings into section blocks under Slack's char limit."""
    blocks, buf, buflen = [], [], 0
    for line in lines:
        add = len(line) + 1
        if buf and buflen + add > SECTION_LIMIT:
            blocks.append(_section("\n".join(buf)))
            buf, buflen = [], 0
        buf.append(line)
        buflen += add
    if buf:
        blocks.append(_section("\n".join(buf)))
    return blocks


def capped_sections(lines, noun):
    """Pack lines into blocks, showing at most MAX_SHOWN with a '…and N more' note."""
    blocks = pack(lines[:MAX_SHOWN])
    if len(lines) > MAX_SHOWN:
        blocks.append(_section(f"_…and {len(lines) - MAX_SHOWN} more {noun} — see the Actions run log._"))
    return blocks


def problem_lines(r):
    lines = [f"• <{r['url']}|{short(r['url'])}>"]
    if not r["ok_index"]:
        extra = " · :arrows_counterclockwise: canonical mismatch" if r["canon_mismatch"] else ""
        lines.append(
            f"    ↳ *index:* {r['coverage']} · verdict `{r['verdict']}` "
            f"· fetch `{r['fetch']}` · last crawl {r['last_crawl']}{extra}"
        )
    for err in r["rich_errors"]:
        lines.append(f"    ↳ *rich result error:* `{err}`")
    return "\n".join(lines)


def build_blocks(problems, errors, results, always_report):
    n = len(problems)
    if n:
        header = f":mag: GSC — {n} of {len(results)} sampled page(s) with issues"
    else:
        header = f":white_check_mark: GSC — all {len(results)} sampled pages indexed, structured data clean"

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]

    if problems:
        blocks += capped_sections([problem_lines(r) for r in problems], "page(s) with issues")

    if always_report and not problems and results:
        rich_pages = sum(1 for r in results if r.get("rich_types"))
        blocks.append(_section(
            f"All {len(results)} sampled pages are on Google (verdict PASS); "
            f"{rich_pages} carry valid structured data."))

    warned = [r for r in results if r.get("rich_warnings") and r.get("ok")]
    if warned:
        wlines = [f"• <{r['url']}|{short(r['url'])}> — `{w}`"
                  for r in warned for w in r["rich_warnings"]]
        blocks.append(_section(":large_yellow_circle: *Rich-result warnings (non-blocking):*"))
        blocks += capped_sections(wlines, "warnings")

    if errors:
        elines = [f"• <{e['url']}|{short(e['url'])}> — `{e['error']}`" for e in errors]
        blocks.append(_section(":warning: *Could not inspect:*"))
        blocks += capped_sections(elines, "errors")

    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "Source: GSC URL Inspection API (index status + rich results) · "
                 "newest pages sampled per type from sitemap_index.xml"}]}
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


# --------------------------------------------------------------------------- #
def main():
    sa_json = env("GSC_SERVICE_ACCOUNT_JSON", required=True)
    site_url = env("GSC_SITE_URL", required=True)
    slack_token = env("SLACK_BOT_TOKEN", required=True)
    slack_channel = env("SLACK_CHANNEL_ID") or env("SLACK_CHANNEL", "#seo-team")
    always_report = env("ALWAYS_REPORT", "false").lower() == "true"

    urls = resolve_urls()
    if not urls:
        print("No URLs resolved (empty sitemap/list)", file=sys.stderr)
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
        time.sleep(0.3)  # be gentle with the API (quota: 2000/day, 600/min)

    if problems or errors or always_report:
        header, blocks = build_blocks(problems, errors, results, always_report)
        post_to_slack(slack_token, slack_channel, header, blocks)
    else:
        print("All sampled pages clean; nothing to post.")


if __name__ == "__main__":
    main()
