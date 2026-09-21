import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://bhoomtv.org"
GROUP_PAGE_HINTS = ("/live/kollywood-plus/", "/live/kollywood-tv/")

CATEGORY_SECTION = os.getenv("CATEGORY_SECTION", "all").strip().lower()
CATEGORY_SEEDS = [
    # These are the two BhoomTV sections we want to scan completely.
    f"{BASE}/channel/tamil/",
    f"{BASE}/channel/tamil-local-tv/",
]
if CATEGORY_SECTION == "tamil":
    CATEGORY_SEEDS = [f"{BASE}/channel/tamil/"]
elif CATEGORY_SECTION == "local":
    CATEGORY_SEEDS = [f"{BASE}/channel/tamil-local-tv/"]

OUT = Path("../output")
DEBUG = Path("../debug")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

MANIFEST_RE = re.compile(
    r"""(?i)https?://[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?"""
)

CONTENT_TYPE_HINTS = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)

MAX_CHANNELS = int(os.getenv("MAX_CHANNELS", "0") or "0")
CATEGORY_PAGE_LIMIT = max(0, int(os.getenv("CATEGORY_PAGE_LIMIT", "0") or "0"))
DEBUG_CHANNELS = int(os.getenv("DEBUG_CHANNELS", "3") or "3")
VALIDATE_STREAMS = os.getenv("VALIDATE_STREAMS", "1") != "0"
STABILITY_SECONDS = max(0, int(os.getenv("STABILITY_SECONDS", "3") or "0"))
PLAYER_INITIAL_WAIT_SECONDS = max(1, int(os.getenv("PLAYER_INITIAL_WAIT_SECONDS", "10") or "10"))
PLAYER_CAPTURE_WAIT_SECONDS = max(1, int(os.getenv("PLAYER_CAPTURE_WAIT_SECONDS", "5") or "5"))
PUBLISH_SENSITIVE_HEADERS = os.getenv("PUBLISH_SENSITIVE_HEADERS", "0") != "0"
MIN_CHANNEL_RETENTION_PERCENT = max(0, min(100, int(os.getenv("MIN_CHANNEL_RETENTION_PERCENT", "50") or "50")))
MIN_REQUIRED_CHANNELS = max(0, int(os.getenv("MIN_REQUIRED_CHANNELS", "0") or "0"))
RECAPTURE_ON_FAILURES = os.getenv("RECAPTURE_ON_FAILURES", "1") != "0"
RECAPTURE_ROUNDS = max(0, int(os.getenv("RECAPTURE_ROUNDS", "1") or "1"))
RETRY_COUNT = max(1, int(os.getenv("RETRY_COUNT", "3") or "3"))
RECOVER_PREVIOUS_ON_CLOUDFLARE = os.getenv("RECOVER_PREVIOUS_ON_CLOUDFLARE", "1") != "0"

CLOUDFLARE_FAIL_FAST = os.getenv("CLOUDFLARE_FAIL_FAST", "1") != "0"
CLOUDFLARE_WAIT_SECONDS = max(0, int(os.getenv("CLOUDFLARE_WAIT_SECONDS", "1") or "1"))
PAGE_NAV_TIMEOUT_SECONDS = max(5, int(os.getenv("PAGE_NAV_TIMEOUT_SECONDS", "30") or "30"))

CLOUDFLARE_MARKERS = (
    "challenges.cloudflare.com",
    "challenge-platform",
    "turnstile",
    "cf-chl-",
    "/cdn-cgi/challenge",
    "just a moment",
    "verify you are human",
    "checking your browser",
)

async def is_cloudflare_challenge(page):
    """Return True when the page is a Cloudflare/Turnstile challenge rather than the requested page."""
    try:
        current_url = (page.url or "").lower()
        if any(marker in current_url for marker in CLOUDFLARE_MARKERS):
            return True

        frames = [(frame.url or "").lower() for frame in page.frames]
        if any(any(marker in frame for marker in CLOUDFLARE_MARKERS) for frame in frames):
            return True

        title = ""
        try:
            title = (await page.title()).lower()
        except Exception:
            pass
        if any(marker in title for marker in CLOUDFLARE_MARKERS):
            return True

        body = ""
        try:
            body = (await page.locator("body").inner_text(timeout=1500)).lower()
        except Exception:
            pass
        return any(marker in body for marker in CLOUDFLARE_MARKERS)
    except Exception:
        return False

async def goto_bhoom(page, url, timeout_seconds=PAGE_NAV_TIMEOUT_SECONDS):
    """Navigate once and classify Cloudflare challenges without attempting to defeat them."""
    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=max(5, timeout_seconds) * 1000,
        )

        # Cloudflare explicitly marks Managed Challenges with this header.
        # Treat that as a deterministic block rather than trying player
        # extraction against the challenge document.
        try:
            response_headers = await response.all_headers() if response else {}
        except Exception:
            response_headers = {}
        cf_mitigated = (response_headers.get("cf-mitigated") or "").lower()
        server = (response_headers.get("server") or "").lower()
        header_blocked = (
            cf_mitigated == "challenge"
            or ("cloudflare" in server and response and response.status in {403,429})
        )

        if CLOUDFLARE_WAIT_SECONDS:
            await page.wait_for_timeout(CLOUDFLARE_WAIT_SECONDS * 1000)
        blocked = header_blocked or await is_cloudflare_challenge(page)
        return response, blocked
    except PlaywrightTimeoutError:
        blocked = await is_cloudflare_challenge(page)
        return None, blocked

# Verified direct HLS sources supplied for TBC TV. These are used only as a
# fallback when the BhoomTV channel page is inaccessible or does not expose
# the player stream to the runner. Both sources are retained as candidates.
KNOWN_TAMIL_STREAMS = {
    "tbc-tv": [
        {
            "url": "https://stream.iplive.xyz/smmedia/tbctv/index.m3u8",
            "type": "hls",
        },
        {
            "url": "https://stream.iplive.xyz/smmedia/tbctv/tracks-v1a1/mono.m3u8",
            "type": "hls",
        },
    ],
}


