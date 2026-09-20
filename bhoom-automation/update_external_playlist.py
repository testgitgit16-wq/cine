#!/usr/bin/env python3
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

SOURCE_URL = os.environ.get("SOURCE_URL", "https://tinyurl.com/amaze-tamil-local-tv")
LOCAL_PLAYLIST = Path(os.environ.get("LOCAL_PLAYLIST", "output/bhoom-tamil.m3u"))
OUT = Path(os.environ.get("OUTPUT_FILE", "output/tamil-combined.m3u"))
REPORT = Path(os.environ.get("REPORT_FILE", "output/tamil-combined-report.json"))
JSON_OUT = Path(os.environ.get("JSON_OUTPUT_FILE", "output/tamil-combined.json"))
TIMEOUT = int(os.environ.get("TIMEOUT_SECONDS", "15"))
RETRIES = int(os.environ.get("RETRIES", "3"))
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "0"))
MAX_CHANNELS = max(0, int(os.environ.get("MAX_CHANNELS", "0") or "0"))

def fetch(url):
    last = None
    for n in range(RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 playlist-refresh/1.0"})
            with urlopen(req, timeout=TIMEOUT) as r:
                return r.geturl(), r.read().decode("utf-8-sig", "replace")
        except Exception as e:
            last = str(e)
            time.sleep(min(2 ** n, 5))
    raise RuntimeError(last or "download failed")

def parse(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    entries = []
    current = []
    for line in lines:
        if line.startswith("#EXTINF:"):
            if current:
                entries.append(current)
            current = [line]
        elif current:
            current.append(line)
        elif line.startswith("#EXTM3U"):
            current = [line]
    if current and current[0].startswith("#EXTINF:"):
        entries.append(current)
    return entries

def stream_url(entry):
    for line in reversed(entry):
        if not line.startswith("#") and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", line):
            return line
    return ""

def normalize_name(entry):
    return re.sub(r"\s+", " ", entry[0].rsplit(",", 1)[-1].strip()).casefold()

def key(url):
    p = urlparse(url)
    transient = {"token","sig","signature","expires","expiry","exp","session","sessionid","auth","authorization","hdnts","cookiecheck","cb"}
    kept = []
    for part in p.query.split("&") if p.query else []:
        if part.split("=", 1)[0].lower() not in transient:
            kept.append(part)
    return (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "&".join(sorted(kept)))

def score(entry, result):
    url = stream_url(entry).lower()
    value = 100 if result["status"] == "WORKING" else 20
    if ".m3u8" in url: value += 30
    if "master.m3u8" in url or "/playlist.m3u8" in url: value += 10
    if "tracks-v1a1" in url or "mono" in url: value -= 5
    return value

def validate(url):
    scheme = urlparse(url).scheme.lower()
    if scheme in {"rtmp", "rtmps"}:
        return {"status": "UNVERIFIED_RTMP", "httpStatus": None}
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 playlist-validator/1.0"})
        with urlopen(req, timeout=TIMEOUT) as r:
            status = getattr(r, "status", 200)
            head = r.read(4096)
            ctype = r.headers.get("Content-Type", "")
            if ".m3u8" in url.lower() or "mpegurl" in ctype.lower() or head.startswith(b"#EXTM3U"):
                ok = head.startswith(b"#EXTM3U")
                return {"status": "WORKING" if ok else "INVALID_M3U8", "httpStatus": status, "contentType": ctype}
            return {"status": "WORKING", "httpStatus": status, "contentType": ctype}
    except Exception as e:
        return {"status": "FAILED", "httpStatus": None, "error": str(e)[:300]}

def main():
    final_url, text = fetch(SOURCE_URL)
    external_entries = parse(text)
    local_entries = parse(LOCAL_PLAYLIST.read_text(encoding="utf-8-sig", errors="replace")) if LOCAL_PLAYLIST.exists() else []
    entries = local_entries + external_entries
    if MAX_ENTRIES:
        entries = entries[:MAX_ENTRIES]

    # Apply MAX_CHANNELS BEFORE validation. This keeps the 5-channel test fast
    # while still validating every unique stream URL belonging to those channels.
    if MAX_CHANNELS > 0:
        selected_channels = []
        selected_keys = set()
        for entry in entries:
            channel_key = normalize_name(entry)
            if not channel_key or channel_key in selected_keys:
                continue
            selected_keys.add(channel_key)
            selected_channels.append(channel_key)
            if len(selected_channels) >= MAX_CHANNELS:
                break
        entries = [entry for entry in entries if normalize_name(entry) in selected_keys]
        print(f"Selected {len(selected_channels)} channel(s) before validation: {selected_channels}")

    seen_urls = set()
    channels = {}
    report_entries = []
    for entry in entries:
        url = stream_url(entry)
        if not url:
            continue
        url_key = key(url)
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        result = validate(url)
        item = {"name": entry[0], "channel": normalize_name(entry), "url": url, **result}
        report_entries.append(item)
        if result["status"] in {"WORKING", "UNVERIFIED_RTMP"}:
            channels.setdefault(item["channel"], []).append({"entry": entry, "url": url, "result": result})

    output = ["#EXTM3U"]
    manifest = []
    channel_items = list(channels.values())
    if MAX_CHANNELS > 0:
        channel_items = channel_items[:MAX_CHANNELS]

    for sources in channel_items:
        ranked = sorted(sources, key=lambda x: score(x["entry"], x["result"]), reverse=True)
        primary = ranked[0]
        output.extend(primary["entry"])
        manifest.append({
            "channel": primary["entry"][0].rsplit(",", 1)[-1].strip(),
            "sources": [{"url": x["url"], "status": x["result"]["status"], "primary": i == 0} for i, x in enumerate(ranked)]
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(output) + "\n", encoding="utf-8")
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps({
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": manifest
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "source": SOURCE_URL,
        "resolvedUrl": final_url,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [str(LOCAL_PLAYLIST), SOURCE_URL],
        "localEntries": len(local_entries),
        "externalEntries": len(external_entries),
        "sourceEntries": len(entries),
        "uniqueStreamEntries": len(report_entries),
        "workingUniqueStreams": sum(x["status"] in {"WORKING", "UNVERIFIED_RTMP"} for x in report_entries),
        "publishedChannels": len(manifest),
        "failedEntries": sum(x["status"] == "FAILED" for x in report_entries),
        "invalidEntries": sum(x["status"] == "INVALID_M3U8" for x in report_entries),
        "unverifiedRtmp": sum(x["status"] == "UNVERIFIED_RTMP" for x in report_entries),
        "entries": report_entries,
        "channels": manifest
    }
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k not in {"entries", "channels"}}, indent=2))

if __name__ == "__main__":
    main()
