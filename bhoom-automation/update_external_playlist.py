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
REPORT = Path(os.environ.get("REPORT_FILE", "output/amaze-tamil-local-tv-report.json"))
TIMEOUT = int(os.environ.get("TIMEOUT_SECONDS", "15"))
RETRIES = int(os.environ.get("RETRIES", "3"))
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "0"))

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

def key(url):
    p = urlparse(url)
    return (p.scheme.lower(), p.netloc.lower(), p.path, p.query)

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

    seen = set()
    output = ["#EXTM3U"]
    report_entries = []
    for entry in entries:
        url = stream_url(entry)
        if not url or key(url) in seen:
            continue
        seen.add(key(url))
        result = validate(url)
        report_entries.append({"name": entry[0], "url": url, **result})
        # Keep working HTTP streams and RTMP streams that cannot be HTTP-probed.
        if result["status"] in {"WORKING", "UNVERIFIED_RTMP"}:
            output.extend(entry)
    
    OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(output) + "\n", encoding="utf-8")
    summary = {
        "source": SOURCE_URL,
        "resolvedUrl": final_url,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [str(LOCAL_PLAYLIST), SOURCE_URL],
        "localEntries": len(local_entries),
        "externalEntries": len(external_entries),
        "sourceEntries": len(entries),
        "uniqueEntries": len(report_entries),
        "publishedEntries": sum(x["status"] in {"WORKING", "UNVERIFIED_RTMP"} for x in report_entries),
        "failedEntries": sum(x["status"] == "FAILED" for x in report_entries),
        "invalidEntries": sum(x["status"] == "INVALID_M3U8" for x in report_entries),
        "unverifiedRtmp": sum(x["status"] == "UNVERIFIED_RTMP" for x in report_entries),
        "entries": report_entries,
    }
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k != "entries"}, indent=2))

if __name__ == "__main__":
    main()
