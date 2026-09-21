from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://bhoomtv.org"
PROXY_BASE_URL = os.getenv("BHOOM_PROXY_BASE_URL", "").rstrip("/")
SECTIONS = {
    "tamil": f"{BASE_URL}/channel/tamil/",
    "local": f"{BASE_URL}/channel/tamil-local-tv/",
}
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
STATE_DIR = ROOT / "state"
DEBUG_DIR = ROOT / "debug"
OUTPUT_JSON = ROOT / "output/bhoom-tamil.json"
OUTPUT_M3U = ROOT / "output/bhoom-tamil.m3u"
OUTPUT_REPORT = ROOT / "output/bhoom-tamil-report.json"
INVENTORY_FILE = ROOT / "state/channel_inventory.json"

UA = os.getenv(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
)
CATEGORY_SECTION = os.getenv("CATEGORY_SECTION", "all").lower()
CATEGORY_PAGE_LIMIT = max(0, int(os.getenv("CATEGORY_PAGE_LIMIT", "0")))
MAX_CHANNELS = max(0, int(os.getenv("MAX_CHANNELS", "0")))
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")))
PAGE_TIMEOUT = max(5, int(os.getenv("PAGE_NAV_TIMEOUT_SECONDS", "25")))
PLAYER_CAPTURE_WAIT = max(1, int(os.getenv("PLAYER_CAPTURE_WAIT_SECONDS", "8")))
STABILITY_SECONDS = max(0, int(os.getenv("STABILITY_SECONDS", "3")))
VALIDATE_STREAMS = os.getenv("VALIDATE_STREAMS", "1") != "0"
SITEMAP_FALLBACK = os.getenv("SITEMAP_FALLBACK", "1") != "0"
SITEMAP_MAX_CHANNEL_PAGES = max(0, int(os.getenv("SITEMAP_MAX_CHANNEL_PAGES", "200")))
MAX_CANDIDATES = max(1, int(os.getenv("MAX_CAPTURE_CANDIDATES", "6")))
BHOOM_COLLECT_URL = os.getenv("BHOOM_COLLECT_URL", "").rstrip("/")
BHOOM_INVENTORY_URL = os.getenv("BHOOM_INVENTORY_URL", "").rstrip("/")
WORKER_MAX_PAGES = max(1, int(os.getenv("WORKER_MAX_PAGES", "200")))

CF_MARKERS = (
    "just a moment",
    "cf-chl-",
    "challenge-platform",
    "attention required",
    "cloudflare ray id",
    "verify you are human",
)

@dataclass
class Validation:
    url: str
    kind: str
    http_status: int | None
    content_type: str | None
    manifest_valid: bool
    segment_status: int | None
    segment_checked: bool
    stable: bool | None
    sequence_before: str | None
    sequence_after: str | None
    changed: bool | None
    usable: bool
    reason: str

def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def log(message: str) -> None:
    print(message, flush=True)

def canonical(url: str) -> str:
    full = urljoin(BASE_URL + "/", url)
    p = urlparse(full)
    path = p.path or "/"
    if path != "/" and not path.endswith("/") and "/live/" in path:
        path += "/"
    return p._replace(path=path, fragment="").geturl()

def slug(url: str) -> str:
    return urlparse(url).path.strip("/").split("/")[-1]

def redact(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}?<query-redacted>" if p.query else url

def normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()

def request_url(url: str) -> str:
    if not PROXY_BASE_URL:
        return url
    parsed = urlparse(url)
    if parsed.hostname not in {"bhoomtv.org", "www.bhoomtv.org"}:
        return url
    return f"{PROXY_BASE_URL}/proxy?url={quote(url, safe='')}"


def cloudflare(status: int, headers: dict[str, str], body: str) -> bool:
    lower = body[:50000].lower()
    if any(marker in lower for marker in CF_MARKERS):
        return True
    return status in (403, 429, 503) and "cloudflare" in headers.get("server", "").lower()

async def fetch(client: httpx.AsyncClient, url: str, attempts: int = 2):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(request_url(url))
            headers = {k.lower(): v for k, v in response.headers.items()}
            if cloudflare(response.status_code, headers, response.text):
                return {"status": response.status_code, "headers": headers, "text": response.text, "blocked": "CLOUDFLARE_CHALLENGE"}
            if response.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(attempt)
                continue
            return {"status": response.status_code, "headers": headers, "text": response.text}
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt < attempts:
                await asyncio.sleep(attempt)
    return {"status": 0, "headers": {}, "text": "", "error": last_error or "NETWORK_ERROR"}

