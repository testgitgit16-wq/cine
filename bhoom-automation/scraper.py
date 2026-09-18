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


async def attach_capture(context, page, capture):
    context.on("request", capture.request)
    context.on("response", capture.response)
    return page


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


async def collect_candidate_links(page):
    candidates = []
    try:
        data = await page.locator("a,button,[role='button']").evaluate_all(
            """els => els.map((el, i) => ({
                i,
                text: (el.innerText || el.textContent || '').trim(),
                href: el.href || ''
            }))"""
        )
        for item in data:
            text = (item.get("text") or "").lower()
            href = item.get("href") or ""
            if ("stream" in text or "source" in text) and href:
                if href.startswith("http"):
                    candidates.append(href)
    except Exception:
        pass
    return list(dict.fromkeys(candidates))


async def scan_channel(context, channel_url, debug=False):
    capture = Capture(channel_url)
    current = await context.new_page()
    await attach_capture(context, current, capture)

    try:
        print(f"Opening {channel_url}")
        await current.goto(
            channel_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await current.wait_for_timeout(3000)

        # Collect iframes already present.
        for frame in current.frames:
            if frame.url.startswith("http"):
                print(f"  frame: {frame.url}")

        await collect_embedded_urls(current, capture)

        # Capture source links before clicking, because some are plain external links.
        source_links = await collect_candidate_links(current)

        # Click source/stream controls. External targets/popups remain inside the same
        # browser context, so context-level request/response listeners capture them too.
        buttons = current.locator("a,button,[role='button']")
        count = await buttons.count()
        for i in range(min(count, 60)):
            try:
                label = (await buttons.nth(i).inner_text(timeout=500)).strip().lower()
                if "stream" in label or "source" in label:
                    await buttons.nth(i).click(timeout=2500, force=True)
                    await current.wait_for_timeout(1800)
                    await collect_embedded_urls(current, capture)
            except Exception:
                continue

        # Open explicit source URLs in new tabs. This catches players hosted in a
        # different page rather than an iframe.
        for link in source_links[:10]:
            if link.rstrip("/") == channel_url.rstrip("/"):
                continue
            try:
                p = await context.new_page()
                await attach_capture(context, p, capture)
                await p.goto(link, wait_until="domcontentloaded", timeout=30000)
                await p.wait_for_timeout(3500)
                await collect_embedded_urls(p, capture)
                await p.close()
            except Exception:
                try:
                    await p.close()
                except Exception:
                    pass

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
