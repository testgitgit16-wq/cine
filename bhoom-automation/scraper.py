import asyncio
import json
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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
MANIFEST = re.compile(r"\.(m3u8|mpd)(?:\?|$)", re.I)

def slug(url):
    return urlparse(url).path.rstrip("/").split("/")[-1] or "channel"

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, locale="en-IN", viewport={"width":1440,"height":1000})
        page = await context.new_page()

        channel_pages = set()
        for category in CATEGORY_PAGES:
            try:
                await page.goto(category, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
                links = await page.locator('a[href*="/live/"]').evaluate_all("els => els.map(a => a.href)")
                channel_pages.update(x.split("#")[0].rstrip("/") + "/" for x in links if "/live/" in x)
            except Exception as e:
                print("[WARN] category:", category, e)

        results = []
        for n, channel_url in enumerate(sorted(channel_pages), 1):
            captures = {}
            current = await context.new_page()

            async def on_request(req):
                if MANIFEST.search(req.url):
                    headers = await req.all_headers()
                    captures.setdefault(req.url, {
                        "url": req.url,
                        "type": "dash" if ".mpd" in req.url.lower() else "hls",
                        "headers": {k:v for k,v in headers.items() if k.lower() in {"referer","origin","user-agent"}}
                    })

            async def on_response(resp):
                if MANIFEST.search(resp.url):
                    captures.setdefault(resp.url, {
                        "url": resp.url,
                        "type": "dash" if ".mpd" in resp.url.lower() else "hls",
                        "headers": {}
                    })
                    captures[resp.url]["status"] = resp.status

            current.on("request", on_request)
            current.on("response", on_response)

            try:
                print(f"[{n}/{len(channel_pages)}] {channel_url}")
                await current.goto(channel_url, wait_until="domcontentloaded", timeout=60000)
                await current.wait_for_timeout(4000)

                # Normal public source/player controls only.
                buttons = current.locator("a,button,[role='button']")
                count = await buttons.count()
                for i in range(min(count, 40)):
                    try:
                        label = (await buttons.nth(i).inner_text(timeout=300)).strip().lower()
                        if "stream" in label or "source" in label:
                            await buttons.nth(i).click(timeout=1500, force=True)
                            await current.wait_for_timeout(1800)
                    except Exception:
                        pass

                await current.wait_for_timeout(2500)

                name = slug(channel_url).replace("-", " ").title()
                try:
                    h1 = await current.locator("h1").first.text_content(timeout=1500)
                    if h1 and h1.strip():
                        name = " ".join(h1.split())
                except Exception:
                    pass

                for item in captures.values():
                    item["headers"].setdefault("user-agent", UA)
                    item["headers"].setdefault("referer", channel_url)

                if captures:
                    results.append({
                        "id": slug(channel_url),
                        "name": name,
                        "pageUrl": channel_url,
                        "streams": list(captures.values())
                    })
            except (PlaywrightTimeoutError, Exception) as e:
                print("[WARN] channel:", channel_url, e)
            finally:
                await current.close()

        await browser.close()

    if not results:
        raise RuntimeError("No public HLS/DASH streams captured; refusing to publish an empty playlist.")

    seen = set()
    m3u = ["#EXTM3U"]
    for ch in results:
        for s in ch["streams"]:
            if s["url"] in seen:
                continue
            seen.add(s["url"])
            name = ch["name"].replace('"', "'").replace(",", " - ")
            m3u.append(f'#EXTINF:-1 tvg-name="{name}" group-title="Tamil",{name}')
            h = s.get("headers", {})
            if h.get("referer"):
                m3u.append(f'#EXTVLCOPT:http-referrer={h["referer"]}')
            if h.get("user-agent"):
                m3u.append(f'#EXTVLCOPT:http-user-agent={h["user-agent"]}')
            m3u.append(s["url"])
            m3u.append("")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "category": "Tamil",
        "channels": results,
        "uniqueStreams": len(seen)
    }
    (OUT / "bhoom-tamil.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "bhoom-tamil.m3u").write_text("\\n".join(m3u), encoding="utf-8")
    print(f"Published {len(results)} channels / {len(seen)} unique streams.")

if __name__ == "__main__":
    asyncio.run(main())
