import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://bhoomtv.org"
CATEGORY_PAGES = [
    f"{BASE}/channel/tamil/",
    f"{BASE}/channel/tamil/page/2/",
    f"{BASE}/channel/tamil/page/3/",
    f"{BASE}/channel/tamil/page/4/",
]

OUT = Path("../output")
DEBUG = Path("../debug")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

# Catch both normal manifest URLs and manifests whose URL has a token/query string.
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


def slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "channel"


def channel_name_from_url(url: str) -> str:
    return slug(url).replace("-", " ").title()


class Capture:
    def __init__(self, channel_url: str):
        self.channel_url = channel_url
        self.items = {}

    async def request(self, req):
        url = req.url
        resource_type = req.resource_type
        if (
            MANIFEST_RE.search(url)
            or resource_type in {"media", "manifest"}
        ):
            try:
                headers = await req.all_headers()
            except Exception:
                headers = {}
            self.add(
                url=url,
                headers=headers,
                status=None,
                content_type=headers.get("content-type", ""),
                resource_type=resource_type,
            )

    async def response(self, response):
        url = response.url
        try:
            headers = await response.all_headers()
        except Exception:
            headers = {}
        content_type = headers.get("content-type", "").lower()
        hit = (
            MANIFEST_RE.search(url)
            or any(x in content_type for x in CONTENT_TYPE_HINTS)
        )
        if hit:
            self.add(
                url=url,
                headers=headers,
                status=response.status,
                content_type=content_type,
                resource_type=None,
            )

    def add(self, url, headers, status, content_type, resource_type):
        clean_headers = {}
        for key in ("referer", "origin", "user-agent"):
            value = headers.get(key)
            if value:
                clean_headers[key] = value

        clean_headers.setdefault("user-agent", UA)
        clean_headers.setdefault("referer", self.channel_url)

        current = self.items.get(url, {})
        current.update(
            {
                "url": url,
                "type": "dash" if ".mpd" in url.lower() else "hls",
                "headers": clean_headers,
            }
        )
        if status is not None:
            current["status"] = status
        if content_type:
            current["contentType"] = content_type
        if resource_type:
            current["resourceType"] = resource_type

        self.items[url] = current


def attach_capture(page, capture):
    # Page-level listeners include requests made by frames on this page,
    # while avoiding cross-channel capture contamination.
    page.on("request", capture.request)
    page.on("response", capture.response)



async def collect_embedded_urls(page, capture):
    # Inspect page HTML/JS for directly embedded HLS/DASH URLs.
    try:
        html = await page.content()
        for url in set(MANIFEST_RE.findall(html)):
            capture.add(
                url=url,
                headers={"user-agent": UA, "referer": page.url},
                status=None,
                content_type="",
                resource_type="embedded",
            )
    except Exception:
        pass