def extract_channel_name(soup: BeautifulSoup, fallback: str) -> str:
    for selector in ("h1", ".entry-title", ".post-title"):
        node = soup.select_one(selector)
        if node:
            value = normalize_text(node.get_text(" ", strip=True))
            if value:
                return value
    if soup.title:
        value = normalize_text(soup.title.get_text(" ", strip=True))
        value = re.sub(r"\s*\|\s*BHOOM.*$", "", value, flags=re.I)
        if value:
            return value
    return fallback

def extract_logo(soup: BeautifulSoup) -> str | None:
    for selector, attr in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ("article img", "src"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attr):
            return canonical(node.get(attr))
    return None

def extract_category_channels(html: str, section: str):
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    rows = []
    for link in soup.find_all("a", href=True):
        href = canonical(link["href"])
        if "/live/" not in urlparse(href).path or href in seen:
            continue
        name = normalize_text(link.get_text(" ", strip=True))
        image = link.find("img")
        if not name and image:
            name = normalize_text(image.get("alt"))
        if not name:
            name = slug(href).replace("-", " ").title()
        seen.add(href)
        rows.append({
            "name": name,
            "slug": slug(href),
            "url": href,
            "logo": canonical(image.get("src")) if image and image.get("src") else None,
            "section": section,
        })
    return rows

def next_page_url(soup: BeautifulSoup, current: str, page_no: int) -> str | None:
    for link in soup.find_all("a", href=True):
        rel = [str(x).lower() for x in (link.get("rel") or [])]
        text = normalize_text(link.get_text(" ", strip=True)).lower()
        if "next" in rel or text in {"next", "next page", "›", "»"}:
            href = canonical(link["href"])
            if "/channel/" in href and href != canonical(current):
                return href
    current_base = canonical(current).rstrip("/")
    matches = []
    for link in soup.find_all("a", href=True):
        href = canonical(link["href"])
        match = re.search(r"/page/(\d+)/?$", href)
        if match and int(match.group(1)) > page_no:
            matches.append((int(match.group(1)), href))
    if matches:
        matches.sort()
        return matches[0][1]
    return f"{current_base}/page/{page_no + 1}/"

def extract_streams(html: str):
    normalized = html.replace("\\/", "/").replace("\u002F", "/").replace("\x2F", "/").replace("&amp;", "&")
    pattern = re.compile(r"https?://[^\s'\"<>\\]+(?:\.m3u8|\.mpd)(?:\?[^\s'\"<>\\]*)?", re.I)
    found = []
    seen = set()
    for match in pattern.finditer(normalized):
        url = match.group(0).replace("\\/", "/")
        kind = "HLS" if ".m3u8" in url.lower() else "DASH"
        url = canonical(url)
        if url not in seen:
            seen.add(url)
            found.append((url, kind))
    soup = BeautifulSoup(normalized, "html.parser")
    for node in soup.find_all(["video", "source"]):
        src = node.get("src")
        if not src:
            continue
        url = canonical(src)
        low = url.lower()
        if ".m3u8" not in low and ".mpd" not in low:
            continue
        kind = "HLS" if ".m3u8" in low else "DASH"
        if url not in seen:
            seen.add(url)
            found.append((url, kind))
    return found[:MAX_CANDIDATES]

def first_hls_variant(text: str) -> str | None:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:") and not lines[i + 1].startswith("#"):
            return lines[i + 1]
    return None