async def discover_tamil_category_pages(page):
    """Discover every available pagination page for the selected Tamil section.

    CATEGORY_PAGE_LIMIT=0 is genuinely unlimited. When BhoomTV exposes a
    rendered "Page X of N" value, N is used as the current end marker. There
    is intentionally no hard 50/500-page ceiling.
    """
    discovered = set()
    live_channels = set()

    for seed in CATEGORY_SEEDS:
        base = seed.rstrip("/")
        print(f"Discovering complete section: {base}")

        page_limit = CATEGORY_PAGE_LIMIT if CATEGORY_PAGE_LIMIT > 0 else None
        page_number = 1
        consecutive_empty = 0
        known_last_page = None

        while page_limit is None or page_number <= page_limit:
            category_url = (
                f"{base}/" if page_number == 1
                else f"{base}/page/{page_number}/"
            )
            try:
                response, cf_blocked = await goto_bhoom(page, category_url)
                status = response.status if response else 0
                print(f"  page {page_number}: HTTP {status}")

                if cf_blocked and CLOUDFLARE_FAIL_FAST:
                    print("  CLOUDFLARE BLOCKED: category page returned a Cloudflare challenge")
                    break

                if status == 404:
                    print(f"  {category_url} -> 404, stopping this section")
                    break

                found = set()
                try:
                    hrefs = await page.locator("a").evaluate_all(
                        """els => els.map(a => ({
                            href: a.href || a.getAttribute('href') || '',
                            dataHref: a.getAttribute('data-href') || '',
                            dataUrl: a.getAttribute('data-url') || ''
                        }))"""
                    )
                    for item in hrefs:
                        for raw in (
                            item.get("href", ""),
                            item.get("dataHref", ""),
                            item.get("dataUrl", ""),
                        ):
                            if raw and "/live/" in raw:
                                found.add(urljoin(BASE, raw))
                except Exception:
                    pass

                try:
                    html = await page.content()
                    for raw in re.findall(
                        r'''(?i)(?:https?:)?//[^"'<>\\s]+/live/[a-z0-9-]+/?''',
                        html,
                    ):
                        found.add(urljoin(BASE, raw))
                    for raw in re.findall(
                        r'''(?i)(?:href|data-href|data-url)\\s*=\\s*["']([^"']*?/live/[^"']*)["']''',
                        html,
                    ):
                        found.add(urljoin(BASE, raw))
                except Exception:
                    html = ""

                try:
                    body_text = await page.locator("body").inner_text()
                except Exception:
                    body_text = ""

                for match in re.findall(
                    r"(?i)Page\\s+(\\d+)\\s+of\\s+(\\d+)",
                    body_text,
                ):
                    current_page, total_pages = map(int, match)
                    if current_page == page_number:
                        known_last_page = max(known_last_page or 0, total_pages)

                pagination_candidates = set(
                    int(x)
                    for x in re.findall(
                        rf'''(?i){re.escape(base)}/page/(\\d+)/?''',
                        html or "",
                    )
                )
                if pagination_candidates:
                    known_last_page = max(
                        known_last_page or 0,
                        max(pagination_candidates),
                    )

                clean_found = set()
                for raw in found:
                    try:
                        parsed = urlparse(raw)
                        if (
                            parsed.netloc.lower() == urlparse(BASE).netloc.lower()
                            and parsed.path.lower().startswith("/live/")
                        ):
                            clean_found.add(f"{BASE}{parsed.path.rstrip('/')}/")
                    except Exception:
                        pass

                if clean_found:
                    # Only retain category pages that actually exposed live
                    # channel links. A challenge/error page must never count
                    # as successful pagination discovery.
                    discovered.add(category_url.rstrip("/") + "/")
                    live_channels.update(clean_found)
                    consecutive_empty = 0
                    print(
                        f"  page {page_number}: {len(clean_found)} live channel links"
                        + (f" / last page {known_last_page}" if known_last_page else "")
                    )
                else:
                    consecutive_empty += 1
                    print(
                        f"  page {page_number}: no live channel links"
                        + (f" / last page {known_last_page}" if known_last_page else "")
                    )

                if known_last_page is not None and page_number >= known_last_page:
                    print(f"  reached detected final page {known_last_page}")
                    break

                if known_last_page is None and consecutive_empty >= 2:
                    print("  no pagination metadata after two empty pages; stopping")
                    break

            except Exception as exc:
                print(f"  page {page_number} discovery error: {exc}")
                continue
            finally:
                page_number += 1

    return sorted(discovered), sorted(live_channels)


def slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "channel"


def channel_name_from_url(url: str) -> str:
    return slug(url).replace("-", " ").title()


def normalize_manifest_url(url: str, content_type: str = ""):
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("blob:"):
        return None
    if url.startswith(("rtmp://", "rtmps://")):
        return url
    if not url.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url)
        if parsed.netloc.lower().endswith("bhoomtv.org") and parsed.path.startswith("/jwplayer"):
            source = parse_qs(parsed.query).get("source", [None])[0]
            return normalize_manifest_url(unquote(source), content_type) if source else None

        path = parsed.path.lower()
        ct = (content_type or "").lower()

        # Some IPTV providers use extensionless stream URLs. Accept them when
        # the browser response explicitly identifies HLS/DASH content.
        known_by_url = any(
            path.endswith(x)
            for x in (".m3u8", ".mpd", ".mp4", ".webm", ".aac", ".mp3")
        )
        known_by_type = any(x in ct for x in CONTENT_TYPE_HINTS) or any(
            x in ct for x in ("video/mp4", "video/webm", "audio/aac", "audio/mpeg")
        )

        if not known_by_url and not known_by_type:
            return None
        return url
    except Exception:
        return None


def stream_type(url: str, content_type: str = ""):
    low = url.lower()
    ct = (content_type or "").lower()
    if low.startswith(("rtmp://", "rtmps://")):
        return "rtmp"
    if ".mpd" in low or "dash+xml" in ct:
        return "dash"
    if ".m3u8" in low or "mpegurl" in ct:
        return "hls"
    # Keep extensionless HTTP media URLs when the browser identified them as
    # media. They are valid IPTV inputs even without a .m3u8/.mpd suffix.
    return "progressive"


class Capture:
    def __init__(self, channel_url: str):
        self.channel_url = channel_url
        self.items = {}
        self.drm_urls = set()

    async def request(self, req):
        url = req.url
        if any(x in url.lower() for x in ("license", "widevine", "playready", "fairplay", "clearkey", "drm")):
            self.drm_urls.add(url)
        if (
            MANIFEST_RE.search(url)
            or url.lower().startswith(("rtmp://", "rtmps://"))
            or req.resource_type in {"media", "manifest"}
        ):
            try:
                headers = await req.all_headers()
            except Exception:
                headers = {}
            self.add(url, headers, None, headers.get("content-type", ""), req.resource_type)

    async def response(self, response):
        url = response.url
        try:
            headers = await response.all_headers()
        except Exception:
            headers = {}
        content_type = headers.get("content-type", "").lower()
        if MANIFEST_RE.search(url) or any(x in content_type for x in CONTENT_TYPE_HINTS):
            self.add(url, headers, response.status, content_type, None)

    def add(self, url, headers, status, content_type, resource_type):
        url = normalize_manifest_url(url, content_type)
        if not url:
            return
        # Preserve only headers actually observed on the media request.
        # Direct channels must not receive invented Referer/User-Agent headers.
        clean_headers = {
            k: headers.get(k)
            for k in ("referer", "origin", "user-agent", "authorization", "cookie")
            if headers.get(k)
        }
        current = self.items.get(url, {})
        current.update({
            "url": url,
            "type": stream_type(url, content_type),
            "headers": clean_headers,
        })
        current["tokenized"] = looks_tokenized(url)
        if status is not None:
            current["status"] = status
        if content_type:
            current["contentType"] = content_type
        if resource_type:
            current["resourceType"] = resource_type
        self.items[url] = current


def looks_tokenized(url: str):
    try:
        keys = {k.lower() for k in parse_qs(urlparse(url).query)}
        return bool(keys & {"token", "tokenid", "auth", "authorization", "signature", "sig", "expires", "exp", "hdnts", "hmac", "key"})
    except Exception:
        return False


