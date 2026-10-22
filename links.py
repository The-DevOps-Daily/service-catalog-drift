"""Check that a catalog link still answers, without sending credentials.

Links to github.com files and repositories are checked through the GitHub API
(the token goes only to api.github.com). Every other link gets a HEAD, then a
GET if HEAD is not answered with 2xx or 3xx, with no Authorization header and
no cookies. Private and loopback addresses are skipped unless allowed.
"""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ghapi import HERE, USER_AGENT, now_iso

TIMEOUT = 10
PER_HOST_INTERVAL = 1.0
MAX_REDIRECTS = 5

# Outcome classes. Only "dead" counts as drift; the rest cannot prove anything.
OK, DEAD, UNVERIFIABLE, SKIPPED = "ok", "dead", "unverifiable", "skipped"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))

GITHUB_FILE = re.compile(r"^/([^/]+)/([^/]+)/(blob|tree)/([^/]+)/?(.*)$")
GITHUB_REPO = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?(?:/(.*))?/?$")
# github.com paths that are not owner/repo
GITHUB_RESERVED = {"orgs", "settings", "marketplace", "apps", "features", "enterprise", "sponsors",
                   "topics", "collections", "search", "notifications", "login", "about", "pricing"}


def is_private_host(host):
    """True if every address the host resolves to is private, loopback or link-local."""
    infos = socket.getaddrinfo(host, None)
    addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast for a in addrs)


