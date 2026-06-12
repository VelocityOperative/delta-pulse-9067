"""单元测试: python -m unittest discover tests"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import parse_target
from ph_scraper.export import export_any
from ph_scraper.rate_limit import RateLimiter
from ph_scraper.scraper import PHLeaderboardScraper, Product, clean_ref


class TestCleanRef(unittest.TestCase):
    def test_ref(self):
        self.assertEqual(clean_ref("https://a.com/?ref=producthunt"), "https://a.com/")

    def test_utm(self):
        self.assertEqual(
            clean_ref("https://a.com/?utm_source=ph&utm_medium=x&page=2"),
            "https://a.com/?page=2")

    def test_mixed(self):
        self.assertEqual(
            clean_ref("https://a.com/p?x=1&utm_campaign=c&ref=producthunt"),
            "https://a.com/p?x=1")

    def test_clean(self):
        self.assertEqual(clean_ref("https://a.com/p?x=1"), "https://a.com/p?x=1")


class TestParseTarget(unittest.TestCase):
    def test_daily(self):
        kw, label = parse_target("daily", "2026-06-04")
        self.assertEqual(kw, {"year": 2026, "month": 6, "day": 4})
        self.assertEqual(label, "2026-06-04")

    def test_weekly(self):
        kw, label = parse_target("weekly", "2026-W23")
        self.assertEqual(kw, {"year": 2026, "week": 23})

    def test_monthly(self):
        kw, label = parse_target("monthly", "2026-06")
        self.assertEqual(kw, {"year": 2026, "month": 6})


class TestNodeToProduct(unittest.TestCase):
    NODE = {
        "id": "123", "name": " Foo ", "slug": "foo",
        "tagline": "Bar baz", "shortenedUrl": "/r/p/123",
        "latestScore": 10, "commentsCount": 2,
        "dailyRank": "1", "weeklyRank": None, "monthlyRank": "5",
        "featuredAt": "2026-06-04T00:01:00-07:00",
        "thumbnailImageUuid": "uuid1",
        "topics": {"edges": [{"node": {"name": "AI"}}, {"node": {"name": "SaaS"}}]},
        "product": {"slug": "foo", "websiteUrl": "https://foo.com/?utm_source=ph"},
    }

    def test_fields(self):
        p = PHLeaderboardScraper._node_to_product(self.NODE)
        self.assertEqual(p.post_id, "123")
        self.assertEqual(p.name, "Foo")
        self.assertEqual(p.real_url, "https://foo.com/")
        self.assertEqual(p.keywords, "AI, SaaS")
        self.assertEqual(p.category, "AI")
        self.assertEqual(p.ph_url, "https://www.producthunt.com/products/foo")
        self.assertEqual(p.votes, 10)
        self.assertTrue(p.thumbnail.endswith("uuid1"))


class TestRateLimiter(unittest.TestCase):
    def test_interval(self):
        rl = RateLimiter(min_interval=0.05, jitter=0)
        t0 = time.monotonic()
        for _ in range(5):
            rl.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.2 - 0.05)


class TestExport(unittest.TestCase):
    def _products(self):
        return [Product(post_id="1", name="A", tagline="t", keywords="k",
                        category="c", ph_url="https://ph", real_url="https://a",
                        votes=5)]

    def test_formats(self):
        with tempfile.TemporaryDirectory() as d:
            for ext in (".xlsx", ".csv", ".json"):
                path = str(Path(d) / ("out" + ext))
                export_any(self._products(), path)
                self.assertTrue(Path(path).stat().st_size > 0)


class TestStore(unittest.TestCase):
    def test_upsert_dedup(self):
        from ph_scraper.store import Store
        with tempfile.TemporaryDirectory() as d:
            st = Store(str(Path(d) / "t.db"))
            p = Product(post_id="1", name="A", votes=1)
            st.upsert_products([p])
            p2 = Product(post_id="1", name="A", votes=9)
            st.upsert_products([p2, Product(post_id="2", name="B")])
            self.assertEqual(st.count(), 2)
            v = st.conn.execute(
                "SELECT votes FROM products WHERE post_id='1'").fetchone()[0]
            self.assertEqual(v, 9)
            st.close()


if __name__ == "__main__":
    unittest.main()