def parse_manifest(body: str, content_type: str, url: str):
    low = body.lower()
    result = {
        "manifestValid": False,
        "variants": 0,
        "encrypted": False,
        "drm": False,
        "drmSystems": [],
        "licenseUrls": [],
    }
    if ".m3u8" in url.lower():
        result["manifestValid"] = "#extm3u" in low
        result["variants"] = body.count("#EXT-X-STREAM-INF")
        result["encrypted"] = "#ext-x-key" in low or "#ext-x-session-key" in low
        for system, marker in (
            ("widevine", "widevine"),
            ("playready", "playready"),
            ("fairplay", "fairplay"),
            ("clearkey", "clearkey"),
            ("skd", "skd://"),
        ):
            if marker in low:
                result["drm"] = True
                result["drmSystems"].append(system)
    elif ".mpd" in url.lower():
        result["manifestValid"] = "<mpd" in low
        result["variants"] = body.count("<representation")
        if "contentprotection" in low:
            result["encrypted"] = True
            result["drm"] = True
            for system, marker in (
                ("widevine", "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"),
                ("playready", "9a04f079-9840-4286-ab92-e65be0885f95"),
                ("clearkey", "e2719d58-a985-b3c9-781a-b030af78d30e"),
            ):
                if marker in low:
                    result["drmSystems"].append(system)
    else:
        result["manifestValid"] = True
    result["drmSystems"] = sorted(set(result["drmSystems"]))
    return result


async def fetch_with_retries(page, url, headers, timeout=20000, retries=RETRY_COUNT):
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            response = await page.request.get(url, headers=headers, timeout=timeout, fail_on_status_code=False)
            if response.status in {401,403,408,425,429} or response.status >= 500:
                if attempt + 1 < retries:
                    await asyncio.sleep(min(2 * (attempt + 1), 5))
                    continue
            return response
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                await asyncio.sleep(min(2 * (attempt + 1), 5))
    if last_error:
        raise last_error
    return None


async def validate_hls_segments(page, manifest_url, body, headers):
    lines = [x.strip() for x in body.splitlines() if x.strip() and not x.startswith("#")]
    if not lines:
        return {"segmentChecked": False, "segmentStatus": None, "segmentValid": False}
    for raw in lines[:3]:
        candidate = urljoin(manifest_url, raw)
        try:
            response = await fetch_with_retries(page, candidate, headers, timeout=15000, retries=2)
            if 200 <= response.status < 400:
                rh = await response.all_headers()
                return {"segmentChecked": True, "segmentStatus": response.status, "segmentValid": True, "segmentContentType": rh.get("content-type", ""), "segmentUrl": candidate}
        except Exception:
            pass
    return {"segmentChecked": True, "segmentStatus": None, "segmentValid": False}


async def validate_stream(page, stream):
    result = {
        "checked": False,
        "httpStatus": stream.get("status"),
        "contentType": stream.get("contentType", ""),
        "redirects": [],
        "manifestValid": None,
        "variants": None,
        "encrypted": False,
        "drm": False,
        "drmSystems": [],
        "error": None,
        "stable": None,
        "validationMode": None,
    }
    if stream["type"] == "rtmp":
        result.update({
            "checked": True,
            "transport": "rtmp",
            "note": "RTMP is detected but cannot be HTTP-validated."
        })
        return result

    # BhoomTV has mixed stream requirements. Try the headers actually observed
    # first, then progressively simpler requests so direct streams are not
    # rejected just because another channel needs Referer/User-Agent.
    captured_headers = dict(stream.get("headers") or {})
    header_attempts = []
    for label, headers in (
        ("captured", captured_headers),
        ("direct", {}),
        ("user-agent", {"user-agent": UA}),
        ("browser-referer", {"user-agent": UA, "referer": page.url}),
    ):
        if headers not in [x[1] for x in header_attempts]:
            header_attempts.append((label, headers))

    last_error = None
    for mode, headers in header_attempts:
        try:
            response = await fetch_with_retries(
                page, stream["url"], headers,
                timeout=20000, retries=RETRY_COUNT
            )
            result["checked"] = True
            result["httpStatus"] = response.status
            response_headers = await response.all_headers()
            result["contentType"] = response_headers.get("content-type", "")
            if response.url and response.url != stream["url"]:
                result["redirects"] = [response.url]
            if response.status >= 400:
                last_error = f"HTTP {response.status}"
                continue

            result["validationMode"] = mode

            if stream["type"] in {"hls", "dash"}:
                body = await response.text()
                result.update(parse_manifest(body, result["contentType"], stream["url"]))
                if stream["type"] == "hls" and result["manifestValid"] and not result["drm"]:
                    result.update(await validate_hls_segments(page, stream["url"], body, headers))
                if STABILITY_SECONDS > 0 and result["manifestValid"]:
                    await asyncio.sleep(STABILITY_SECONDS)
                    response2 = await fetch_with_retries(
                        page, stream["url"], headers,
                        timeout=20000, retries=2
                    )
                    result["stable"] = bool(response2 and response2.status < 400)
            else:
                result["manifestValid"] = True
            return result
        except Exception as exc:
            last_error = str(exc)

    result["error"] = last_error or "validation failed"
    return result


def attach_capture(page, capture):
    page.on("request", capture.request)
    page.on("response", capture.response)


async def trigger_playback(page):
    try:
        for selector in [
            "button[aria-label*='play' i]",
            "[role='button'][aria-label*='play' i]",
            ".jw-icon-playback",
            ".jw-display-icon-container",
            ".vjs-big-play-button",
            ".plyr__control--overlaid",
            ".fp-ui",
            ".player",
        ]:
            loc = page.locator(selector)
            count = await loc.count()
            for i in range(min(count, 3)):
                try:
                    await loc.nth(i).click(timeout=1500, force=True)
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass

        videos = page.locator("video")
        count = await videos.count()
        for i in range(min(count, 5)):
            try:
                await videos.nth(i).evaluate(
                    """v => { try { const p=v.play(); if(p&&p.catch)p.catch(()=>{}); } catch(_){} }"""
                )
            except Exception:
                pass
    except Exception:
        pass


async def playback_status(page):
    try:
        return await page.locator("video").evaluate_all(
            """els => els.map(v => ({
                readyState: v.readyState,
                paused: v.paused,
                currentTime: v.currentTime || 0,
                duration: Number.isFinite(v.duration) ? v.duration : null,
                error: v.error ? {code:v.error.code, message:v.error.message || ''} : null
            }))"""
        )
    except Exception:
        return []


async def collect_performance_urls(page, capture=None):
    """Collect manifest URLs from browser performance resources.

    capture is optional as a defensive compatibility guard. All normal callers
    pass the shared Capture instance; if an unexpected one-argument call ever
    occurs, create a local capture instead of aborting the whole channel scan.
    """
    if capture is None:
        capture = Capture(page.url)
    try:
        entries = await page.evaluate(
            """performance.getEntriesByType('resource').map(e=>e.name).filter(Boolean)"""
        )
        for url in set(entries):
            if re.search(r"(?i)\.(?:m3u8|mpd)(?:\?|$)", url):
                # URL discovered from performance timing; no header requirement
                # is assumed unless the actual request captured one.
                capture.add(url, {}, None, "", "performance")
    except Exception:
        pass


async def collect_embedded_urls(page, capture):
    try:
        html = await page.content()
        for raw_url in set(MANIFEST_RE.findall(html)):
            capture.add(raw_url, {}, None, "", "embedded")
        for raw in re.findall(r"""(?i)(?:source|file|src)[=:]["']([^"']+)["']""", html):
            capture.add(unquote(raw), {}, None, "", "embedded-source")
    except Exception:
        pass


