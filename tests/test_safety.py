"""Catalog files are written by strangers. These tests pin down what the checker
will and will not do with the values in them.

- The GitHub token never leaves api.github.com.
- Link checks send no credentials, only reach public addresses, connect to the
  address they checked (no second DNS lookup), and re-check every redirect.
- Owner, repository and path values are validated and quoted before they go
  into a GitHub API path.
"""

import io
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import drift  # noqa: E402
import ghapi  # noqa: E402
import links  # noqa: E402
from fake_world import NOW, FakeGitHub  # noqa: E402

PUBLIC = "93.184.215.14"


class FakeSocket:
    def __init__(self, response):
        self.sent = b""
        self.response = response

    def sendall(self, data):
        self.sent += data

    def makefile(self, *a, **k):
        return io.BytesIO(self.response)

    def close(self):
        pass

    def settimeout(self, t):
        pass

    def setsockopt(self, *a):
        pass


def reply(status, headers=""):
    return f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\n{headers}\r\n".encode()


class Net:
    """Patches DNS and sockets. `answers` maps host -> IPs; `responses` maps IP -> raw reply."""

    def __init__(self, answers, responses):
        self.answers = answers
        self.responses = responses
        self.lookups = []
        self.connects = []
        self.sockets = []

    def getaddrinfo(self, host, port, *a, **k):
        self.lookups.append(host)
        ips = self.answers.get(host)
        if not ips:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
                for ip in ips]

    def create_connection(self, address, timeout=None, *a, **k):
        self.connects.append(address)
        s = FakeSocket(self.responses[address[0]])
        self.sockets.append(s)
        return s

    def __enter__(self):
        self._patches = [mock.patch.object(links.socket, "getaddrinfo", self.getaddrinfo),
                         mock.patch.object(links.socket, "create_connection", self.create_connection),
                         mock.patch.object(links, "PER_HOST_INTERVAL", 0)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()


def checker(**kw):
    return links.LinkChecker(github=None, cache_dir=tempfile.mkdtemp(), use_cache=False, **kw)


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
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/x"}, None)

        with mock.patch.object(ghapi._opener, "open", side_effect=fake_open):
            r = gh.get("/repos/a/b")
        self.assertEqual(r.status, 302)
        self.assertEqual(seen, ["https://api.github.com/repos/a/b"])


class LinkRequests(unittest.TestCase):
    def test_pinned_address_host_header_no_credentials(self):
        with Net({"docs.example.com": [PUBLIC]}, {PUBLIC: reply(200)}) as net:
            res = checker().check("http://docs.example.com/runbook?x=1")
        self.assertEqual(res["result"], links.OK)
        self.assertEqual(net.connects, [(PUBLIC, 80)])
        self.assertEqual(net.lookups, ["docs.example.com"])  # one lookup, then the pinned address
        sent = net.sockets[0].sent
        self.assertIn(b"HEAD /runbook?x=1 HTTP/1.1", sent)
        self.assertIn(b"Host: docs.example.com", sent)
        self.assertNotIn(b"authorization", sent.lower())
        self.assertNotIn(b"cookie", sent.lower())

    def test_https_uses_original_name_for_sni(self):
        lc = checker()
        names = []

        def wrap(sock, server_hostname=None):
            names.append(server_hostname)
            return sock

        lc._tls = mock.Mock(wrap_socket=wrap)
        with Net({"docs.example.com": [PUBLIC]}, {PUBLIC: reply(200)}) as net:
            res = lc.check("https://docs.example.com/")
        self.assertEqual(res["result"], links.OK)
        self.assertEqual(net.connects, [(PUBLIC, 443)])
        self.assertEqual(names, ["docs.example.com"])

    def test_non_public_addresses_are_not_contacted(self):
        for ips in (["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["100.64.0.1"], ["::1"],
                    ["fd00::1"], [PUBLIC, "10.0.0.1"]):
            with Net({"x.example": ips}, {}) as net:
                res = checker().check("http://x.example/")
            self.assertEqual(net.connects, [], ips)
            self.assertEqual(res["result"], links.UNVERIFIABLE, ips)
            self.assertIn("non-public", res["reason"])

    def test_allow_private_is_explicit(self):
        with Net({"grafana.internal": ["10.0.0.5"]}, {"10.0.0.5": reply(200)}) as net:
            res = checker(allow_private=True).check("http://grafana.internal/d/1")
        self.assertEqual(res["result"], links.OK)
        self.assertEqual(net.connects, [("10.0.0.5", 80)])

    def test_redirect_hops_are_checked_again(self):
        answers = {"short.example": [PUBLIC], "metadata.internal": ["169.254.169.254"]}
        with Net(answers, {PUBLIC: reply(302, "Location: http://metadata.internal/latest\r\n")}) as net:
            res = checker().check("http://short.example/go")
        self.assertEqual({a for a, _ in net.connects}, {PUBLIC})
        self.assertEqual(res["result"], links.UNVERIFIABLE)
        self.assertIn("redirect target", res["reason"])

    def test_head_404_falls_back_to_get(self):
        lc = checker()
        calls = []

        def request(url, method):
            calls.append(method)
            return ((404, {}) if method == "HEAD" else (200, {})), None

        with mock.patch.object(lc, "_request", side_effect=request):
            res = lc.check("https://docs.example.com/page")
        self.assertEqual(res["result"], links.OK)
        self.assertEqual(calls, ["HEAD", "GET"])

    def test_status_classes(self):
        for code, expected in ((404, links.DEAD), (410, links.DEAD), (403, links.UNVERIFIABLE),
                               (401, links.UNVERIFIABLE), (503, links.UNVERIFIABLE), (301, links.OK)):
            lc = checker()
            with mock.patch.object(lc, "_request", return_value=((code, {}), None)):
                self.assertEqual(lc.check(f"https://x.example/{code}")["result"], expected, code)

    def test_team_pages_on_github_com_are_not_called_dead(self):
        res = checker().check("https://github.com/orgs/acme/teams/payments")
        self.assertEqual(res["result"], links.UNVERIFIABLE)


class GitHubPaths(unittest.TestCase):
    def lc(self):
        gh = FakeGitHub(world={})
        return links.LinkChecker(gh, cache_dir=tempfile.mkdtemp(), use_cache=False), gh

    def test_dot_dot_is_not_fetched(self):
        for url in ("https://github.com/acme/repo/blob/main/..%2F..%2F..%2Fusers%2Fx",
                    "https://github.com/acme/..%2F..%2Fuser",
                    "https://github.com/acme/repo/blob/main/docs/../../x",
                    "https://github.com/ac%20me/repo",
                    "https://github.com/acme/re%3Fpo/blob/main/x"):
            lc, gh = self.lc()
            res = lc.check(url)
            self.assertEqual(gh.log, [], url)
            self.assertIn("not a valid GitHub reference", res["reason"], url)

    def test_segments_are_quoted(self):
        lc, gh = self.lc()
        lc.check("https://github.com/acme/repo/blob/main/docs/a%3Fb%23c.md")
        self.assertEqual(gh.log[0], "/repos/acme/repo/contents/docs/a%3Fb%23c.md?ref=main")

    def test_slugs_from_catalog_files_are_validated(self):
        for bad in ("../x", "a/..", "a/b/c", "a/.", "a b/c", "a/b?x=1"):
            self.assertEqual(drift.repo_slug({"github.com/project-slug": bad}, None), (None, None), bad)
        self.assertEqual(drift.repo_slug({"backstage.io/source-location": "url:https://github.com/../x/"}, None),
                         (None, None))

    def test_team_names_are_validated(self):
        gh = FakeGitHub(world={})
        c = drift.Checker(gh, None, now=NOW)
        res = c.check_owner("group:../../user", "acme", None)
        self.assertEqual(res["status"], "unverifiable")
        self.assertEqual(gh.log, [])


class LinksAreOptIn(unittest.TestCase):
    def test_no_fetch_without_check_links(self):
        gh = FakeGitHub(world={})
        c = drift.Checker(gh, None, now=NOW)
        res, details = c.check_links([{"url": "https://github.com/acme/x/blob/main/RUNBOOK.md", "kind": "runbook"}])
        self.assertEqual(res["status"], "n/a")
        self.assertEqual(details[0]["result"], "not checked")
        self.assertEqual(gh.log, [])


if __name__ == "__main__":
    unittest.main()