def first_hls_segment(text: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                return match.group(1)
        elif not line.startswith("#"):
            return line
    return None

def sequence(text: str) -> str | None:
    match = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    return match.group(1) if match else None

def html_like(content: bytes) -> bool:
    sample = content[:512].lstrip().lower()
    return sample.startswith(b"<html") or sample.startswith(b"<!doctype") or b"just a moment" in sample

async def validate_hls(client, url: str, referer: str | None) -> Validation:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return Validation(url, "HLS", None, None, False, None, False, None, None, None, None, False, f"REQUEST_ERROR:{exc}")
    ctype = response.headers.get("content-type")
    if cloudflare(response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.text):
        return Validation(url, "HLS", response.status_code, ctype, False, None, False, None, None, None, None, False, "CLOUDFLARE_CHALLENGE")
    body = response.text
    if response.status_code != 200 or not body.lstrip().startswith("#EXTM3U"):
        return Validation(url, "HLS", response.status_code, ctype, False, None, False, None, None, None, None, False, "INVALID_HLS_MANIFEST")

    manifest_url = url
    media = first_hls_variant(body)
    target = body
    seq_before = sequence(body)
    if media:
        manifest_url = urljoin(url, media)
        try:
            child = await client.get(manifest_url, headers=headers)
            target = child.text
            seq_before = sequence(target) or seq_before
            if child.status_code != 200 or not target.lstrip().startswith("#EXTM3U"):
                return Validation(url, "HLS", response.status_code, ctype, False, child.status_code, False, None, seq_before, None, None, False, "INVALID_MEDIA_PLAYLIST")
        except httpx.HTTPError as exc:
            return Validation(url, "HLS", response.status_code, ctype, False, None, False, None, seq_before, None, None, False, f"MEDIA_REQUEST_ERROR:{exc}")

    segment = first_hls_segment(target)
    seg_status = None
    seg_checked = False
    if segment:
        try:
            seg = await client.get(
                urljoin(manifest_url, segment),
                headers={**headers, "Range": "bytes=0-1023"},
            )
            seg_status = seg.status_code
            seg_checked = True
            if seg.status_code not in (200, 206) or not seg.content or html_like(seg.content):
                return Validation(url, "HLS", response.status_code, ctype, True, seg_status, True, None, seq_before, None, None, False, "SEGMENT_CHECK_FAILED")
        except httpx.HTTPError as exc:
            return Validation(url, "HLS", response.status_code, ctype, True, None, True, None, seq_before, None, None, False, f"SEGMENT_REQUEST_ERROR:{exc}")

    stable = None
    seq_after = seq_before
    changed = None
    if STABILITY_SECONDS:
        await asyncio.sleep(STABILITY_SECONDS)
        try:
            second = await client.get(manifest_url, headers=headers)
            second_text = second.text
            stable = second.status_code == 200 and second_text.lstrip().startswith("#EXTM3U")
            seq_after = sequence(second_text)
            if seq_before is not None and seq_after is not None:
                changed = seq_before != seq_after
        except httpx.HTTPError:
            stable = False

    return Validation(url, "HLS", response.status_code, ctype, True, seg_status, seg_checked, stable, seq_before, seq_after, changed, True, "VALID_HLS")

async def validate_dash(client, url: str, referer: str | None) -> Validation:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return Validation(url, "DASH", None, None, False, None, False, None, None, None, None, False, f"REQUEST_ERROR:{exc}")
    ctype = response.headers.get("content-type")
    if cloudflare(response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.text):
        return Validation(url, "DASH", response.status_code, ctype, False, None, False, None, None, None, None, False, "CLOUDFLARE_CHALLENGE")
    if response.status_code != 200 or "<MPD" not in response.text:
        return Validation(url, "DASH", response.status_code, ctype, False, None, False, None, None, None, None, False, "INVALID_MPD")
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        return Validation(url, "DASH", response.status_code, ctype, False, None, False, None, None, None, None, False, "MPD_XML_PARSE_FAILED")
    names = {node.tag.rsplit("}", 1)[-1] for node in root.iter()}
    playable = "Period" in names and (
        "Representation" in names or "BaseURL" in names or "SegmentTemplate" in names or "SegmentList" in names
    )
    return Validation(url, "DASH", response.status_code, ctype, playable, None, False, True, None, None, None, playable, "VALID_MPD" if playable else "MPD_NO_PLAYABLE_STRUCTURE")

def load_inventory():
    if not INVENTORY_FILE.exists():
        return {}
    try:
        raw = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = raw.get("channels", []) if isinstance(raw, dict) else []
    return {canonical(item["url"]): item for item in rows if isinstance(item, dict) and item.get("url")}

def merge_inventory(inventory, rows):
    for row in rows:
        key = canonical(row["url"])
        previous = inventory.get(key, {})
        merged = dict(previous)
        merged.update({
            "name": row.get("name") or previous.get("name") or slug(key).replace("-", " ").title(),
            "slug": row.get("slug") or previous.get("slug") or slug(key),
            "url": key,
            "logo": row.get("logo") or previous.get("logo"),
            "section": row.get("section") or previous.get("section") or "tamil",
            "last_discovered_at": now(),
        })
        inventory[key] = merged

def save_inventory(inventory):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now(),
        "channel_count": len(inventory),
        "channels": sorted(
            inventory.values(),
            key=lambda x: (str(x.get("section", "")), str(x.get("name", "")).lower()),
        ),
    }
    INVENTORY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def refresh_worker_inventory(client):
    if not BHOOM_COLLECT_URL or not BHOOM_INVENTORY_URL:
        return {}

    inventory = {}
    log(f"Worker collector: {BHOOM_COLLECT_URL}")

    for section in ("tamil", "local"):
        for page_no in range(1, WORKER_MAX_PAGES + 1):
            collect_url = f"{BHOOM_COLLECT_URL}/collect?section={section}&page={page_no}"
            try:
                response = await client.get(collect_url)
                log(
                    f"  [WORKER COLLECT] section={section} page={page_no} "
                    f"HTTP={response.status_code}"
                )
                if response.status_code != 200:
                    break

                data = response.json()
                if not data.get("ok"):
                    log(f"    [WORKER ERROR] {data}")
                    break

                log(
                    f"    channels_on_page={data.get('channels_on_page', 0)} "
                    f"inventory_count={data.get('inventory_count', 0)} "
                    f"complete={data.get('complete')}"
                )

                if data.get("complete"):
                    break

                await asyncio.sleep(1.1)
            except Exception as exc:
                log(f"    [WORKER COLLECT ERROR] {exc}")
                break

        try:
            response = await client.get(f"{BHOOM_INVENTORY_URL}/inventory?{section}")
            log(
                f"  [WORKER INVENTORY] section={section} "
                f"HTTP={response.status_code}"
            )
            if response.status_code == 200:
                data = response.json()
                rows = data.get("channels", [])
                if isinstance(rows, list):
                    inventory.update({
                        canonical(row["url"]): row
                        for row in rows
                        if isinstance(row, dict) and row.get("url")
                    })
                    log(f"    inventory returned {len(rows)} channels for {section}")
        except Exception as exc:
            log(f"    [WORKER INVENTORY ERROR] {exc}")

    if inventory:
        try:
            response = await client.get(f"{BHOOM_INVENTORY_URL}/inventory")
            if response.status_code == 200:
                data = response.json()
                rows = data.get("channels", [])
                for row in rows:
                    if isinstance(row, dict) and row.get("url"):
                        inventory[canonical(row["url"])] = row
                log(f"  [WORKER INVENTORY] combined={len(inventory)}")
        except Exception as exc:
            log(f"  [WORKER INVENTORY COMBINED ERROR] {exc}")

    return inventory

