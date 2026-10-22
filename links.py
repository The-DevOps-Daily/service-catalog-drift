"""Check that a catalog link still answers, without sending credentials.

Links come from catalog files that anyone can write, so the checker treats them
as untrusted:

- Links to github.com files and repositories are checked through the GitHub
  API (the token goes only to api.github.com). Owner and repository names are
  validated and every path segment is quoted; anything else is reported as an
  invalid reference and not fetched.
- Every other link gets a HEAD, then a GET if HEAD is not answered with 2xx or
  3xx, with no Authorization header and no cookies. The host name is resolved
  once, every address must be a public (global) address, and the connection
  goes to that pinned address, with the original name in the Host header and
  in TLS SNI. Each redirect hop is resolved and checked the same way.
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
import urllib.parse

from ghapi import HERE, USER_AGENT, is_stale, now_iso

TIMEOUT = 10
PER_HOST_INTERVAL = 1.0
MAX_REDIRECTS = 5

# Outcome classes. Only "dead" counts as drift; the rest cannot prove anything.
OK, DEAD, UNVERIFIABLE, SKIPPED = "ok", "dead", "unverifiable", "skipped"

GITHUB_FILE = re.compile(r"^/([^/]+)/([^/]+)/(blob|tree)/([^/]+)/?(.*)$")
GITHUB_REPO = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?(?:/(.*))?/?$")
# github.com paths that are not owner/repo
GITHUB_RESERVED = {"orgs", "settings", "marketplace", "apps", "features", "enterprise", "sponsors",
                   "topics", "collections", "search", "notifications", "login", "about", "pricing"}
GITHUB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def valid_github_name(name):
    """An owner or repository name that is safe to put in an API path."""
    return bool(name) and name not in (".", "..") and bool(GITHUB_NAME.match(name))


def quote_path(path):
    """Quote each segment of a file path, or return None if a segment is empty, . or .."""
    segments = path.strip("/").split("/")
    if any(s in ("", ".", "..") for s in segments):
        return None
    return "/".join(urllib.parse.quote(s, safe="") for s in segments)


def resolve_public(host, allow_private=False):
    """Resolve once. Return (ip, None) if every address is global, else (None, reason)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None, "does not resolve in public DNS"
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            return None, "unexpected address in DNS answer"
    if not addrs:
        return None, "does not resolve in public DNS"
    if not allow_private and not all(a.is_global for a in addrs):
        return None, "resolves to a non-public address, not checked"
    addrs.sort(key=lambda a: a.version)  # IPv4 first
    return str(addrs[0]), None


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to a given IP; the Host header still carries the original name."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self.pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a given IP; SNI and certificate checks use the original name."""

    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.pinned_ip = ip
        self.tls_context = context

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)


