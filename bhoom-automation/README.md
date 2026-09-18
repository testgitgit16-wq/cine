# Bhoom Tamil automation

This folder discovers Tamil channel pages, opens their public players with Playwright, captures normal HLS (.m3u8) or DASH (.mpd) requests, records playback-safe Referer/User-Agent headers, and writes:

- output/bhoom-tamil.m3u
- output/bhoom-tamil.json

The GitHub Actions workflow runs every 6 hours and can also be started manually from Actions.

The scraper does not bypass DRM, authentication, geo controls, or other access restrictions. Use/redistribute streams only where you have the necessary rights or permission.

For India-only sources, a GitHub-hosted runner may see fewer streams than an India-based machine/VPS. In that case the same scraper can be run on an India-based self-hosted runner and push the generated output.
