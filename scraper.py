import json
import requests
from bs4 import BeautifulSoup

URL = "https://bhoomtv.org/channel/tamil/"
OUTPUT_FILE = "channels.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

def scrape_channels():
    print(f"Fetching {URL}...")
    try:
        response = requests.get(URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Failed to fetch page: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    channels = []

    for link in soup.find_all("a"):
        href = link.get("href", "")
        title = link.get_text(strip=True)
        img = link.find("img")
        img_src = img.get("src", "") if img else None

        if title and href:
            channels.append({
                "title": title,
                "url": href,
                "image": img_src
            })

    print(f"Scraped {len(channels)} channel elements.")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)

    print(f"Saved results to {OUTPUT_FILE}")

if __name__ == "__main__":
    scrape_channels()
