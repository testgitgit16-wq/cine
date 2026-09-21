# Bhoom Tamil Stream Collector

Clean-start production scraper for the public BHOOM TV Tamil and Tamil Local TV sections. The implementation discovers channel pages, validates publicly reachable HLS/DASH streams, and publishes IPTV-style output.

It follows real pagination when available and treats CATEGORY_PAGE_LIMIT=0 as unlimited. Temporary Cloudflare challenges are detected and stop further requests to the blocked resource; the scraper does not attempt to defeat Cloudflare, authentication, DRM, or access controls.

Outputs: output/bhoom-tamil.json, output/bhoom-tamil.m3u, output/bhoom-tamil-report.json, and state/channel_inventory.json.

Local run:

    python -m pip install -r bhoom-automation/requirements.txt
    python -m playwright install chromium
    python -u bhoom-automation/scraper.py

Environment: CATEGORY_SECTION=all|tamil|local, CATEGORY_PAGE_LIMIT=0, MAX_CHANNELS=0, VALIDATE_STREAMS=1, STABILITY_SECONDS=3, PLAYER_CAPTURE_WAIT_SECONDS=8.

Use and redistribute streams only where you have the necessary rights or permission.
