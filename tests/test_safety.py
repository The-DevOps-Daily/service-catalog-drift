"""The token must never leave api.github.com, and link checks send no credentials."""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ghapi  # noqa: E402
import links  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}

    def read(self, n=-1):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TokenScope(unittest.TestCase):
    def test_client_refuses_other_hosts(self):
        gh = ghapi.GitHub(token="secret", cache_dir=tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            gh.get("https://example.com/api")

    def test_redirect_off_github_is_not_followed(self):
        gh = ghapi.GitHub(token="secret", cache_dir=tempfile.mkdtemp(), use_cache=False)
        seen = []

        def fake_open(req, timeout=None):
            seen.append(req.full_url)
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/x"}, None)

        with mock.patch.object(ghapi._opener, "open", side_effect=fake_open):
            r = gh.get("/repos/a/b")
        self.assertEqual(r.status, 302)
        self.assertEqual(seen, ["https://api.github.com/repos/a/b"])

    def test_link_checker_sends_no_credentials(self):
        lc = links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False)
        captured = []

        def fake_open(req, timeout=None):
            captured.append(req)
            return FakeHTTPResponse(200)

        with mock.patch.object(links, "is_private_host", return_value=False), \
                mock.patch.object(links._opener, "open", side_effect=fake_open), \
                mock.patch.object(links, "PER_HOST_INTERVAL", 0):
            res = lc.check("https://docs.example.com/runbook")
        self.assertEqual(res["result"], links.OK)
        self.assertTrue(captured)
        for req in captured:
            names = {k.lower() for k, _ in req.header_items()}
            self.assertNotIn("authorization", names)
            self.assertNotIn("cookie", names)

    def test_private_addresses_are_not_contacted(self):
        lc = links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False)
        with mock.patch.object(links, "is_private_host", return_value=True), \
                mock.patch.object(links._opener, "open") as opener:
            res = lc.check("http://grafana.internal/d/1")
        opener.assert_not_called()
        self.assertEqual(res["result"], links.UNVERIFIABLE)

    def test_head_404_falls_back_to_get(self):
        lc = links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False)
        import urllib.error

        def fake_open(req, timeout=None):
            if req.get_method() == "HEAD":
                raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
            return FakeHTTPResponse(200)

        with mock.patch.object(links, "is_private_host", return_value=False), \
                mock.patch.object(links._opener, "open", side_effect=fake_open), \
                mock.patch.object(links, "PER_HOST_INTERVAL", 0):
            res = lc.check("https://docs.example.com/page")
        self.assertEqual(res["result"], links.OK)

    def test_status_classes(self):
        lc = links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False)
        import urllib.error
        for code, expected in ((404, links.DEAD), (410, links.DEAD), (403, links.UNVERIFIABLE),
                               (401, links.UNVERIFIABLE), (503, links.UNVERIFIABLE)):
            def fake_open(req, timeout=None, code=code):
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            with mock.patch.object(links, "is_private_host", return_value=False), \
                    mock.patch.object(links._opener, "open", side_effect=fake_open), \
                    mock.patch.object(links, "PER_HOST_INTERVAL", 0):
                self.assertEqual(lc.check(f"https://x.example/{code}")["result"], expected, code)

    def test_team_pages_on_github_com_are_not_called_dead(self):
        lc = links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False)
        res = lc.check("https://github.com/orgs/acme/teams/payments")
        self.assertEqual(res["result"], links.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()
