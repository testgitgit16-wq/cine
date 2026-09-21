import unittest

from bs4 import BeautifulSoup

from scraper import extract_category_channels, next_page_url, write_m3u


class ScraperTests(unittest.TestCase):
    def test_category_links(self):
        html = """
        <nav><a rel="next" href="/channel/tamil/page/2/">Next</a></nav>
        <a href="/live/one-tv/"><img src="/logo.png" alt="One TV"></a>
        <a href="/live/two-tv/">Two TV</a>
        """
        rows = extract_category_channels(html, "tamil")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "One TV")
        self.assertEqual(rows[1]["section"], "tamil")

    def test_next_link(self):
        soup = BeautifulSoup(
            '<a rel="next" href="/channel/tamil/page/2/">Next</a>',
            "html.parser",
        )
        self.assertEqual(
            next_page_url(soup, "https://bhoomtv.org/channel/tamil/", 1),
            "https://bhoomtv.org/channel/tamil/page/2/",
        )

    def test_m3u_header(self):
        import scraper
        scraper.OUTPUT_M3U = scraper.ROOT / "test-output.m3u"
        count = write_m3u(
            [{
                "name": "Example TV",
                "section": "tamil",
                "logo": "",
                "streams": [{
                    "url": "https://example.com/live.m3u8",
                    "headers": {
                        "referer": "https://bhoomtv.org/live/example-tv/",
                        "user-agent": "UA",
                    },
                }],
            }]
        )
        self.assertEqual(count, 1)
        text = scraper.OUTPUT_M3U.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#EXTM3U"))
        self.assertIn("#EXTINF:-1", text)
        self.assertIn("https://example.com/live.m3u8", text)
        scraper.OUTPUT_M3U.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