async def get_dooplayer_sources(page, capture, channel_url):
    sources = []

    try:
        options = await page.locator(
            "li.dooplay_player_option"
        ).evaluate_all(
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

    for option in options:
        post = option["post"]
        media_type = option["type"]
        nume = option["nume"]

        if not post or not nume:
            continue

        api_url = f"{api_base}{post}/{media_type}/{nume}"
        print(f"  source API: {api_url}")

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
                print(f"  API status {response.status}: {api_url}")
                continue

            try:
                data = await response.json()
            except Exception:
                text_body = await response.text()
                data = {"raw": text_body}

            # Keep the API result in debug JSON for diagnosis.
            sources.append({
                "option": option,
                "apiUrl": api_url,
                "response": data,
            })

            embed_url = ""
            if isinstance(data, dict):
                embed_url = data.get("embed_url") or data.get("url") or ""

            if embed_url:
                print(f"  embed_url found ({len(embed_url)} chars)")
            else:
                print("  API returned no embed_url/url")

            if not embed_url:
                continue

            # DooPlayer commonly returns an iframe HTML snippet.
            iframe_srcs = re.findall(
                r"""<iframe[^>]+src=["']([^"']+)["']""",
                embed_url,
                re.I,
            )

            candidates = iframe_srcs or [embed_url]

            for candidate in candidates:
                if not candidate.startswith("http"):
                    continue

                try:
                    player = await page.context.new_page()
                    attach_capture(player, capture)

                    await player.goto(
                        candidate,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await player.wait_for_timeout(4000)
                    await collect_embedded_urls(player, capture)

                    # Also inspect the player DOM for video/source URLs.
                    try:
                        media_urls = await player.locator(
                            "video,source"
                        ).evaluate_all(
                            """els => els.map(el => ({
                                src: el.src || el.currentSrc || '',
                                type: el.type || ''
                            })).filter(x => x.src)"""
                        )
                        for media in media_urls:
                            if media["src"]:
                                capture.add(
                                    url=media["src"],
                                    headers={
                                        "user-agent": UA,
                                        "referer": candidate,
                                    },
                                    status=None,
                                    content_type=media.get("type", ""),
                                    resource_type="dom-media",
                                )
                    except Exception:
                        pass

                    await player.wait_for_timeout(2500)
                    await player.close()

                except Exception as e:
                    print(f"  embed error: {candidate} -> {e}")

        except Exception as e:
            print(f"  API error: {api_url} -> {e}")

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


async def scan_channel(context, channel_url, debug=False):
    capture = Capture(channel_url)
    current = await context.new_page()
    attach_capture(current, capture)

    async def on_popup(popup):
        attach_capture(popup, capture)
    current.on("popup", on_popup)

    try:
        print(f"Opening {channel_url}")
        await current.goto(
            channel_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await current.wait_for_timeout(1500)

        # Collect iframes already present. Some players host the actual player
        # on a different page, so open each iframe URL directly as a fallback.
        iframe_urls = []
        try:
            iframe_urls = await current.locator("iframe").evaluate_all(
                "els => els.map(x => x.src).filter(Boolean)"
            )
        except Exception:
            pass

        for frame in current.frames:
            if frame.url.startswith("http"):
                print(f"  frame: {frame.url}")

        await collect_embedded_urls(current, capture)

        filtered_iframes = [
            u for u in dict.fromkeys(iframe_urls)
            if not any(
                blocked in u.lower()
                for blocked in (
                    "googleads.g.doubleclick.net",
                    "googlesyndication.com",
                    "google.com/recaptcha",
                    "doubleclick.net",
                )
            )
        ]

        for iframe_url in filtered_iframes[:5]:
            p = None
            try:
                p = await context.new_page()
                attach_capture(p, capture)
                await p.goto(iframe_url, wait_until="domcontentloaded", timeout=30000)
                await p.wait_for_timeout(3000)
                await collect_embedded_urls(p, capture)
                await p.close()
            except Exception:
                try:
                    if p:
                        await p.close()
                except Exception:
                    pass

        # Primary path: call the DooPlayer API used by the site's own player JS.
        await get_dooplayer_sources(
            current,
            capture,
            channel_url,
        )

        # Fallback: click the actual DooPlayer <li> source elements.
        options = current.locator("li.dooplay_player_option")
        count = await options.count()
        for i in range(min(count, 20)):
            try:
                await options.nth(i).click(timeout=3000, force=True)
                await current.wait_for_timeout(2500)
                await collect_embedded_urls(current, capture)
            except Exception:
                continue

        # Final wait for delayed/lazy player requests.
        await current.wait_for_timeout(3500)
        await collect_embedded_urls(current, capture)

        name = channel_name_from_url(channel_url)
        try:
            h1 = await current.locator("h1").first.text_content(timeout=1500)
            if h1 and h1.strip():
                name = " ".join(h1.split())
        except Exception:
            pass

        if debug:
            DEBUG.mkdir(parents=True, exist_ok=True)
            try:
                await current.screenshot(
                    path=str(DEBUG / f"{slug(channel_url)}.png"),
                    full_page=True,
                )
            except Exception:
                pass
            try:
                (DEBUG / f"{slug(channel_url)}.html").write_text(
                    await current.content(),
                    encoding="utf-8",
                )
            except Exception:
                pass

        return {
            "id": slug(channel_url),
            "name": name,
            "pageUrl": channel_url,
            "streams": sorted(capture.items.values(), key=lambda x: x["url"]),
        }

    except PlaywrightTimeoutError:
        print(f"  TIMEOUT: {channel_url}")
        return {
            "id": slug(channel_url),
            "name": channel_name_from_url(channel_url),
            "pageUrl": channel_url,
            "streams": [],
        }
    except Exception as e:
        print(f"  ERROR: {channel_url}: {e}")
        return {
            "id": slug(channel_url),
            "name": channel_name_from_url(channel_url),
            "pageUrl": channel_url,
            "streams": [],
        }
    finally:
        try:
            await current.close()
        except Exception:
            pass


async def main():
    OUT.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            locale="en-IN",
            viewport={"width": 1440, "height": 1000},
            service_workers="block",
        )

        page = await context.new_page()

        # Discover all channel pages.
        channel_pages = set()
        for category_url in CATEGORY_PAGES:
            try:
                print(f"Category: {category_url}")
                await page.goto(
                    category_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await page.wait_for_timeout(1500)

                links = await page.locator(
                    'a[href*="/live/"]'
                ).evaluate_all("els => els.map(a => a.href)")

                for url in links:
                    if "/live/" in url:
                        channel_pages.add(
                            url.split("#")[0].rstrip("/") + "/"
                        )
            except Exception as e:
                print(f"  category error: {e}")

        channels = sorted(channel_pages)

        if MAX_CHANNELS > 0:
            channels = channels[:MAX_CHANNELS]

        print(f"Channels to scan: {len(channels)}")

        results = []
        for index, channel_url in enumerate(channels, 1):
            debug = index <= DEBUG_CHANNELS
            print(f"[{index}/{len(channels)}] {channel_url}")

            item = await scan_channel(
                context,
                channel_url,
                debug=debug,
            )

            if item["streams"]:
                print(f"  FOUND {len(item['streams'])} stream(s)")
                results.append(item)
            else:
                print("  NO STREAM CAPTURED")

        await context.close()
        await browser.close()

    # Never overwrite a working playlist with an empty scrape.
    if not results:
        raise RuntimeError(
            "No HLS/DASH manifests were captured. "
            "The pages were reachable, but the player did not expose a stream "
            "to the GitHub runner. This can be caused by geo-restriction or "
            "a player/source that requires an India-based browser session."
        )

    seen = set()
    m3u = ["#EXTM3U"]

    for channel in results:
        safe_name = (
            channel["name"]
            .replace('"', "'")
            .replace(",", " - ")
        )

        for stream in channel["streams"]:
            url = stream["url"]
            if url in seen:
                continue
            seen.add(url)

            m3u.append(
                f'#EXTINF:-1 tvg-name="{safe_name}" '
                f'group-title="Tamil",{safe_name}'
            )

            headers = stream.get("headers", {})
            if headers.get("referer"):
                m3u.append(
                    f'#EXTVLCOPT:http-referrer={headers["referer"]}'
                )
            if headers.get("user-agent"):
                m3u.append(
                    f'#EXTVLCOPT:http-user-agent={headers["user-agent"]}'
                )

            m3u.append(url)
            m3u.append("")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "category": "Tamil",
        "channels": results,
        "uniqueStreams": len(seen),
    }

    (OUT / "bhoom-tamil.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "bhoom-tamil.m3u").write_text(
        "\n".join(m3u),
        encoding="utf-8",
    )

    print(
        f"Published {len(results)} channels / {len(seen)} unique streams."
    )


if __name__ == "__main__":
    asyncio.run(main())
