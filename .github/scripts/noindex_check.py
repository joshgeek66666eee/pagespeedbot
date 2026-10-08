#!/usr/bin/env python3
"""
noindex / nofollow monitor.

Fetches each page in the sitemap and flags any that carry a robots "noindex"
or "nofollow" directive — either in a <meta name="robots"|"googlebot"> tag or
in the X-Robots-Tag HTTP response header. Posts flagged pages to Slack.

Stdlib only — no auth, no API quota.

Env:
  SLACK_BOT_TOKEN        (required)  Slack bot token, xoxb-...
  SLACK_CHANNEL_ID       (optional)  channel id; overrides default
  SLACK_CHANNEL          (optional)  channel name or id, default #seo-team
  SITEMAP_URL            (optional)  sitemap index or URL sitemap
  SITEMAP_INCLUDE        (optional)  comma list; keep only child sitemaps matching
  SITEMAP_EXCLUDE        (optional)  comma list; drop matching (default image,video,webStory,videoGallery)
  EXTRA_URLS             (optional)  comma/newline extra URLs to always check
  MAX_URLS               (optional)  hard cap, default 3000
  WORKERS                (optional)  parallel fetch workers, default 10
  MAX_RPS                (optional)  max fetch requests/sec, default 10
  ALWAYS_REPORT          (optional)  "true" -> post a digest even if all clean
"""

import concurrent.futures
import datetime
import gzip
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import json

SLACK_ENDPOINT = "https://slack.com/api/chat.postMessage"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
NAME_RE = re.compile(r'name\s*=\s*["\']?\s*(robots|googlebot)\b', re.I)
CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.I)


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return val


# --------------------------------------------------------------------------- #
# Sitemap -> page URLs
# --------------------------------------------------------------------------- #
def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def is_sitemap_url(u):
    return urllib.parse.urlparse(u).path.rstrip("/").lower().endswith(".xml")