async def discover_section(client, section: str):
    url = SECTIONS[section]
    page_no = 1
    seen = set()
    rows = []
    blocked = False
    log(f"Discovering complete section: [{url}]")
    while url and url not in seen:
        if CATEGORY_PAGE_LIMIT and page_no > CATEGORY_PAGE_LIMIT:
            log(f"  PAGE LIMIT REACHED: {CATEGORY_PAGE_LIMIT}")
            break
        seen.add(url)
        result = await fetch(client, url, attempts=3)
        if result.get("blocked"):
            log(f"  page {page_no}: HTTP {result['status']}")
            log("  CLOUDFLARE BLOCKED: stopping this section without repeated challenge requests")
            blocked = True
            break
        log(f"  page {page_no}: HTTP {result['status']}")
        if result["status"] == 404:
            break
        if result["status"] != 200:
            log("  category request failed; stopping section")
            break
        page_rows = extract_category_channels(result["text"], section)
        log(f"  page {page_no}: {len(page_rows)} live channel links")
        rows.extend(page_rows)
        soup = BeautifulSoup(result["text"], "html.parser")
        next_url = next_page_url(soup, url, page_no)
        if not page_rows or not next_url or next_url in seen:
            break
        url = next_url
        page_no += 1
        await asyncio.sleep(0.5)
    return rows, blocked

