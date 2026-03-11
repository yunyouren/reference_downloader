import unittest

from reference_tool import DomainLimiter


class TestDomainLimiter(unittest.TestCase):
    def test_backoff_wait_seconds(self):
        dl = DomainLimiter(max_per_domain=0, min_delay_ms=0)
        dl.backoff("example.com", 10.0, now=100.0)
        self.assertAlmostEqual(dl.compute_wait_seconds("example.com", now=105.0), 5.0, places=6)
        self.assertAlmostEqual(dl.compute_wait_seconds("example.com", now=111.0), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