def fetch_bytes(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last


def parse_sitemap(data):
    root = ET.fromstring(data)
    if _localname(root.tag) not in ("sitemapindex", "urlset"):
        raise ValueError(f"not a sitemap (root <{_localname(root.tag)}>)")
    out = []
    for node in root:
        loc = None
        for child in node:
            if _localname(child.tag) == "loc" and child.text:
                loc = child.text.strip()
        if loc:
            out.append(loc)
    return out


def collect_from_sitemap(root_url, include, exclude, max_urls):
    try:
        entries = parse_sitemap(fetch_bytes(root_url))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not read sitemap {root_url}: {e}", file=sys.stderr)
        return []
    children = [u for u in entries if is_sitemap_url(u)]
    pages = [u for u in entries if not is_sitemap_url(u)]
    if not children:
        return pages[:max_urls]
    collected = []
    for child in children:
        name = child.rstrip("/").rsplit("/", 1)[-1]
        if include and not any(inc in child for inc in include):
            continue
        if exclude and any(exc in child for exc in exclude):
            print(f"  {name}: skipped (excluded)")
            continue
        try:
            cpages = [u for u in parse_sitemap(fetch_bytes(child)) if not is_sitemap_url(u)]
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: skipped ({e})")
            continue
        print(f"  {name}: {len(cpages)} pages")
        collected.extend(cpages)
        if len(collected) >= max_urls:
            break
    return collected[:max_urls]


def resolve_urls():
    max_urls = int(env("MAX_URLS", "3000"))
    extra = [u.strip() for u in env("EXTRA_URLS", "").replace("\n", ",").split(",") if u.strip()]
    sitemap_url = env("SITEMAP_URL")
    body = []
    if sitemap_url:
        include = [s.strip() for s in env("SITEMAP_INCLUDE", "").split(",") if s.strip()]
        exclude = [s.strip() for s in env(
            "SITEMAP_EXCLUDE", "image,video,webStory,videoGallery").split(",") if s.strip()]
        print(f"Building URL list from sitemap {sitemap_url}")
        body = collect_from_sitemap(sitemap_url, include, exclude, max_urls)
    seen, out = set(), []
    for u in extra + body:
        if is_sitemap_url(u):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= max_urls:
            break
    return out


# --------------------------------------------------------------------------- #
# robots directive detection
# --------------------------------------------------------------------------- #
def directives_from_meta(html):
    """Return set of directives from <meta name=robots|googlebot content=...>."""
    found = set()
    for tag in META_RE.findall(html):
        if not NAME_RE.search(tag):
            continue
        m = CONTENT_RE.search(tag)
        if not m:
            continue
        for part in m.group(1).lower().split(","):
            found.add(part.strip())
    return found


def directives_from_header(header_val):
    found = set()
    if header_val:
        # X-Robots-Tag: [bot:] directive, directive
        val = header_val.split(":", 1)[-1] if ":" in header_val else header_val
        for part in val.lower().split(","):
            found.add(part.strip())
    return found


def check_url(url, tries=3):
    """Fetch a page; return dict with any noindex/nofollow directives + source."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                status = resp.status
                header_val = resp.headers.get("X-Robots-Tag")
                html = resp.read().decode("utf-8", "replace")
            meta = directives_from_meta(html)
            hdr = directives_from_header(header_val)
            flags = {}
            for d in ("noindex", "nofollow", "none"):
                where = []
                if d in meta:
                    where.append("meta")
                if d in hdr:
                    where.append("X-Robots-Tag")
                if where:
                    flags[d] = "+".join(where)
            return {"url": url, "status": status, "flags": flags}
        except urllib.error.HTTPError as e:
            # a 4xx/5xx is a fetch problem, not a robots signal
            return {"url": url, "error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"url": url, "error": last}
    return {"url": url, "error": last or "unknown error"}


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
SECTION_LIMIT = 2900
MAX_SHOWN = 40


def short(url):
    p = urllib.parse.urlparse(url)
    return p.path or "/"


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def pack(lines):
    blocks, buf, ln = [], [], 0
    for line in lines:
        add = len(line) + 1
        if buf and ln + add > SECTION_LIMIT:
            blocks.append(_section("\n".join(buf)))
            buf, ln = [], 0
        buf.append(line)
        ln += add
    if buf:
        blocks.append(_section("\n".join(buf)))
    return blocks


def capped(lines, noun):
    blocks = pack(lines[:MAX_SHOWN])
    if len(lines) > MAX_SHOWN:
        blocks.append(_section(f"_…and {len(lines) - MAX_SHOWN} more {noun} — see the run log._"))
    return blocks


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def save_state(path, flagged):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "flagged": flagged,  # {url: "noindex (meta), nofollow (meta)"}
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def build_blocks(new, recovered, current, errors, total, always_report, first_run):
    n = len(new)
    if n:
        header = f":no_entry_sign: Robots — {n} NEW page(s) with noindex/nofollow"
    elif recovered:
        header = f":white_check_mark: Robots — tag removed from {len(recovered)} page(s)"
    else:
        header = f":white_check_mark: Robots — no new noindex/nofollow ({total} checked)"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]

    if new:
        blocks += capped([f"• <{u}|{short(u)}> — *{current.get(u, '')}*" for u in new], "pages")
    if recovered:
        blocks.append(_section(":large_green_circle: *Tag removed (now indexable/followable):*"))
        blocks += capped([f"• <{u}|{short(u)}>" for u in recovered], "pages")
    if always_report and not new and not recovered:
        blocks.append(_section(f"No change. Currently flagged in total: {len(current)}."))
    if errors:
        blocks.append(_section(":warning: *Could not fetch:*"))
        blocks += capped([f"• <{e['url']}|{short(e['url'])}> — `{e['error']}`" for e in errors], "errors")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Source: direct page fetch (meta robots + X-Robots-Tag) · alerts on change only"}]})
    return header, blocks


def post_to_slack(token, channel, header, blocks):
    payload = json.dumps({"channel": channel, "text": header, "blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_ENDPOINT, data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        print(f"ERROR: Slack API returned not-ok: {body.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(f"Posted to Slack channel {channel}.")


class RateLimiter:
    def __init__(self, per_second):
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self._interval


def main():
    slack_token = env("SLACK_BOT_TOKEN", required=True)
    slack_channel = env("SLACK_CHANNEL_ID") or env("SLACK_CHANNEL", "#seo-team")
    always_report = env("ALWAYS_REPORT", "false").lower() == "true"
    state_file = env("STATE_FILE", ".github/state/noindex-state.json")

    prev = load_state(state_file)
    first_run = prev is None
    prev_flagged = set((prev or {}).get("flagged", {}))

    urls = resolve_urls()
    if not urls:
        print("No URLs resolved", file=sys.stderr)
        sys.exit(1)

    workers = int(env("WORKERS", "10"))
    limiter = RateLimiter(float(env("MAX_RPS", "10")))
    print(f"Checking {len(urls)} page(s) for noindex/nofollow ({workers} workers)")

    def worker(url):
        limiter.wait()
        return check_url(url)

    current, errors, checked = {}, [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(worker, urls):
            checked += 1
            if "error" in r:
                errors.append(r)
                print(f"  ERR   {r['url']} -> {r['error']}")
            elif r["flags"]:
                current[r["url"]] = ", ".join(f"{d} ({src})" for d, src in r["flags"].items())
                print(f"  FLAG  {r['url']} -> {current[r['url']]}")
            else:
                print(f"  ok    {r['url']}")

    new = sorted(set(current) - prev_flagged)
    recovered = sorted(prev_flagged - set(current))
    print(f"Flagged now: {len(current)} | new: {len(new)} | recovered: {len(recovered)}")

    if first_run:
        print(f"First run — baseline saved ({len(current)} flagged), no alert.")
        if not always_report:
            save_state(state_file, current)
            return

    if new or recovered or always_report:
        header, blocks = build_blocks(new, recovered, current, errors, checked, always_report, first_run)
        post_to_slack(slack_token, slack_channel, header, blocks)  # exits(1) on Slack failure
    else:
        print("No change; nothing to post.")

    save_state(state_file, current)  # reached only if Slack didn't fail -> no lost alerts


if __name__ == "__main__":
    main()