class LinkChecker:
    def __init__(self, github, cache_dir=os.path.join(HERE, ".cache", "links"), use_cache=True,
                 allow_private=False, full_access_orgs=()):
        self.gh = github
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.allow_private = allow_private
        self.full_access_orgs = {o.lower() for o in full_access_orgs}
        self._last_hit = {}
        self._lock = threading.Lock()
        self.requests = 0
        os.makedirs(cache_dir, exist_ok=True)

    def check(self, url):
        url = (url or "").strip()
        # A browser percent-encodes spaces and other unsafe characters; do the same.
        url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except ValueError as e:
            return {"result": DEAD, "reason": f"not a valid URL ({e})"}
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return {"result": SKIPPED, "reason": f"not an http(s) link ({parsed.scheme or 'no scheme'})"}
        host = parsed.hostname.lower()
        if host in ("github.com", "www.github.com"):
            gh_result = self._check_github(parsed)
            if gh_result is not None:
                return gh_result
        return self._cached_http(url)

    # -------------------------------------------------------------- github.com

    def _check_github(self, parsed):
        path = urllib.parse.unquote(parsed.path)
        m = GITHUB_FILE.match(path)
        if m:
            owner, repo, _, ref, file_path = m.groups()
            if owner.lower() in GITHUB_RESERVED:
                return None
            api = f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(file_path.rstrip('/'))}"
            r = self.gh.get(api, {"ref": ref}) if file_path else self.gh.get(f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(ref, safe='')}")
            if r.status == 200:
                return {"result": OK, "reason": "exists on GitHub", "status": 200, "checked_at": r.fetched_at}
            if r.status == 404:
                return self._github_404(owner, repo, "file or branch not found on GitHub", r.fetched_at)
            return {"result": UNVERIFIABLE, "reason": f"GitHub API answered {r.status}", "status": r.status}
        m = GITHUB_REPO.match(path)
        if m and m.group(1).lower() not in GITHUB_RESERVED:
            owner, repo, rest = m.groups()
            r = self.gh.get(f"/repos/{owner}/{repo}")
            if r.status == 200:
                if rest and rest.split("/")[0] == "wiki" and not (r.json() or {}).get("has_wiki", True):
                    return {"result": DEAD, "reason": "repository wiki is turned off", "status": 200,
                            "checked_at": r.fetched_at}
                return {"result": OK, "reason": "repository exists on GitHub", "status": 200,
                        "checked_at": r.fetched_at}
            if r.status == 404:
                return self._github_404(owner, repo, "repository not found on GitHub", r.fetched_at)
            return {"result": UNVERIFIABLE, "reason": f"GitHub API answered {r.status}", "status": r.status}
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1 and parts[0].lower() not in GITHUB_RESERVED:
            r = self.gh.get(f"/users/{parts[0]}")
            if r.status in (200, 404):
                return {"result": OK if r.status == 200 else DEAD,
                        "reason": "account exists on GitHub" if r.status == 200 else "account not found on GitHub",
                        "status": r.status, "checked_at": r.fetched_at}
        # Team pages and other github.com pages answer 404 to anyone not signed in.
        return {"result": UNVERIFIABLE, "reason": "github.com page that needs a signed-in user", "status": None}

    def _github_404(self, owner, repo, reason, fetched_at):
        repo_resp = self.gh.get(f"/repos/{owner}/{repo}")
        if repo_resp.status == 200:
            return {"result": DEAD, "reason": reason, "status": 404, "checked_at": fetched_at}
        if owner.lower() in self.full_access_orgs:
            return {"result": DEAD, "reason": "repository not found on GitHub", "status": 404,
                    "checked_at": fetched_at}
        return {"result": UNVERIFIABLE, "reason": "repository not visible (deleted, or private)", "status": 404,
                "checked_at": fetched_at}

    # -------------------------------------------------------------------- http

    def _cached_http(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = os.path.join(self.cache_dir, key[:2], key + ".json")
        if self.use_cache and os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        result = self._http(url)
        result["checked_at"] = result.get("checked_at") or now_iso()
        if result.get("transient"):
            return result
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(result, f)
        os.replace(path + ".tmp", path)
        return result

    def _pace(self, host):
        with self._lock:
            wait = PER_HOST_INTERVAL - (time.monotonic() - self._last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def _request(self, url, method):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname.lower()
        try:
            private = is_private_host(host)
        except (socket.gaierror, UnicodeError):
            return None, "does not resolve in public DNS"
        if private and not self.allow_private:
            return None, "private or internal address, not checked"
        self._pace(host)
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        self.requests += 1
        try:
            with _opener.open(req, timeout=TIMEOUT) as r:
                if method == "GET":
                    r.read(65536)
                return (r.status, dict(r.headers)), None
        except urllib.error.HTTPError as e:
            return (e.code, dict(e.headers or {})), None
        except ssl.SSLError as e:
            return None, f"TLS error ({type(e).__name__})"
        except (http.client.InvalidURL, ValueError) as e:
            return None, f"invalid URL ({type(e).__name__})"
        except http.client.HTTPException as e:
            return None, f"connection failed ({type(e).__name__})"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
                return None, "TLS error"
            if isinstance(reason, socket.gaierror):
                return None, "does not resolve in public DNS"
            if "timed out" in str(reason).lower():
                return None, "timed out"
            return None, f"connection failed ({type(reason).__name__})"

    def _follow(self, url, method):
        hops = []
        for _ in range(MAX_REDIRECTS + 1):
            resp, err = self._request(url, method)
            if err:
                return None, err, hops
            status, headers = resp
            if status in (301, 302, 303, 307, 308):
                loc = {k.lower(): v for k, v in headers.items()}.get("location")
                if not loc:
                    return status, None, hops
                nxt = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlsplit(nxt).scheme not in ("http", "https"):
                    return status, None, hops
                hops.append(status)
                url = nxt
                continue
            return status, None, hops
        return 399, None, hops  # still redirecting after MAX_REDIRECTS: count as 3xx

    def _http(self, url):
        status, err, hops = self._follow(url, "HEAD")
        if status is None or not (200 <= status < 400):
            g_status, g_err, g_hops = self._follow(url, "GET")
            if g_status is not None or status is None:
                status, err, hops = g_status, g_err, g_hops
        if status is None:
            transient = err in ("timed out",) or err.startswith("connection failed")
            return {"result": UNVERIFIABLE, "reason": err, "transient": transient}
        out = {"status": status, "redirects": len(hops)}
        if 200 <= status < 400:
            out.update(result=OK, reason=f"HTTP {status}" + (" after redirect" if hops else ""))
        elif status in (404, 410):
            out.update(result=DEAD, reason=f"HTTP {status}")
        elif status in (401, 403, 407, 429, 999):
            out.update(result=UNVERIFIABLE, reason=f"HTTP {status}: needs login or blocks automated requests")
        else:
            out.update(result=UNVERIFIABLE, reason=f"HTTP {status}")
            out["transient"] = status >= 500
        return out