async def discover_sitemap(client):
    queue = [f"{BASE_URL}/wp-sitemap.xml", f"{BASE_URL}/sitemap_index.xml", f"{BASE_URL}/sitemap.xml"]
    visited = set()
    live = []
    while queue and len(visited) < 25:
        current = canonical(queue.pop(0))
        if current in visited:
            continue
        visited.add(current)
        result = await fetch(client, current, attempts=1)
        if result.get("blocked") or result["status"] != 200:
            continue
        try:
            root = ET.fromstring(result["text"])
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "loc":
                continue
            value = normalize_text(node.text)
            if not value:
                continue
            value = canonical(value)
            if value.endswith(".xml") and value not in visited:
                queue.append(value)
            elif "/live/" in urlparse(value).path:
                live.append(value)
    result = []
    seen = set()
    for url in live:
        if url not in seen:
            seen.add(url)
            result.append(url)
    log(f"  SITEMAP LIVE URLS DISCOVERED: {len(result)}")
    return result

def classify_channel(html: str):
    text = normalize_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).lower()
    if "tamil local" in text:
        return "local"
    if re.search(r"\btamil\b", text):
        return "tamil"
    return None

async def sitemap_fallback(client, inventory):
    if not SITEMAP_FALLBACK or SITEMAP_MAX_CHANNEL_PAGES <= 0:
        return
    urls = await discover_sitemap(client)
    remaining = [u for u in urls if u not in inventory]
    if not remaining:
        return
    total = min(len(remaining), SITEMAP_MAX_CHANNEL_PAGES)
    log(f"  SITEMAP FALLBACK: inspecting {total} live pages")
    for index, url in enumerate(remaining[:total], start=1):
        result = await fetch(client, url, attempts=1)
        if result.get("blocked"):
            log("  SITEMAP FALLBACK: Cloudflare challenge encountered; stopping")
            break
        if result["status"] != 200:
            continue
        section = classify_channel(result["text"])
        if section not in SECTIONS:
            continue
        soup = BeautifulSoup(result["text"], "html.parser")
        merge_inventory(inventory, [{
            "name": extract_channel_name(soup, slug(url).replace("-", " ").title()),
            "slug": slug(url),
            "url": url,
            "logo": extract_logo(soup),
            "section": section,
        }])
        if index % 10 == 0:
            log(f"  SITEMAP FALLBACK PROGRESS: {index}/{total} pages inspected")

