import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import drift  # noqa: E402
import summarize  # noqa: E402
from fake_world import NOW, FakeGitHub, FakeLinks  # noqa: E402


class Fisher(unittest.TestCase):
    def test_known_values(self):
        # Fisher's tea-tasting table: two-sided p = 0.4857
        self.assertAlmostEqual(summarize.fisher_two_sided(3, 1, 1, 3), 0.4857, places=4)
        self.assertAlmostEqual(summarize.fisher_two_sided(10, 0, 0, 10), 1.0825e-05, places=8)
        self.assertEqual(summarize.fisher_two_sided(5, 5, 5, 5), 1.0)


class Summary(unittest.TestCase):
    def test_fixture_summary(self):
        gh = FakeGitHub()
        checker = drift.Checker(gh, FakeLinks(gh), now=NOW)
        files = [os.path.join(ROOT, "fixtures", "catalog-info.yaml")]
        entries, counts, _ = drift.run(drift.load_local(files, None), checker, {"component"})
        run = {"entries": entries, "file_counts": counts, "started_at": "a", "finished_at": "b",
               "stale_days": 365, "api_calls": 0, "link_requests": 0}
        s = summarize.summarize(run)
        self.assertEqual(s["entries"], 6)
        self.assertEqual(s["status"]["drift"], 3)
        cv = s["catalog_vs_codeowners"]
        self.assertEqual(cv["entries_with_both"], 3)  # payments-api, billing-worker, search-indexer
        self.assertEqual(cv["agree_and_resolve"], 1)
        self.assertEqual(cv["disagree"], {"different team names": 1})


if __name__ == "__main__":
    unittest.main()