async def get_dooplayer_sources(page, capture, channel_url):
    sources = []
    try:
        options = await page.locator("li.dooplay_player_option").evaluate_all(
            """els => els.map(el => ({
                post: el.getAttribute('data-post') || '',
                type: el.getAttribute('data-type') || 'movie',
                nume: el.getAttribute('data-nume') || '',
                title: (el.innerText || el.textContent || '').trim()
            }))"""
        )
    except Exception:
        options = []

    api_base = "https://bhoomtv.org/wp-json/dooplayer/v2/"
    print(f"  DooPlayer sources discovered: {len(options)}")

    for source_index, option in enumerate(options, 1):
        post = option["post"]
        media_type = option["type"]
        nume = option["nume"]
        if not post or not nume:
            continue

        api_url = f"{api_base}{post}/{media_type}/{nume}"
        print(f"  source [{source_index}/{len(options)}]: {option.get('title','')}")

        try:
            response = await page.request.get(
                api_url,
                headers={
                    "Referer": channel_url,
                    "User-Agent": UA,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=30000,
            )
            if not response.ok:
                print(f"    API status {response.status}")
                continue

            try:
                data = await response.json()
            except Exception:
                data = {"raw": await response.text()}

            sources.append({"option": option, "apiUrl": api_url, "response": data})
            embed_url = data.get("embed_url") or data.get("url") or "" if isinstance(data, dict) else ""
            if not embed_url:
                print("    no embed_url/url")
                continue

            iframe_srcs = re.findall(r"""<iframe[^>]+src=["']([^"']+)["']""", embed_url, re.I)
            candidates = iframe_srcs or [embed_url]

            for candidate in candidates:
                if not candidate.startswith("http"):
                    continue
                player = None
                try:
                    player = await page.context.new_page()
                    attach_capture(player, capture)
                    await player.goto(candidate, wait_until="domcontentloaded", timeout=30000)
                    await player.wait_for_timeout(PLAYER_INITIAL_WAIT_SECONDS * 1000)
                    await trigger_playback(player)
                    await collect_embedded_urls(player, capture)
                    await collect_performance_urls(player, capture)

                    try:
                        media_urls = await player.locator("video,source").evaluate_all(
                            """els => els.map(el => ({
                                src: el.src || el.currentSrc || '',
                                type: el.type || ''
                            })).filter(x => x.src)"""
                        )
                        for media in media_urls:
                            if media["src"]:
                                capture.add(
                                    media["src"],
                                    {},
                                    None,
                                    media.get("type", ""),
                                    "dom-media",
                                )
                    except Exception:
                        pass

                    await player.wait_for_timeout(PLAYER_CAPTURE_WAIT_SECONDS * 1000)
                    await trigger_playback(player)
                    await collect_performance_urls(player, capture)
                except Exception as e:
                    print(f"    embed error: {e}")
                finally:
                    if player:
                        try:
                            await player.close()
                        except Exception:
                            pass

        except Exception as e:
            print(f"    API error: {e}")

    if sources:
        try:
            DEBUG.mkdir(parents=True, exist_ok=True)
            (DEBUG / f"{slug(channel_url)}-sources.json").write_text(
                json.dumps(sources, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    return sources


async def detect_drm(page, capture=None):
    result = {"encryptedMediaExtensions": False, "encryptedEventSeen": False, "licenseRequests": [], "drmIndicators": []}
    if capture:
        result["licenseRequests"].extend(sorted(capture.drm_urls))
    try:
        result["encryptedMediaExtensions"] = bool(await page.evaluate("() => typeof navigator.requestMediaKeySystemAccess === 'function'"))
    except Exception:
        pass
    try:
        result["encryptedEventSeen"] = bool(await page.evaluate("() => !!document.querySelector('video') && !!document.querySelector('video').mediaKeys"))
    except Exception:
        pass
    try:
        entries = await page.evaluate("performance.getEntriesByType('resource').map(e=>e.name).filter(Boolean)")
        for url in sorted(set(entries)):
            low = url.lower()
            if any(x in low for x in ("license", "widevine", "playready", "fairplay", "drm", "clearkey", "skd://")):
                result["licenseRequests"].append(url)
    except Exception:
        pass
    if result["licenseRequests"] or result["encryptedEventSeen"]:
        result["drmIndicators"].append("encrypted-media/license activity")
    return result


async def scan_channel(context, channel_url, debug=False):
    capture = Capture(channel_url)
    current = await context.new_page()
    attach_capture(current, capture)

    async def on_popup(popup):
        attach_capture(popup, capture)
    current.on("popup", on_popup)

    try:
        print(f"Opening {channel_url}")
        response, cf_blocked = await goto_bhoom(current, channel_url, timeout_seconds=PAGE_NAV_TIMEOUT_SECONDS)
        if cf_blocked and CLOUDFLARE_FAIL_FAST:
            print("  CLOUDFLARE BLOCKED: live page is a challenge page; player sources cannot be discovered")
            return {
                "id": slug(channel_url),
                "name": channel_name_from_url(channel_url),
                "logo": "",
                "pageUrl": channel_url,
                "streams": [],
                "blockedReason": "CLOUDFLARE_CHALLENGE",
            }

        iframe_urls = []
        try:
            iframe_urls = await current.locator("iframe").evaluate_all("els => els.map(x=>x.src).filter(Boolean)")
        except Exception:
            pass

        for frame in current.frames:
            if frame.url.startswith("http"):
                print(f"  frame: {frame.url}")

        await collect_embedded_urls(current, capture)

        filtered_iframes = [
            u for u in dict.fromkeys(iframe_urls)
            if not any(blocked in u.lower() for blocked in (
                "googleads.g.doubleclick.net",
                "googlesyndication.com",
                "google.com/recaptcha",
                "doubleclick.net",
            ))
        ]

        for iframe_url in filtered_iframes:
            p = None
            try:
                p = await context.new_page()
                attach_capture(p, capture)
                await p.goto(iframe_url, wait_until="domcontentloaded", timeout=30000)
                await p.wait_for_timeout(3000)
                await collect_embedded_urls(p, capture)
                await collect_performance_urls(p, capture)
            except Exception:
                pass
            finally:
                if p:
                    try:
                        await p.close()
                    except Exception:
                        pass

        await get_dooplayer_sources(current, capture, channel_url)

        options = current.locator("li.dooplay_player_option")
        count = await options.count()
        print(f"  DooPlayer clickable sources: {count}")
        for i in range(count):
            try:
                await options.nth(i).click(timeout=3000, force=True)
                await current.wait_for_timeout(2500)
                await collect_embedded_urls(current, capture)
                await collect_performance_urls(current, capture)
            except Exception:
                continue

        await trigger_playback(current)
        await current.wait_for_timeout(4500)
        await collect_embedded_urls(current, capture)
        await collect_performance_urls(current, capture)
        drm = await detect_drm(current, capture)
        streams = sorted(capture.items.values(), key=lambda x: x["url"])
        if VALIDATE_STREAMS and streams:
            print(f"  Validating {len(streams)} captured stream(s)")
            for stream in streams:
                stream["validation"] = await validate_stream(current, stream)
                v = stream["validation"]
                print(f'    {stream["type"]} HTTP={v.get("httpStatus")} valid={v.get("manifestValid")} segment={v.get("segmentValid")} drm={v.get("drm")} stable={v.get("stable")}')

            needs_refresh = any(
                not stream_is_usable(s) or
                int(s.get("validation", {}).get("httpStatus") or 0) in {401, 403, 408, 429}
                for s in streams
            )
            if RECAPTURE_ON_FAILURES and needs_refresh and RECAPTURE_ROUNDS > 0:
                for round_no in range(1, RECAPTURE_ROUNDS + 1):
                    print(f"  Fresh URL recapture {round_no}/{RECAPTURE_ROUNDS}")
                    before_urls = set(capture.items.keys())
                    await get_dooplayer_sources(current, capture, channel_url)
                    refreshed_options = current.locator("li.dooplay_player_option")
                    refreshed_count = await refreshed_options.count()
                    for i in range(refreshed_count):
                        try:
                            await refreshed_options.nth(i).click(timeout=3000, force=True)
                            await current.wait_for_timeout(1800)
                            await collect_embedded_urls(current, capture)
                            await collect_performance_urls(current, capture)
                        except Exception:
                            continue
                    await trigger_playback(current)
                    await current.wait_for_timeout(2500)
                    await collect_performance_urls(current, capture)
                    new_streams = [s for url, s in capture.items.items() if url not in before_urls]
                    for fresh_stream in new_streams:
                        fresh_stream["validation"] = await validate_stream(current, fresh_stream)
                    streams = sorted(capture.items.values(), key=lambda x: x["url"])
                    if any(stream_is_usable(s) for s in streams):
                        print("  Fresh working URL captured after rotation/expiry.")
                        break

        name = channel_name_from_url(channel_url)
        logo = ""
        try:
            logo = await current.locator('meta[property="og:image"]').get_attribute("content") or ""
        except Exception:
            pass
        if not logo:
            try:
                logo = await current.locator("img.wp-post-image, .entry-thumb img, article img").first.get_attribute("src") or ""
            except Exception:
                pass
        try:
            h1 = await current.locator("h1").first.text_content(timeout=1500)
            if h1 and h1.strip():
                name = " ".join(h1.split())
        except Exception:
            pass

        if debug:
            DEBUG.mkdir(parents=True, exist_ok=True)
            try:
                await current.screenshot(path=str(DEBUG / f"{slug(channel_url)}.png"), full_page=True)
            except Exception:
                pass
            try:
                (DEBUG / f"{slug(channel_url)}.html").write_text(await current.content(), encoding="utf-8")
            except Exception:
                pass

        return {"id": slug(channel_url), "name": name, "logo": logo, "pageUrl": channel_url,
                "streams": streams, "playback": await playback_status(current), "drm": drm}

    except PlaywrightTimeoutError:
        print(f"  TIMEOUT: {channel_url}")
        return {"id": slug(channel_url), "name": channel_name_from_url(channel_url), "pageUrl": channel_url, "streams": []}
    except Exception as e:
        print(f"  ERROR: {channel_url}: {e}")
        return {"id": slug(channel_url), "name": channel_name_from_url(channel_url), "pageUrl": channel_url, "streams": []}
    finally:
        try:
            await current.close()
        except Exception:
            pass


TRANSIENT_QUERY_KEYS = {
    "token", "tokenid", "auth", "authorization", "signature", "sig",
    "expires", "expire", "exp", "hdnts", "hmac", "key", "session",
}


def normalize_channel_key(name: str):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def stream_key(url: str):
    try:
        p = urlparse(url)
        pairs = []
        for key, values in parse_qs(p.query, keep_blank_values=True).items():
            if key.lower() in TRANSIENT_QUERY_KEYS:
                continue
            for value in values:
                pairs.append((key.lower(), value))
        query = "&".join(f"{k}={v}" for k, v in sorted(pairs))
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}?{query}".rstrip("?")
    except Exception:
        return url.lower()


def stream_is_usable(stream):
    """Return True only for a usable stream after validation.

    When validation is enabled, a captured URL is not considered publishable
    merely because the browser saw a request. HLS/DASH must return a valid
    manifest and, when checked, at least one valid segment. This prevents
    challenge pages, expired tokens, HTML responses, and dead URLs from
    entering the IPTV playlist.
    """
    if not stream.get("url"):
        return False
    if not VALIDATE_STREAMS:
        return True
    if stream.get("type") == "rtmp":
        return True
    v = stream.get("validation") or {}
    status = int(v.get("httpStatus") or 0)
    if not (200 <= status < 400):
        return False
    if v.get("drm"):
        return False
    if v.get("manifestValid") is False:
        return False
    if stream.get("type") in {"hls", "dash"} and v.get("manifestValid") is not True:
        return False
    if v.get("segmentChecked") and v.get("segmentValid") is not True:
        return False
    return True

def stream_quality_score(stream):
    """Higher score = better candidate to keep for a duplicate channel."""
    v = stream.get("validation", {})
    score = 0
    if stream.get("type") == "hls":
        score += 30
    elif stream.get("type") == "dash":
        score += 25
    elif stream.get("type") == "progressive":
        score += 20
    elif stream.get("type") == "rtmp":
        score += 10

    status = int(v.get("httpStatus") or 0)
    if 200 <= status < 300:
        score += 30
    elif 300 <= status < 400:
        score += 20

    if v.get("manifestValid") is True:
        score += 25
    if v.get("stable") is True:
        score += 15
    if v.get("variants", 0):
        score += min(int(v.get("variants") or 0), 10)
    if stream.get("headers"):
        score += 2
    return score


def merge_duplicate_channels(items):
    """
    Remove duplicate channel names, but NEVER discard the whole channel just
    because another duplicate was encountered first.

    For each duplicate group:
      1. collect all streams from all copies;
      2. keep only usable streams;
      3. dedupe equivalent URLs;
      4. sort working streams by quality;
      5. ALWAYS retain at least the best working stream.
    """
    groups = {}

    for item in items:
        key = normalize_channel_key(item.get("name", "") or item.get("id", ""))
        if not key:
            key = item.get("id", "") or "unknown"

        groups.setdefault(key, []).append(item)

    merged = []

    for key, group in groups.items():
        # Prefer the duplicate page that has the largest number of working streams.
        group = sorted(
            group,
            key=lambda x: (
                len(x.get("streams", [])),
                max((stream_quality_score(s) for s in x.get("streams", [])), default=0),
                bool(x.get("logo")),
            ),
            reverse=True,
        )

        base = dict(group[0])
        base["streams"] = []

        if not base.get("logo"):
            for item in group:
                if item.get("logo"):
                    base["logo"] = item["logo"]
                    break

        # Prefer the most complete channel name.
        names = [x.get("name", "") for x in group if x.get("name")]
        if names:
            base["name"] = max(names, key=len)

        all_streams = []
        for item in group:
            for stream in item.get("streams", []):
                if stream_is_usable(stream):
                    all_streams.append(stream)

        unique = {}
        for stream in all_streams:
            key_stream = stream_key(stream["url"])
            old = unique.get(key_stream)
            if old is None or stream_quality_score(stream) > stream_quality_score(old):
                unique[key_stream] = stream

        working = sorted(unique.values(), key=stream_quality_score, reverse=True)

        # Critical rule: a duplicate cleanup must leave at least ONE working stream.
        if working:
            base["streams"] = working
            base["capturedStreamCount"] = sum(len(x.get("streams", [])) for x in group)
            base["usableStreamCount"] = len(working)
            merged.append(base)

    return merged


def ensure_one_working_stream_per_channel(channels):
    """
    Final safety pass before M3U generation.
    Even after global stream dedupe, each channel gets its best remaining
    working stream. This prevents a duplicate stream from accidentally
    deleting the only stream for a channel.
    """
    seen = set()
    output = []

    # Process channels with the strongest working stream first.
    ordered = sorted(
        channels,
        key=lambda c: max(
            (stream_quality_score(s) for s in c.get("streams", [])),
            default=0
        ),
        reverse=True,
    )

    for channel in ordered:
        streams = sorted(channel.get("streams", []), key=stream_quality_score, reverse=True)
        kept = []

        for stream in streams:
            key = stream_key(stream["url"])
            if key not in seen:
                kept.append(stream)
                seen.add(key)

        # If every stream was already used by another channel, keep the
        # channel's best working stream anyway. This guarantees one working
        # entry per unique channel name.
        if not kept and streams:
            kept = [streams[0]]

        if kept:
            copy_channel = dict(channel)
            copy_channel["streams"] = kept
            copy_channel["usableStreamCount"] = len(kept)
            output.append(copy_channel)

    return output


async def known_stream_fallback(context, channel_url):
    """
    Validate verified direct sources for a known channel.

    This fallback does not bypass BhoomTV/Cloudflare. It only uses direct HLS
    URLs that are already known and supplied independently of the page.
    """
    key = slug(channel_url).lower()
    specs = KNOWN_TAMIL_STREAMS.get(key, [])
    if not specs:
        return []

    page = None
    results = []
    try:
        page = await context.new_page()
        for spec in specs:
            stream = {
                "url": spec["url"],
                "type": spec["type"],
                "headers": {},
                "tokenized": looks_tokenized(spec["url"]),
            }
            if VALIDATE_STREAMS:
                stream["validation"] = await validate_stream(page, stream)
                v = stream["validation"]
                print(
                    f"  KNOWN FALLBACK {key}: "
                    f"{stream['type']} HTTP={v.get('httpStatus')} "
                    f"valid={v.get('manifestValid')} "
                    f"segment={v.get('segmentValid')} "
                    f"stable={v.get('stable')}"
                )
            results.append(stream)
    except Exception as exc:
        print(f"  KNOWN FALLBACK ERROR {key}: {exc}")
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass

    return [s for s in results if stream_is_usable(s)]


def classify_stream_failure(stream):
    v = stream.get("validation", {})
    if v.get("drm"):
        return "DRM"
    status = v.get("httpStatus")
    if status:
        status = int(status)
        if status in {401, 403}:
            return f"HTTP {status} / EXPIRED_OR_FORBIDDEN"
        if status == 404:
            return "HTTP 404"
        if status == 429:
            return "HTTP 429"
        if status >= 500:
            return f"HTTP {status}"
        if status >= 400:
            return f"HTTP {status}"
    if v.get("segmentChecked") and not v.get("segmentValid"):
        return "HLS_SEGMENT_FAILED"
    if v.get("manifestValid") is False:
        return "INVALID_MANIFEST"
    if v.get("error"):
        return "TIMEOUT_OR_NETWORK"
    return "NO_WORKING_STREAM"


def public_channel_copy(channel):
    item = dict(channel)
    item["streams"] = []
    for stream in channel.get("streams", []):
        s = dict(stream)
        headers = dict(s.get("headers", {}))
        if not PUBLISH_SENSITIVE_HEADERS:
            headers.pop("authorization", None)
            headers.pop("cookie", None)
        s["headers"] = headers
        item["streams"].append(s)
    return item


def load_previous_channel_count():
    previous = OUT / "bhoom-tamil.json"
    if not previous.exists():
        return None
    try:
        data = json.loads(previous.read_text(encoding="utf-8"))
        return int(data.get("uniqueChannels") or len(data.get("channels", [])))
    except Exception:
        return None

def load_previous_inventory():
    """Load the last published channel inventory for recovery when category pages are blocked."""
    previous = OUT / "bhoom-tamil.json"
    if not previous.exists():
        return {}

    try:
        data = json.loads(previous.read_text(encoding="utf-8"))
    except Exception:
        return {}

    inventory = {}
    for channel in data.get("channels", []) or []:
        page_url = str(channel.get("pageUrl") or "").strip()
        if not page_url:
            continue
        if not page_url.startswith(BASE + "/live/"):
            continue

        clean = page_url.rstrip("/") + "/"
        streams = channel.get("streams") or []
        if not streams:
            continue

        inventory[clean] = channel

    return inventory


async def validate_previous_stream_fallback(context, channel_url, previous_channel):
    """
    Re-use previously published direct streams only when the live BhoomTV
    channel page cannot expose a stream. This is a recovery path, not a
    Cloudflare bypass.
    """
    page = None
    recovered = []

    try:
        page = await context.new_page()

        for old in previous_channel.get("streams", []) or []:
            url = str(old.get("url") or "").strip()
            if not url:
                continue

            stream = {
                "url": url,
                "type": old.get("type") or stream_type(url, old.get("contentType", "")),
                "headers": dict(old.get("headers") or {}),
                "tokenized": looks_tokenized(url),
            }

            if VALIDATE_STREAMS:
                stream["validation"] = await validate_stream(page, stream)
                v = stream["validation"]
                http_status = int(v.get("httpStatus") or 0)
                manifest_ok = v.get("manifestValid") is True
                segment_ok = not v.get("segmentChecked") or v.get("segmentValid") is True
                stream_ok = (
                    stream["type"] == "rtmp"
                    or (
                        200 <= http_status < 400
                        and manifest_ok
                        and segment_ok
                    )
                )

                print(
                    f"  PREVIOUS FALLBACK {slug(channel_url)}: "
                    f"{stream['type']} HTTP={v.get('httpStatus')} "
                    f"valid={v.get('manifestValid')} "
                    f"segment={v.get('segmentValid')} "
                    f"stable={v.get('stable')} "
                    f"keep={stream_ok}"
                )

                if not stream_ok:
                    continue

            recovered.append(stream)

        unique = {}
        for stream in recovered:
            unique[stream_key(stream["url"])] = stream

        return sorted(unique.values(), key=stream_quality_score, reverse=True)

    except Exception as exc:
        print(f"  PREVIOUS FALLBACK ERROR {slug(channel_url)}: {exc}")
        return []
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass



async def scan_multi_source_group(context, channel_url, debug=False):
    """Extract each DooPlayer source on a grouped live page as an individual channel."""
    page = await context.new_page()
    results = []
    try:
        print(f"Opening grouped channel page: {channel_url}")
        await page.goto(channel_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(PLAYER_INITIAL_WAIT_SECONDS * 1000)
        try:
            logo = await page.locator('meta[property="og:image"]').get_attribute("content") or ""
        except Exception:
            logo = ""

        options = await page.locator("li.dooplay_player_option").evaluate_all(
            """els => els.map((el, index) => ({
                index,
                title: (el.innerText || el.textContent || '').trim(),
                post: el.getAttribute('data-post') || '',
                type: el.getAttribute('data-type') || 'tv',
                nume: el.getAttribute('data-nume') || ''
            })).filter(x => x.title)"""
        )
        print(f"  Group source options: {len(options)}")

        for option_index, option in enumerate(options, 1):
            title = " ".join(option["title"].split())
            capture = Capture(channel_url)
            attach_capture(page, capture)

            try:
                await page.locator("li.dooplay_player_option").nth(option["index"]).click(
                    timeout=4000, force=True
                )
                await page.wait_for_timeout(PLAYER_CAPTURE_WAIT_SECONDS * 1000)
                await collect_embedded_urls(page, capture)
                await collect_performance_urls(page, capture)
                await trigger_playback(page)
                await page.wait_for_timeout(PLAYER_CAPTURE_WAIT_SECONDS * 1000)
                await collect_embedded_urls(page, capture)
                await collect_performance_urls(page)
            except Exception as exc:
                print(f"    [{option_index}] click error for {title}: {exc}")

            if not capture.items and option.get("post") and option.get("nume"):
                try:
                    api_url = (
                        f"https://bhoomtv.org/wp-json/dooplayer/v2/"
                        f"{option['post']}/{option.get('type') or 'tv'}/{option['nume']}"
                    )
                    response = await page.request.get(
                        api_url,
                        headers={
                            "Referer": channel_url,
                            "User-Agent": UA,
                            "Accept": "application/json,text/plain,*/*",
                        },
                        timeout=30000,
                    )
                    if response.ok:
                        try:
                            data = await response.json()
                        except Exception:
                            data = {}
                        embed_url = (
                            data.get("embed_url") or data.get("url") or ""
                            if isinstance(data, dict) else ""
                        )
                        iframe_srcs = re.findall(
                            r"""<iframe[^>]+src=["']([^"']+)["']""",
                            embed_url, re.I
                        )
                        for candidate in (iframe_srcs or [embed_url]):
                            if not candidate.startswith("http"):
                                continue
                            player = None
                            try:
                                player = await context.new_page()
                                attach_capture(player, capture)
                                await player.goto(candidate, wait_until="domcontentloaded", timeout=30000)
                                await player.wait_for_timeout(PLAYER_INITIAL_WAIT_SECONDS * 1000)
                                await trigger_playback(player)
                                await collect_embedded_urls(player, capture)
                                await collect_performance_urls(player, capture)
                                await player.wait_for_timeout(PLAYER_CAPTURE_WAIT_SECONDS * 1000)
                                await collect_performance_urls(player, capture)
                            except Exception as exc:
                                print(f"    embed error for {title}: {exc}")
                            finally:
                                if player:
                                    try:
                                        await player.close()
                                    except Exception:
                                        pass
                except Exception as exc:
                    print(f"    API fallback error for {title}: {exc}")

            streams = list(capture.items.values())
            unique = {}
            for stream in streams:
                unique[stream_key(stream["url"])] = stream
            streams = list(unique.values())

            if VALIDATE_STREAMS and streams:
                print(f"    Validating {len(streams)} stream(s) for {title}")
                for stream in streams:
                    stream["validation"] = await validate_stream(page, stream)

            usable = [stream for stream in streams if stream_is_usable(stream)]
            if not usable:
                print(f"    {title}: NO USABLE STREAM")
                continue

            usable.sort(key=stream_quality_score, reverse=True)
            item_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            results.append({
                "id": f"group-{slug(channel_url)}-{item_id or option_index}",
                "name": title,
                "logo": logo,
                "pageUrl": channel_url,
                "sourceTitle": title,
                "sourceIndex": option_index,
                "streams": usable,
                "capturedStreamCount": len(streams),
                "usableStreamCount": len(usable),
            })
            print(f"    {title}: {len(usable)} usable stream(s)")

        if debug:
            DEBUG.mkdir(parents=True, exist_ok=True)
            safe_slug = slug(channel_url)
            try:
                await page.screenshot(path=str(DEBUG / f"{safe_slug}-group.png"), full_page=True)
            except Exception:
                pass
            try:
                (DEBUG / f"{safe_slug}-group.html").write_text(await page.content(), encoding="utf-8")
            except Exception:
                pass
        return results
    except Exception as exc:
        print(f"  GROUP PAGE ERROR: {exc}")
        return []
    finally:
        try:
            await page.close()
        except Exception:
            pass

async def main():
    OUT.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent=UA,
            locale="en-IN",
            viewport={"width": 1440, "height": 1000},
        )
        await context.add_init_script(
            """Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
               Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});"""
        )

        page = await context.new_page()
        channel_pages = set()

        category_pages, discovered_live_channels = await discover_tamil_category_pages(page)
        channel_pages.update(discovered_live_channels)
        print(f"Tamil category pages discovered: {len(category_pages)}")
        print(f"Tamil live channels discovered during pagination: {len(discovered_live_channels)}")

        if CLOUDFLARE_FAIL_FAST and not category_pages:
            raise RuntimeError(
                "BhoomTV category preflight failed: no category page could be read. "
                "The GitHub Actions runner is receiving a Cloudflare/Turnstile challenge. "
                "Provide an authorized machine-readable feed/API or allowlist the runner before production scraping."
            )

        for category_url in category_pages:
            try:
                print(f"Category: {category_url}")
                response, cf_blocked = await goto_bhoom(page, category_url)
                if cf_blocked and CLOUDFLARE_FAIL_FAST:
                    print("  CLOUDFLARE BLOCKED: skipping category extraction")
                    continue
                # BhoomTV may render live links outside the normal anchor DOM.
                # Collect regular anchors, data attributes, and rendered HTML so
                # channel discovery survives site/theme changes.
                found = set()

                try:
                    hrefs = await page.locator("a").evaluate_all(
                        """els => els.map(a => ({
                            href: a.href || a.getAttribute('href') || '',
                            dataHref: a.getAttribute('data-href') || '',
                            dataUrl: a.getAttribute('data-url') || ''
                        }))"""
                    )
                    for item in hrefs:
                        for raw in (item.get("href", ""), item.get("dataHref", ""), item.get("dataUrl", "")):
                            if raw and "/live/" in raw:
                                found.add(urljoin(BASE, raw))
                except Exception:
                    pass

                try:
                    html = await page.content()
                    for raw in re.findall(
                        r'''(?i)(?:https?:)?//[^"'<>\\s]+/live/[a-z0-9-]+/?''',
                        html
                    ):
                        found.add(urljoin(BASE, raw))

                    for raw in re.findall(
                        r'''(?i)(?:href|data-href|data-url)\\s*=\\s*["']([^"']*?/live/[^"']*)["']''',
                        html
                    ):
                        found.add(urljoin(BASE, raw))
                except Exception:
                    pass

                for raw in found:
                    try:
                        parsed = urlparse(raw)
                        if (
                            parsed.netloc.lower() == urlparse(BASE).netloc.lower()
                            and parsed.path.lower().startswith("/live/")
                        ):
                            clean = f"{BASE}{parsed.path.rstrip()}/"
                            channel_pages.add(clean)
                    except Exception:
                        pass

                print(f"  live links discovered on page: {len(found)}")
            except Exception as e:
                print(f"  category error: {e}")

        # If BhoomTV category pages are blocked and yield no live links,
        # recover the previous published channel inventory. This prevents a
        # transient Cloudflare block from reducing the production playlist to
        # a single hard-coded channel.
        previous_inventory = load_previous_inventory()
        discovered_from_categories = set(channel_pages)
        previous_count_for_recovery = len(previous_inventory)

        # Recover from the previous published inventory when category
        # discovery is clearly incomplete (for example, Cloudflare blocks one
        # or both sections). This keeps both Tamil TV and Tamil Local channels
        # available instead of publishing a tiny partial list.
        discovery_threshold = max(
            1,
            int(previous_count_for_recovery * 0.80),
        )
        if (
            previous_inventory
            and len(discovered_from_categories) < discovery_threshold
        ):
            channel_pages.update(previous_inventory.keys())
            print(
                f"Category discovery returned only "
                f"{len(discovered_from_categories)} live links; "
                f"recovered {len(previous_inventory)} channels from previous output."
            )

        # Keep verified direct-source channels available even when the
        # BhoomTV category page is blocked by Cloudflare.
        if CATEGORY_SECTION in {"all", "tamil"}:
            for known_slug in KNOWN_TAMIL_STREAMS:
                channel_pages.add(f"{BASE}/live/{known_slug}/")

        channels = sorted(channel_pages)
        if MAX_CHANNELS > 0:
            channels = channels[:MAX_CHANNELS]

        print(f"Channels discovered: {len(channel_pages)}")
        print(f"Channels to scan: {len(channels)}")
        print("MAX_CHANNELS=0 means ALL channels")

        results = []
        scanned_items = []
        total_streams = 0

        for index, channel_url in enumerate(channels, 1):
            debug = index <= DEBUG_CHANNELS
            print(f"[{index}/{len(channels)}] {channel_url}")

            # Future-proof: any BhoomTV /live/ page exposing multiple
            # DooPlayer source options is treated as a grouped page.
            is_group_page = False
            option_count = 0
            try:
                probe = await context.new_page()
                _, probe_cf_blocked = await goto_bhoom(
                    probe, channel_url,
                    timeout_seconds=min(PAGE_NAV_TIMEOUT_SECONDS, 20),
                )
                if probe_cf_blocked and CLOUDFLARE_FAIL_FAST:
                    option_count = -1
                    is_group_page = False
                else:
                    option_count = await probe.locator("li.dooplay_player_option").count()
                    is_group_page = option_count >= 2
                await probe.close()
            except Exception as exc:
                print(f"  group-page probe skipped: {exc}")

            if option_count == -1:
                print("  CLOUDFLARE BLOCKED: live page unavailable; switching to validated previous-stream recovery")
                item = {
                    "id": slug(channel_url),
                    "name": channel_name_from_url(channel_url),
                    "pageUrl": channel_url,
                    "streams": [],
                    "blockedReason": "CLOUDFLARE_CHALLENGE",
                }
            else:
                item = None

            if item is None:
                print(f"  source options detected: {option_count}")
                if is_group_page:
                    group_items = await scan_multi_source_group(context, channel_url, debug=debug)
                    results.extend(group_items)
                    print(f"  GROUP PAGE: {len(group_items)} individual channels")
                    continue

                item = await scan_channel(context, channel_url, debug=debug)

            # For known channels, add independently supplied direct HLS
            # sources as verified fallbacks/alternates. This is especially
            # useful when the BhoomTV page itself is blocked by Cloudflare.
            fallback_streams = await known_stream_fallback(context, channel_url)
            if fallback_streams:
                existing = {stream_key(s["url"]): s for s in item.get("streams", [])}
                for stream in fallback_streams:
                    existing.setdefault(stream_key(stream["url"]), stream)
                item["streams"] = sorted(
                    existing.values(),
                    key=stream_quality_score,
                    reverse=True,
                )

            # Do not repeatedly validate the old stream inventory after a
            # Cloudflare challenge. That validation is independent of the page,
            # but it can consume most of the run when the same protected page
            # blocks every channel. Only use the previous-stream validation path
            # when the live page itself was actually reached.
            if (
                not item.get("streams")
                and channel_url in previous_inventory
                and (
                    item.get("blockedReason") != "CLOUDFLARE_CHALLENGE"
                    or RECOVER_PREVIOUS_ON_CLOUDFLARE
                )
            ):
                previous_fallback = await validate_previous_stream_fallback(
                    context,
                    channel_url,
                    previous_inventory[channel_url],
                )
                if previous_fallback:
                    item["streams"] = previous_fallback
                    item["recoveryMode"] = "validated-previous-stream"
                    if not item.get("name"):
                        item["name"] = previous_inventory[channel_url].get(
                            "name", channel_name_from_url(channel_url)
                        )
                    if not item.get("logo"):
                        item["logo"] = previous_inventory[channel_url].get("logo", "")
                    print(
                        f"  RECOVERED {channel_url}: "
                        f"{len(previous_fallback)} validated previous stream(s)"
                    )

            scanned_items.append(item)
            captured_count = len(item["streams"])
            total_streams += captured_count
            usable = [s for s in item["streams"] if stream_is_usable(s)]
            item["capturedStreamCount"] = captured_count
            item["usableStreamCount"] = len(usable)
            if usable:
                item["streams"] = usable
                print(f"  USABLE {len(usable)} / CAPTURED {captured_count}")
                results.append(item)
            else:
                print(f"  NO USABLE STREAM / CAPTURED {len(item['streams'])}")

        before_merge = len(results)
        results = merge_duplicate_channels(results)
        print(f"Duplicate channel cleanup: {before_merge} -> {len(results)} unique channels")

        # Final safety pass: duplicate streams must not make a unique channel
        # disappear. Each remaining channel gets at least one working stream.
        results = ensure_one_working_stream_per_channel(results)
        print(f"Final working-channel safety pass: {len(results)} channels retained")

        # Never publish a dramatically smaller playlist just because a source
        # category was temporarily blocked. The existing output remains intact
        # when this threshold is not met, because the workflow fails before its
        # publish step.
        previous_count = load_previous_channel_count()

        if MIN_REQUIRED_CHANNELS > 0 and len(results) < MIN_REQUIRED_CHANNELS:
            raise RuntimeError(
                "Production minimum-channel safety stop: "
                f"{len(results)} channels recovered, but at least "
                f"{MIN_REQUIRED_CHANNELS} are required."
            )
        if (
            previous_count
            and MIN_CHANNEL_RETENTION_PERCENT > 0
            and len(results) * 100 < previous_count * MIN_CHANNEL_RETENTION_PERCENT
        ):
            raise RuntimeError(
                "Production safety stop: only "
                f"{len(results)} of {previous_count} previously published "
                f"channels were recovered, below the configured "
                f"{MIN_CHANNEL_RETENTION_PERCENT}% retention threshold. "
                "No new playlist will be published."
            )

        await context.close()
        await browser.close()

    if not results:
        raise RuntimeError(
            "No HLS/DASH manifests were captured. Pages were reachable, but "
            "the player did not expose a stream to the GitHub runner."
        )

    seen = set()
    m3u = ["#EXTM3U"]

    for channel in results:
        safe_name = channel["name"].replace('"', "'").replace(",", " - ")
        logo = channel.get("logo", "")

        # At this point each channel is guaranteed to have at least one
        # working stream. Prefer the strongest stream first.
        streams = sorted(
            channel["streams"],
            key=stream_quality_score,
            reverse=True,
        )

        channel_written = False

        for stream in streams:
            url = normalize_manifest_url(stream["url"])
            if not url:
                continue

            dedupe_key = stream_key(url)

            # Keep the first/global copy of duplicate URLs, except when this
            # would remove the channel entirely; the safety pass above already