import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://bhoomtv.org"
CATEGORY_PAGES = [
    f"{BASE}/channel/tamil-news/",
    f"{BASE}/channel/tamil/",
    f"{BASE}/channel/tamil/page/2/",
    f"{BASE}/channel/tamil/page/3/",
    f"{BASE}/channel/tamil/page/4/",
    f"{BASE}/channel/tamil-local-tv/",
    *[f"{BASE}/channel/tamil-local-tv/page/{i}/" for i in range(1, 10)],
]

OUT = Path("../output")
DEBUG = Path("../debug")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
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
DEBUG_CHANNELS = int(os.getenv("DEBUG_CHANNELS", "3") or "3")
VALIDATE_STREAMS = os.getenv("VALIDATE_STREAMS", "1") != "0"
STABILITY_SECONDS = max(0, int(os.getenv("STABILITY_SECONDS", "3") or "0"))
PUBLISH_SENSITIVE_HEADERS = os.getenv("PUBLISH_SENSITIVE_HEADERS", "1") != "0"


def slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "channel"


def channel_name_from_url(url: str) -> str:
    return slug(url).replace("-", " ").title()


def normalize_manifest_url(url: str):
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
            return normalize_manifest_url(unquote(source)) if source else None
        path = parsed.path.lower()
        if not any(path.endswith(x) for x in (".m3u8", ".mpd", ".mp4", ".webm", ".aac", ".mp3")):
            return None
        return url
    except Exception:
        return None


def stream_type(url: str):
    low = url.lower()
    if low.startswith(("rtmp://", "rtmps://")):
        return "rtmp"
    if ".mpd" in low:
        return "dash"
    if ".m3u8" in low:
        return "hls"
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
        url = normalize_manifest_url(url)
        if not url:
            return
        clean_headers = {
            k: headers.get(k)
            for k in ("referer", "origin", "user-agent", "authorization", "cookie")
            if headers.get(k)
        }
        clean_headers.setdefault("user-agent", UA)
        clean_headers.setdefault("referer", self.channel_url)
        current = self.items.get(url, {})
        lower_url = url.lower()
        current_type = stream_type(url)
        current.update({
            "url": url,
            "type": current_type,
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
    }
    if stream["type"] == "rtmp":
        result.update({"checked": True, "transport": "rtmp",
                       "note": "RTMP is detected but cannot be HTTP-validated."})
        return result

    try:
        response = await page.request.get(
            stream["url"],
            headers=stream.get("headers", {}),
            timeout=20000,
            fail_on_status_code=False,
        )
        result["checked"] = True
        result["httpStatus"] = response.status
        response_headers = await response.all_headers()
        result["contentType"] = response_headers.get("content-type", "")
        if response.url and response.url != stream["url"]:
            result["redirects"].append(response.url)
        if response.status >= 400:
            result["error"] = f"HTTP {response.status}"
            return result

        if stream["type"] in {"hls", "dash"}:
            body = await response.text()
            result.update(parse_manifest(body, result["contentType"], stream["url"]))
            if STABILITY_SECONDS > 0 and result["manifestValid"]:
                await asyncio.sleep(STABILITY_SECONDS)
                response2 = await page.request.get(
                    stream["url"],
                    headers=stream.get("headers", {}),
                    timeout=20000,
                    fail_on_status_code=False,
                )
                result["stable"] = response2.status < 400
        else:
            result["manifestValid"] = True
    except Exception as e:
        result["error"] = str(e)
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


async def collect_performance_urls(page, capture):
    try:
        entries = await page.evaluate(
            """performance.getEntriesByType('resource').map(e=>e.name).filter(Boolean)"""
        )
        for url in set(entries):
            if re.search(r"(?i)\.(?:m3u8|mpd)(?:\?|$)", url):
                capture.add(url, {"user-agent": UA, "referer": page.url}, None, "", "performance")
    except Exception:
        pass


async def collect_embedded_urls(page, capture):
    try:
        html = await page.content()
        for raw_url in set(MANIFEST_RE.findall(html)):
            capture.add(raw_url, {"user-agent": UA, "referer": page.url}, None, "", "embedded")
        for raw in re.findall(r"""(?i)(?:source|file|src)[=:]["']([^"']+)["']""", html):
            capture.add(unquote(raw), {"user-agent": UA, "referer": page.url}, None, "", "embedded-source")
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

    # IMPORTANT: inspect EVERY source option. There is intentionally no 20-source cap.
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
                    await player.wait_for_timeout(2500)
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
                                    {"user-agent": UA, "referer": candidate},
                                    None,
                                    media.get("type", ""),
                                    "dom-media",
                                )
                    except Exception:
                        pass

                    await player.wait_for_timeout(2500)
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
    """Detect DRM/EME usage without attempting to bypass or extract keys."""
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
        await current.goto(channel_url, wait_until="domcontentloaded", timeout=45000)
        await current.wait_for_timeout(1500)

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

        # Fallback click path also checks EVERY source option.
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
                print(f'    {stream["type"]} HTTP={v.get("httpStatus")} valid={v.get("manifestValid")} drm={v.get("drm")} stable={v.get("stable")}')

        name = channel_name_from_url(channel_url)
        logo = ""
        try:
            logo = await current.locator("meta[property=\"og:image\"]").get_attribute("content") or ""
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
                "streams": streams,
                "playback": await playback_status(current),
                "drm": drm}

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
    validation = stream.get("validation", {})
    if stream["type"] == "rtmp":
        return True
    if validation.get("drm"):
        return False
    status = validation.get("httpStatus")
    if status is None or not (200 <= int(status) < 400):
        return False
    if stream["type"] in {"hls", "dash"}:
        if validation.get("manifestValid") is not True:
            return False
    elif stream["type"] == "progressive":
        if validation.get("manifestValid") is not True:
            return False
    if validation.get("stable") is False:
        return False
    return True

