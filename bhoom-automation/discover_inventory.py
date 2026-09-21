#!/usr/bin/env python3
"""
Public-index discovery for BhoomTV.

This is an API/index-first discovery method. It does not solve CAPTCHAs,
bypass Cloudflare, rotate identities, or defeat access controls.

Sources tried:
  1. Public Tamil category pagination
  2. WordPress REST API public post types
  3. Public WordPress sitemap indexes

The resulting inventory is merged into output/bhoom-tamil.json so the normal
validator can test the discovered live pages/previously known streams.
"""
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

BASE = "https://bhoomtv.org"
OUT = Path(os.environ.get("OUTPUT_DIR", "output"))
INVENTORY = OUT / "bhoom-tamil.json"
UA = "Mozilla/5.0 (compatible; TamilTV-public-index/1.0)"
TIMEOUT = int(os.environ.get("DISCOVERY_TIMEOUT_SECONDS", "12"))
RETRIES = max(1, int(os.environ.get("DISCOVERY_RETRIES", "2")))
CATEGORY_LIMIT = max(0, int(os.environ.get("CATEGORY_PAGE_LIMIT", "0") or "0"))
SECTION = os.environ.get("CATEGORY_SECTION", "all").strip().lower()

SEEDS = []
if SECTION in ("all", "tamil"):
    SEEDS.append((f"{BASE}/channel/tamil/", {"tamil"}))
if SECTION in ("all", "local"):
    SEEDS.append((f"{BASE}/channel/tamil-local-tv/", {"tamil", "local"}))

LIVE_RE = re.compile(r"https?://(?:www\.)?bhoomtv\.org/live/[a-z0-9-]+/?", re.I)
MANIFEST_RE = re.compile(r"https?://[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?", re.I)
OPTION_RE = re.compile(
    r'<li[^>]+class=["\'][^"\']*dooplay_player_option[^"\']*["\'][^>]*'
    r'[^>]*data-post=["\']([^"\']+)["\'][^>]*'
    r'[^>]*data-type=["\']([^"\']+)["\'][^>]*'
    r'[^>]*data-nume=["\']([^"\']+)["\']',
    re.I,
)
TAMIL_HINTS = ("tamil", "tamil local", "tamil-local", "local tamil")

def fetch(url, accept="text/html,*/*"):
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urlopen(req, timeout=TIMEOUT) as r:
                return getattr(r, "status", 200), r.read().decode("utf-8", "replace"), r.geturl()
        except HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (403, 429):
                return e.code, "", url
        except Exception as e:
            last = str(e)
        if attempt < RETRIES:
            time.sleep(min(attempt, 3))
    return 0, "", url

def is_live(url):
    try:
        p = urlparse(url)
        return p.netloc.lower() == urlparse(BASE).netloc.lower() and p.path.lower().startswith("/live/")
    except Exception:
        return False

def clean_live(url):
    if not is_live(url):
        return ""
    p = urlparse(url)
    return f"{BASE}{p.path.rstrip('/')}/"

def discover_category_pages():
    found = set()
    channels = set()
    for seed, _ in SEEDS:
        base = seed.rstrip("/")
        page = 1
        last_page = None
        empty = 0
        print(f"[DISCOVERY] SECTION {base}", flush=True)
        while CATEGORY_LIMIT == 0 or page <= CATEGORY_LIMIT:
            url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
            status, body, _ = fetch(url)
            print(f"[DISCOVERY] PAGE {page} HTTP={status}", flush=True)
            if status in (403, 429):
                print(f"[DISCOVERY] PAGE {page} BLOCKED -> stop this section (no bypass)", flush=True)
                break
            if status == 404:
                break
            links = {clean_live(x) for x in LIVE_RE.findall(body)}
            links.discard("")
            for link in links:
                channels.add(link)
            if links:
                found.add(url)
                empty = 0
            else:
                empty += 1
            nums = [int(x) for x in re.findall(rf"/page/(\d+)/?", body, re.I)]
            m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", re.sub(r"<[^>]+>", " ", body), re.I)
            if m:
                last_page = max(last_page or 0, int(m.group(2)))
            if nums:
                last_page = max(last_page or 0, max(nums))
            print(f"[DISCOVERY] PAGE {page} CHANNELS={len(links)} TOTAL={len(channels)}" + (f" LAST={last_page}" if last_page else ""), flush=True)
            if last_page and page >= last_page:
                break
            if not last_page and empty >= 2:
                break
            page += 1
    return found, channels

def rest_types():
    status, body, _ = fetch(f"{BASE}/wp-json/wp/v2/types", "application/json")
    if status != 200:
        print(f"[REST] types HTTP={status}", flush=True)
        return ["tv", "posts"]
    try:
        data = json.loads(body)
    except Exception:
        return ["tv", "posts"]
    candidates = []
    for slug, meta in data.items():
        text = f"{slug} {meta.get('name','')} {meta.get('rest_base','')}".lower()
        if any(x in text for x in ("tv", "live", "channel")):
            candidates.append(meta.get("rest_base") or slug)
    # Keep known DooPlay names as a fallback even if the discovery metadata is odd.
    for x in ("tv", "posts"):
        if x not in candidates:
            candidates.append(x)
    return list(dict.fromkeys(candidates))

def post_terms(item):
    vals = []
    emb = item.get("_embedded") or {}
    for group in emb.get("wp:term", []) or []:
        for term in group or []:
            vals.append(str(term.get("name") or term.get("slug") or ""))
    return " ".join(vals).lower()

def post_text(item):
    pieces = [
        str(item.get("slug") or ""),
        str(item.get("title", {}).get("rendered") or ""),
        str(item.get("content", {}).get("rendered") or ""),
        post_terms(item),
    ]
    return html.unescape(" ".join(pieces)).lower()

