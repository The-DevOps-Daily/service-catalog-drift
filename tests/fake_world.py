"""A recorded-style GitHub and web for the fixture catalog. No network."""

import json
import urllib.parse
from datetime import datetime, timezone

from ghapi import API, Response
from links import DEAD, OK, UNVERIFIABLE, LinkChecker

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
ORG = "acme-fixture"


def key(path, params=None):
    return path + ("?" + urllib.parse.urlencode(params) if params else "")


def repo(name, archived=False, full_name=None):
    return {"full_name": full_name or f"{ORG}/{name}", "archived": archived,
            "default_branch": "main", "pushed_at": "2026-10-01T00:00:00Z", "private": False}


def commit(date, login="dev1", bot=False):
    return {"author": {"login": login + ("[bot]" if bot else ""), "type": "Bot" if bot else "User"},
            "commit": {"author": {"name": login}, "committer": {"date": date}}}


def content(text):
    import base64
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def unknown_owner(handle, line, source):
    # The two message formats GitHub uses (checked 2026-10-22)
    if "/" in handle:
        what = f"the team {handle} exists, is publicly visible, and has write access to the repository"
    else:
        what = f"{handle} exists and has write access to the repository"
    return {"kind": "Unknown owner", "line": line, "source": source,
            "message": f"Unknown owner on line {line}: make sure {what}\n\n  {source}\n"}


WORLD = {
    key(f"/orgs/{ORG}/teams", {"per_page": 1}): (404, {"message": "Not Found"}),

    key(f"/repos/{ORG}/payments-api"): (200, repo("payments-api")),
    key(f"/repos/{ORG}/payments-api/commits", {"sha": "main", "per_page": 30}): (200, [commit("2026-10-01T10:00:00Z")]),
    key(f"/repos/{ORG}/payments-api/contents/.github/CODEOWNERS", {"ref": "main"}):
        (200, content("* @acme-fixture/payments\n/docs/ @acme-fixture/payments @acme-fixture/sre\n")),
    key(f"/repos/{ORG}/payments-api/codeowners/errors", {"ref": "main"}): (200, {"errors": []}),
    key(f"/repos/{ORG}/payments-api/contents/docs/runbook.md", {"ref": "main"}): (200, content("# Runbook")),

    key(f"/repos/{ORG}/billing-worker"): (200, repo("billing-worker")),
    key(f"/repos/{ORG}/billing-worker/commits", {"sha": "main", "per_page": 30}): (200, [commit("2026-09-15T10:00:00Z")]),
    key(f"/repos/{ORG}/billing-worker/contents/.github/CODEOWNERS", {"ref": "main"}):
        (200, content("* @acme-fixture/billing @acme-fixture/sre\n")),
    key(f"/repos/{ORG}/billing-worker/codeowners/errors", {"ref": "main"}):
        (200, {"errors": [unknown_owner("@acme-fixture/billing", 1, "* @acme-fixture/billing @acme-fixture/sre")]}),

    key(f"/repos/{ORG}/legacy-reports"): (200, repo("legacy-reports", archived=True)),
    key(f"/repos/{ORG}/legacy-reports/commits", {"sha": "main", "per_page": 30}): (200, [commit("2024-02-01T10:00:00Z")]),
    key("/users/former-engineer"): (404, {"message": "Not Found"}),

    key(f"/repos/{ORG}/search-indexer"): (200, repo("search", full_name=f"{ORG}/search")),
    key(f"/repos/{ORG}/search"): (200, repo("search")),
    key(f"/repos/{ORG}/search/commits", {"sha": "main", "per_page": 30}): (200, [commit("2026-10-10T10:00:00Z")]),
    key(f"/repos/{ORG}/search/contents/.github/CODEOWNERS", {"ref": "main"}): (200, content("* @acme-fixture/discovery\n")),
    key(f"/repos/{ORG}/search/codeowners/errors", {"ref": "main"}): (200, {"errors": []}),
    key(f"/repos/{ORG}/search/contents/RUNBOOK.md", {"ref": "main"}): (404, {"message": "Not Found"}),
    key("/search/code", {"q": f'"{ORG}/search" filename:CODEOWNERS org:{ORG}', "per_page": 20}): (200, {"items": []}),

    key(f"/repos/{ORG}/feature-flags"): (200, repo("feature-flags")),
    key(f"/repos/{ORG}/feature-flags/commits", {"sha": "main", "per_page": 30}):
        (200, [commit("2026-10-20T10:00:00Z", "renovate", bot=True), commit("2025-05-01T10:00:00Z")]),
    key("/search/code", {"q": f'"{ORG}/platform" filename:CODEOWNERS org:{ORG}', "per_page": 20}):
        (200, {"items": [{"path": ".github/CODEOWNERS", "repository": {"full_name": f"{ORG}/infra", "fork": False}}]}),
    key(f"/repos/{ORG}/infra"): (200, repo("infra")),
    key(f"/repos/{ORG}/infra/commits", {"sha": "main", "per_page": 30}): (200, [commit("2026-10-01T10:00:00Z")]),
    key(f"/repos/{ORG}/infra/contents/.github/CODEOWNERS", {"ref": "main"}): (200, content("* @acme-fixture/platform\n")),
    key(f"/repos/{ORG}/infra/codeowners/errors", {"ref": "main"}): (200, {"errors": []}),

    key(f"/repos/{ORG}/old-cli"): (200, repo("old-cli", archived=True)),
    key(f"/repos/{ORG}/old-cli/commits", {"sha": "main", "per_page": 30}): (200, [commit("2023-01-01T10:00:00Z")]),
}

WEB = {
    "https://grafana.acme-fixture.example/d/payments": {"result": UNVERIFIABLE, "reason": "does not resolve in public DNS"},
    "https://wiki.acme-fixture.example/search": {"result": UNVERIFIABLE, "reason": "HTTP 401: needs login or blocks automated requests", "status": 401},
}


class FakeGitHub:
    """Answers from WORLD; anything not in it is a 404, and every call is logged."""

    def __init__(self, world=None):
        self.world = dict(WORLD if world is None else world)
        self.log = []
        self.token = "fake"
        self.calls = 0
        self.cache_hits = 0

    def get(self, path, params=None, accept=None, follow=True):
        if path.startswith(API):
            path = path[len(API):]
        k = key(path, params)
        self.log.append(k)
        self.calls += 1
        status, body = self.world.get(k, (404, {"message": "Not Found"}))
        return Response(API + k, status, {}, json.dumps(body), "2026-10-22T00:00:00+00:00")


class FakeLinks(LinkChecker):
    def __init__(self, gh, web=None):
        super().__init__(gh, cache_dir=_tmpdir())
        self.web = WEB if web is None else web

    def _cached_http(self, url):
        return dict(self.web.get(url, {"result": DEAD, "reason": "HTTP 404", "status": 404}))


def _tmpdir():
    import tempfile
    return tempfile.mkdtemp(prefix="links-cache-")


__all__ = ["FakeGitHub", "FakeLinks", "NOW", "OK", "DEAD"]