async def capture_with_browser(page, url: str):
    candidates = []
    seen = set()
    challenge = False

    def on_response(response):
        nonlocal challenge
        rurl = response.url
        status = response.status
        low = rurl.lower()
        try:
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:
            ctype = ""
        if status in (403, 429, 503) and ("challenge" in low or "cloudflare" in ctype):
            challenge = True
        is_stream = ".m3u8" in low or ".mpd" in low or "mpegurl" in ctype or "dash+xml" in ctype
        if not is_stream or rurl in seen:
            return
        seen.add(rurl)
        kind = "HLS" if ".m3u8" in low or "mpegurl" in ctype else "DASH"
        log(f"      [CAPTURE] type={kind} HTTP={status} url={redact(rurl)}")
        if status < 400 and len(candidates) < MAX_CANDIDATES:
            candidates.append((rurl, kind))

    page.on("response", on_response)
    try:
        response = await page.goto(request_url(url), wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
        status = response.status if response else 0
        title = normalize_text(await page.title()).lower()
        if status in (403, 429, 503) or any(marker in title for marker in CF_MARKERS):
            challenge = True
        await page.wait_for_timeout(1200)
        try:
            await page.evaluate("""() => {
                for (const v of document.querySelectorAll('video')) {
                    try { v.muted = true; v.play().catch(() => {}); } catch (_) {}
                }
            }""")
        except Exception:
            pass
        for selector in (
            "button[aria-label*='play' i]",
            ".vjs-big-play-button",
            ".jw-icon-play",
            "[data-plyr='play']",
        ):
            try:
                loc = page.locator(selector).first
                if await loc.count():
                    await loc.click(timeout=1000, force=True)
                    await page.wait_for_timeout(500)
            except Exception:
                pass
        await page.wait_for_timeout(PLAYER_CAPTURE_WAIT * 1000)
    except Exception as exc:
        if challenge:
            return candidates, "CLOUDFLARE_CHALLENGE"
        return candidates, "PLAYWRIGHT_ERROR:" + str(exc)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if challenge and not candidates:
        return candidates, "CLOUDFLARE_CHALLENGE"
    return candidates, None

def write_m3u(channels):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    count = 0
    for channel in sorted(channels, key=lambda x: str(x.get("name", "")).lower()):
        name = normalize_text(channel.get("name"))
        if not name:
            continue
        group = "BHOOM TV | " + ("Tamil Local" if channel.get("section") == "local" else "Tamil")
        tvg_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "channel"
        for stream in channel.get("streams", []):
            url = str(stream.get("url") or "").strip()
            if not url:
                continue
            attrs = f'tvg-id="{tvg_id}" tvg-name="{name}" group-title="{group}"'
            if channel.get("logo"):
                attrs += f' tvg-logo="{normalize_text(channel["logo"])}"'
            lines.append(f"#EXTINF:-1 {attrs},{name}")
            headers = stream.get("headers") or {}
            if headers.get("referer"):
                lines.append(f"#EXTVLCOPT:http-referrer={headers['referer']}")
            if headers.get("user-agent"):
                lines.append(f"#EXTVLCOPT:http-user-agent={headers['user-agent']}")
            lines.append(url)
            lines.append("")
            count += 1
    OUTPUT_M3U.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return count

async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    selected = list(SECTIONS) if CATEGORY_SECTION == "all" else [CATEGORY_SECTION]
    if any(section not in SECTIONS for section in selected):
        raise SystemExit("CATEGORY_SECTION must be all, tamil, or local")

    log("=== BHOOM FRESH SCRAPER ===")
    log(f"Started: {now()}")
    log(f"Sections: {', '.join(selected)}")
    log(f"CATEGORY_PAGE_LIMIT={CATEGORY_PAGE_LIMIT} (0=unlimited)")
    log(f"MAX_CHANNELS={MAX_CHANNELS} (0=all)")
    log(f"VALIDATE_STREAMS={int(VALIDATE_STREAMS)}")
    log(f"STABILITY_SECONDS={STABILITY_SECONDS}")
    log("Cloudflare policy: detect and stop; never bypass")
    if PROXY_BASE_URL:
        log(f"Bhoom proxy: {PROXY_BASE_URL}")
    else:
        log("Bhoom proxy: DISABLED (direct access)")

    inventory = load_inventory()
    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        blocked_sections = {}
        worker_inventory = await refresh_worker_inventory(client)

        if worker_inventory:
            merge_inventory(inventory, list(worker_inventory.values()))
            log(f"Worker inventory usable: {len(worker_inventory)} channels")
            for section in selected:
                blocked_sections[section] = False
        else:
            log("Worker inventory unavailable; falling back to direct category discovery")
            for section in selected:
                rows, blocked = await discover_section(client, section)
                merge_inventory(inventory, rows)
                blocked_sections[section] = blocked
                log(f"Section {section}: {len(rows)} channels discovered this run")
                await asyncio.sleep(0.5)

            await sitemap_fallback(client, inventory)

        save_inventory(inventory)

        channels = [x for x in inventory.values() if x.get("section") in selected]
        channels.sort(key=lambda x: str(x.get("name", "")).lower())
        if MAX_CHANNELS:
            channels = channels[:MAX_CHANNELS]

        log(f"Inventory total: {len(inventory)}")
        log(f"Channels to scan: {len(channels)}")
        log("MAX_CHANNELS=0 means ALL channels in selected inventory")

        playwright = None
        browser = None
        context = None
        page = None
        if channels:
            try:
                from playwright.async_api import async_playwright
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                )
                page = await context.new_page()
                log("Playwright capture engine: READY")
            except Exception as exc:
                log(f"Playwright capture engine unavailable: {exc}")

        output_channels = []
        failures = []

        try:
            for index, channel in enumerate(channels, start=1):
                name = channel.get("name", channel.get("slug", "Unknown"))
                url = channel["url"]
                log(f"\n[{index}/{len(channels)}] [{url}]")
                result = await fetch(client, url, attempts=2)
                candidates = []
                reason = None
                via = "html"

                if result.get("blocked"):
                    reason = result["blocked"]
                    log("  CLOUDFLARE BLOCKED: live page unavailable")
                elif result["status"] == 200:
                    candidates = extract_streams(result["text"])
                    for stream_url, kind in candidates:
                        log(f"  [HTML STREAM] type={kind} url={redact(stream_url)}")
                elif result["status"] == 404:
                    reason = "HTTP_404"
                elif result["status"]:
                    reason = f"HTTP_{result['status']}"
                else:
                    reason = result.get("error", "NETWORK_ERROR")

                if not candidates and page is not None:
                    via = "playwright"
                    log("  No HTML stream URL found; observing normal browser network traffic")
                    captured, capture_reason = await capture_with_browser(page, url)
                    for item in captured:
                        if item not in candidates:
                            candidates.append(item)
                    if capture_reason and not candidates:
                        reason = capture_reason

                candidates = candidates[:MAX_CANDIDATES]
                log(f"  Captured stream candidates: {len(candidates)}")
                streams = []
                validations = []

                for stream_index, (stream_url, kind) in enumerate(candidates, start=1):
                    if VALIDATE_STREAMS:
                        log(f"    [VALIDATE {stream_index}/{len(candidates)}] {kind} {redact(stream_url)}")
                        validation = await (
                            validate_hls(client, stream_url, url)
                            if kind == "HLS"
                            else validate_dash(client, stream_url, url)
                        )
                        validations.append(validation.__dict__)
                        log(
                            f"      [VALIDATION] HTTP={validation.http_status} "
                            f"manifest={validation.manifest_valid} "
                            f"segment={validation.segment_status} "
                            f"segment_checked={validation.segment_checked} "
                            f"stable={validation.stable} changed={validation.changed} "
                            f"USABLE={validation.usable} REASON={validation.reason}"
                        )
                        if not validation.usable:
                            continue
                        validation_data = validation.__dict__
                    else:
                        validation_data = None

                    streams.append({
                        "url": stream_url,
                        "type": kind,
                        "headers": {"referer": url, "user-agent": UA},
                        "validation": validation_data,
                    })

                if not streams and not reason:
                    reason = "STREAM_VALIDATION_FAILED" if candidates else "NO_STREAM_FOUND"

                log(
                    f"  [CHANNEL RESULT] {name} -> USABLE={len(streams)} "
                    f"CAPTURED={len(candidates)} BLOCKED_REASON={reason or 'NONE'}"
                )

                item = dict(channel)
                item["streams"] = streams
                item["last_scan_at"] = now()
                item["scan"] = {
                    "captured": len(candidates),
                    "usable": len(streams),
                    "captured_via": via,
                    "blocked_reason": reason,
                    "validations": validations,
                }

                if streams:
                    output_channels.append(item)
                else:
                    failures.append({
                        "name": name,
                        "url": url,
                        "section": channel.get("section"),
                        "reason": reason,
                    })
        finally:
            for obj in (page, context, browser):
                if obj is not None:
                    try:
                        await obj.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass

        stream_count = write_m3u(output_channels)
        stats = {
            "inventory_total": len(inventory),
            "scanned": len(channels),
            "channels_written": len(output_channels),
            "streams_written": stream_count,
            "failed_channels": len(failures),
            "section_blocked": blocked_sections,
        }
        OUTPUT_JSON.write_text(
            json.dumps(
                {
                    "generated_at": now(),
                    "source": BASE_URL,
                    "sections": selected,
                    "stats": stats,
                    "channels": output_channels,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        OUTPUT_REPORT.write_text(
            json.dumps(
                {
                    "generated_at": now(),
                    "source": BASE_URL,
                    "stats": stats,
                    "failures": failures,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        log("")
        log("=== FINAL RESULT ===")
        log(f"Inventory total: {len(inventory)}")
        log(f"Scanned: {len(channels)}")
        log(f"Channels written: {len(output_channels)}")
        log(f"Stream entries written: {stream_count}")
        log(f"Failed channels: {len(failures)}")
        for section, blocked in blocked_sections.items():
            if blocked:
                log(f"WARNING: {section} discovery stopped on Cloudflare; known inventory retained.")
        log("====================")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