def merge_duplicate_channels(items):
    merged = {}
    for item in items:
        key = normalize_channel_key(item.get("name", "") or item.get("id", ""))
        if not key:
            key = item.get("id", "")
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["streams"] = []
            continue

        existing = merged[key]
        if not existing.get("logo") and item.get("logo"):
            existing["logo"] = item["logo"]
        if len(item.get("name", "")) > len(existing.get("name", "")):
            existing["name"] = item["name"]
        existing["streams"].extend(item.get("streams", []))

    for item in merged.values():
        unique = {}
        for stream in item["streams"]:
            key = stream_key(stream["url"])
            old = unique.get(key)
            if old is None:
                unique[key] = stream
            else:
                old_score = int(old.get("validation", {}).get("httpStatus") or 0)
                new_score = int(stream.get("validation", {}).get("httpStatus") or 0)
                if new_score > old_score:
                    unique[key] = stream
        item["streams"] = list(unique.values())
    return list(merged.values())


async def main():
    OUT.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            locale="en-IN",
            viewport={"width": 1440, "height": 1000},
        )

        page = await context.new_page()
        channel_pages = set()

        for category_url in CATEGORY_PAGES:
            try:
                print(f"Category: {category_url}")
                await page.goto(category_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                links = await page.locator('a[href*="/live/"]').evaluate_all("els=>els.map(a=>a.href)")
                for url in links:
                    if "/live/" in url:
                        channel_pages.add(url.split("#")[0].rstrip("/") + "/")
            except Exception as e:
                print(f"  category error: {e}")

        channels = sorted(channel_pages)
        if MAX_CHANNELS > 0:
            channels = channels[:MAX_CHANNELS]

        print(f"Channels discovered: {len(channel_pages)}")
        print(f"Channels to scan: {len(channels)}")
        print("MAX_CHANNELS=0 means ALL channels")

        results = []
        total_streams = 0

        for index, channel_url in enumerate(channels, 1):
            debug = index <= DEBUG_CHANNELS
            print(f"[{index}/{len(channels)}] {channel_url}")
            item = await scan_channel(context, channel_url, debug=debug)
            total_streams += len(item["streams"])
            usable = [s for s in item["streams"] if stream_is_usable(s)]
            item["capturedStreamCount"] = len(item["streams"])
            item["usableStreamCount"] = len(usable)
            if usable:
                item["streams"] = usable
                print(f"  USABLE {len(usable)} / CAPTURED {len(item['streams'])}")
                results.append(item)
            else:
                print(f"  NO USABLE STREAM / CAPTURED {len(item['streams'])}")

        before_merge = len(results)
        results = merge_duplicate_channels(results)
        print(f"Duplicate channel cleanup: {before_merge} -> {len(results)} unique channels")

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
        for stream in channel["streams"]:
            url = normalize_manifest_url(stream["url"])
            if not url or url in seen:
                continue
            seen.add(url)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""
            m3u.append(f'#EXTINF:-1 tvg-name="{safe_name}"{logo_attr} group-title="Tamil",{safe_name}')
            headers = stream.get("headers", {})
            if headers.get("referer"):
                m3u.append(f'#EXTVLCOPT:http-referrer={headers["referer"]}')
            if headers.get("origin"):
                m3u.append(f'#EXTVLCOPT:http-origin={headers["origin"]}')
            if headers.get("user-agent"):
                m3u.append(f'#EXTVLCOPT:http-user-agent={headers["user-agent"]}')
            if headers.get("authorization"):
                m3u.append(f'#EXTVLCOPT:http-header=Authorization: {headers["authorization"]}')
            if headers.get("cookie"):
                m3u.append(f'#EXTVLCOPT:http-header=Cookie: {headers["cookie"]}')
            m3u.append(url)
            m3u.append("")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "category": "Tamil",
        "channels": results,
        "uniqueChannels": len(results),
        "uniqueStreams": len(seen),
        "capturedStreams": total_streams,
        "usableStreams": len(seen),
        "validation": {"enabled": VALIDATE_STREAMS, "stabilitySeconds": STABILITY_SECONDS},
        "streamFormat": "hls-dash-rtmp-progressive-with-browser-headers",
        "drmNote": "DRM/EME is detected and recorded, but DRM keys/licenses are not bypassed or extracted.",
        "deduplication": {
            "channels": "normalized channel name",
            "streams": "normalized URL with transient token/signature/expiry query parameters removed"
        },
    }

    (OUT / "bhoom-tamil.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "bhoom-tamil.m3u").write_text("\n".join(m3u), encoding="utf-8")

    print("=" * 50)
    print(f"FINAL: {len(results)} channels / {len(seen)} unique streams")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