def extract_options(item):
    raw = json.dumps(item, ensure_ascii=False)
    content = str(item.get("content", {}).get("rendered") or "")
    source = content + "\n" + raw
    return list(dict.fromkeys(OPTION_RE.findall(source)))

def extract_manifests(text):
    return list(dict.fromkeys(MANIFEST_RE.findall(html.unescape(text or ""))))

def direct_sources_for_post(item):
    sources = extract_manifests(json.dumps(item, ensure_ascii=False))
    for post, typ, nume in extract_options(item):
        api = f"{BASE}/wp-json/dooplayer/v2/{post}/{typ}/{nume}"
        status, body, _ = fetch(api, "application/json,text/plain,*/*")
        print(f"[REST] PLAYER post={post} type={typ} nume={nume} HTTP={status}", flush=True)
        if status != 200:
            continue
        sources.extend(extract_manifests(body))
        try:
            data = json.loads(body)
            embed = data.get("embed_url") or data.get("url") or ""
            sources.extend(extract_manifests(embed))
            for iframe in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', embed, re.I):
                st, iframe_body, _ = fetch(urljoin(BASE, iframe))
                print(f"[REST] EMBED HTTP={st} URL={iframe.split('?')[0]}", flush=True)
                if st == 200:
                    sources.extend(extract_manifests(iframe_body))
        except Exception:
            pass
    return list(dict.fromkeys(sources))

def discover_rest():
    channels = {}
    types = rest_types()
    print(f"[REST] POST TYPES {types}", flush=True)
    for rest_type in types:
        page = 1
        while page <= 100:
            url = f"{BASE}/wp-json/wp/v2/{rest_type}?per_page=100&page={page}&_embed=1"
            status, body, _ = fetch(url, "application/json")
            print(f"[REST] {rest_type} PAGE={page} HTTP={status}", flush=True)
            if status in (400, 404):
                break
            if status in (403, 429):
                print(f"[REST] {rest_type} BLOCKED -> stop type (no bypass)", flush=True)
                break
            if status != 200:
                break
            try:
                items = json.loads(body)
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break
            for item in items:
                link = clean_live(str(item.get("link") or ""))
                if not link:
                    continue
                text = post_text(item)
                # REST records with explicit taxonomy/category are preferred.
                # If taxonomy is unavailable, content/slug still must contain a
                # Tamil hint before being admitted to this Tamil-only inventory.
                if not any(h in text for h in TAMIL_HINTS):
                    continue
                streams = direct_sources_for_post(item)
                channels[link] = {
                    "id": urlparse(link).path.rstrip("/").split("/")[-1],
                    "name": html.unescape(re.sub("<[^>]+>", "", str(item.get("title", {}).get("rendered") or ""))).strip()
                          or link.rstrip("/").split("/")[-1].replace("-", " ").title(),
                    "pageUrl": link,
                    "logo": "",
                    "streams": [{"url": u, "type": "hls" if ".m3u8" in u.lower() else "dash", "headers": {}} for u in streams],
                    "discoveryMode": "wordpress-rest",
                }
            print(f"[REST] {rest_type} PAGE={page} CANDIDATES={len(channels)}", flush=True)
            if len(items) < 100:
                break
            page += 1
    return channels

def merge_inventory(category_channels, rest_channels):
    existing = {}
    if INVENTORY.exists():
        try:
            data = json.loads(INVENTORY.read_text(encoding="utf-8"))
            for ch in data.get("channels", []) or []:
                url = clean_live(str(ch.get("pageUrl") or ""))
                if url:
                    existing[url] = ch
        except Exception:
            pass

    before = len(existing)
    for url in category_channels:
        existing.setdefault(url, {
            "id": url.rstrip("/").split("/")[-1],
            "name": url.rstrip("/").split("/")[-1].replace("-", " ").title(),
            "pageUrl": url,
            "logo": "",
            "streams": [],
            "discoveryMode": "category-pagination",
        })
    for url, ch in rest_channels.items():
        old = existing.get(url, {})
        merged = dict(old)
        for key in ("id", "name", "pageUrl", "logo"):
            if ch.get(key):
                merged[key] = ch[key]
        if ch.get("streams"):
            old_streams = {str(x.get("url")): x for x in old.get("streams", []) if x.get("url")}
            for s in ch["streams"]:
                old_streams.setdefault(s["url"], s)
            merged["streams"] = list(old_streams.values())
        else:
            merged.setdefault("streams", [])
        merged["discoveryMode"] = "wordpress-rest" if ch.get("streams") else merged.get("discoveryMode", "wordpress-rest")
        existing[url] = merged

    # The normal scraper only uses previous inventory entries that contain a
    # stream. Give newly discovered pages a harmless sentinel so blocked pages
    # are still added to its scan list; the sentinel is never published.
    for url, ch in existing.items():
        if not ch.get("streams"):
            ch["streams"] = [{
                "url": "http://127.0.0.1:9/bhoom-discovery-sentinel.m3u8",
                "type": "hls",
                "headers": {},
                "discoveryOnly": True,
            }]

    channels = sorted(existing.values(), key=lambda x: str(x.get("name") or x.get("pageUrl") or "").casefold())
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "discoveryMethod": "category-pagination + wordpress-rest",
        "uniqueChannels": len(channels),
        "channels": channels,
    }
    INVENTORY.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DISCOVERY SUMMARY] PREVIOUS={before} CATEGORY_NEW={len(category_channels)} REST_NEW={len(rest_channels)} TOTAL_SCAN_INVENTORY={len(channels)}", flush=True)

def main():
    _, category_channels = discover_category_pages()
    rest_channels = discover_rest()
    merge_inventory(category_channels, rest_channels)

if __name__ == "__main__":
    main()