class LinkChecker:
    def __init__(self, github, cache_dir=os.path.join(HERE, ".cache", "links"), use_cache=True,
                 allow_private=False, full_access_orgs=(), max_age_hours=24):
        self.gh = github
        self.max_age = max_age_hours * 3600
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.allow_private = allow_private
        self.full_access_orgs = {o.lower() for o in full_access_orgs}
        self._last_hit = {}
        self._lock = threading.Lock()
        self._tls = ssl.create_default_context()
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
        invalid = {"result": DEAD, "reason": "not a valid GitHub reference, not fetched", "status": None}
        m = GITHUB_FILE.match(path)
        if m:
            owner, repo, _, ref, file_path = m.groups()
            if owner.lower() in GITHUB_RESERVED:
                return None
            if not (valid_github_name(owner) and valid_github_name(repo)) or ref in (".", ".."):
                return invalid
            base = f"/repos/{owner}/{repo}"
            quoted = quote_path(file_path) if file_path.strip("/") else ""
            if quoted is None:
                return invalid
            # /commits/{ref} accepts a branch, a tag or a commit SHA
            ref_resp = self.gh.get(f"{base}/commits/{urllib.parse.quote(ref, safe='')}", {"per_page": 1})
            if ref_resp.status in (404, 422):
                repo_resp = self.gh.get(base)
                if repo_resp.status != 200:
                    return self._github_404(owner, repo, "repository not found on GitHub", ref_resp.fetched_at)
                # A ref with a slash in it (feature/x) is ambiguous in a URL: do not call it dead.
                return {"result": UNVERIFIABLE, "reason": "ref not found (it may contain a slash)",
                        "status": ref_resp.status, "checked_at": ref_resp.fetched_at}
            if ref_resp.status != 200:
                return {"result": UNVERIFIABLE, "reason": f"GitHub API answered {ref_resp.status}",
                        "status": ref_resp.status}
            if not quoted:
                return {"result": OK, "reason": "ref exists on GitHub", "status": 200,
                        "checked_at": ref_resp.fetched_at}
            r = self.gh.get(f"{base}/contents/{quoted}", {"ref": ref})
            if r.status == 200:
                return {"result": OK, "reason": "exists on GitHub", "status": 200, "checked_at": r.fetched_at}
            if r.status == 404:
                # blob/release/v1/README.md can mean ref "release" or ref "release/v1": only call
                # the file dead if no longer ref made of the next path segments exists.
                segments = file_path.strip("/").split("/")
                for i in range(1, min(3, len(segments))):
                    longer = ref + "/" + "/".join(segments[:i])
                    lr = self.gh.get(f"{base}/commits/{urllib.parse.quote(longer, safe='')}", {"per_page": 1})
                    if lr.status == 200:
                        return {"result": UNVERIFIABLE, "reason": "ambiguous ref (a longer ref with a slash exists)",
                                "status": 404, "checked_at": r.fetched_at}
                return {"result": DEAD, "reason": "file not found on GitHub at that ref", "status": 404,
                        "checked_at": r.fetched_at}
            return {"result": UNVERIFIABLE, "reason": f"GitHub API answered {r.status}", "status": r.status}
        m = GITHUB_REPO.match(path)
        if m and m.group(1).lower() not in GITHUB_RESERVED:
            owner, repo, rest = m.groups()
            if not (valid_github_name(owner) and valid_github_name(repo)):
                return invalid
            r = self.gh.get(f"/repos/{owner}/{repo}")
            if r.status == 200:
                parts = [x for x in (rest or "").split("/") if x]
                if not parts:
                    return {"result": OK, "reason": "repository exists on GitHub", "status": 200,
                            "checked_at": r.fetched_at}
                flag = {"issues": "has_issues", "wiki": "has_wiki", "discussions": "has_discussions"}.get(parts[0])
                if flag and len(parts) == 1:
                    on = (r.json() or {}).get(flag)
                    if on is False:
                        return {"result": DEAD, "reason": f"repository {parts[0]} turned off", "status": 200,
                                "checked_at": r.fetched_at}
                    if on:
                        return {"result": OK, "reason": f"repository {parts[0]} on", "status": 200,
                                "checked_at": r.fetched_at}
                # An issue, a release or a wiki page: the repository exists, the page was not checked.
                return {"result": UNVERIFIABLE, "reason": "page below the repository not checked", "status": 200,
                        "checked_at": r.fetched_at}
            if r.status == 404:
                return self._github_404(owner, repo, "repository not found on GitHub", r.fetched_at)
            return {"result": UNVERIFIABLE, "reason": f"GitHub API answered {r.status}", "status": r.status}
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1 and parts[0].lower() not in GITHUB_RESERVED:
            if not valid_github_name(parts[0]):
                return invalid
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
                cached = json.load(f)
            if not is_stale(cached.get("checked_at"), self.max_age):
                return cached
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

    def _connection(self, scheme, host, ip, port):
        if scheme == "https":
            return PinnedHTTPSConnection(host, ip, port, TIMEOUT, self._tls)
        return PinnedHTTPConnection(host, ip, port, TIMEOUT)

    def _request(self, url, method):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname.lower()
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None, "invalid URL (port)"
        ip, err = resolve_public(host, self.allow_private)
        if err:
            return None, err
        self._pace(host)
        target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        conn = self._connection(parsed.scheme, host, ip, port)
        self.requests += 1
        try:
            conn.request(method, target, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            r = conn.getresponse()
            if method == "GET":
                r.read(65536)
            return (r.status, dict(r.getheaders())), None
        except (ssl.SSLError, ssl.CertificateError) as e:
            return None, f"TLS error ({type(e).__name__})"
        except (http.client.InvalidURL, ValueError) as e:
            return None, f"invalid URL ({type(e).__name__})"
        except (socket.timeout, TimeoutError):
            return None, "timed out"
        except (http.client.HTTPException, OSError) as e:
            return None, f"connection failed ({type(e).__name__})"
        finally:
            conn.close()

    def _follow(self, url, method):
        hops = []
        for _ in range(MAX_REDIRECTS + 1):
            resp, err = self._request(url, method)
            if err:
                return None, (f"redirect target {err}" if hops else err), hops
            status, headers = resp
            if status in (301, 302, 303, 307, 308):
                loc = {k.lower(): v for k, v in headers.items()}.get("location")
                if not loc:
                    return None, f"HTTP {status} without a Location header", hops
                nxt = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlsplit(nxt).scheme not in ("http", "https"):
                    return None, "redirect to a non-http(s) address", hops
                hops.append(status)
                url = nxt  # the next hop is resolved and checked again in _request
                continue
            return status, None, hops
        return None, f"more than {MAX_REDIRECTS} redirects", hops

    def _http(self, url):
        status, err, hops = self._follow(url, "HEAD")
        if status is None or not (200 <= status < 400):
            g_status, g_err, g_hops = self._follow(url, "GET")
            if g_status is not None or status is None:
                status, err, hops = g_status, g_err, g_hops
        if status is None:
            transient = err == "timed out" or "connection failed" in err
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
