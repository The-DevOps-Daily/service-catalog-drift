"""Read-only GitHub API client with a disk cache and rate-limit handling.

The token is only ever sent to https://api.github.com. Redirects are not
followed automatically, so a redirect can never carry the token to another host.
"""

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "service-catalog-drift (+https://github.com/The-DevOps-Daily/service-catalog-drift)"
SEARCH_INTERVAL = 6.5  # code search allows 10 requests a minute


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


class Response:
    def __init__(self, url, status, headers, body, fetched_at, redirected_from=None):
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body
        self.fetched_at = fetched_at
        self.redirected_from = redirected_from

    def json(self):
        return json.loads(self.body) if self.body else None

    def to_cache(self):
        return {
            "url": self.url,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
            "fetched_at": self.fetched_at,
            "redirected_from": self.redirected_from,
        }


def find_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GitHub:
    def __init__(self, token=None, cache_dir=os.path.join(HERE, ".cache", "github"), use_cache=True, verbose=False):
        self.token = token if token is not None else find_token()
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.verbose = verbose
        self.calls = 0
        self.cache_hits = 0
        self.first_fetch = None
        self.last_fetch = None
        self._last_search = 0.0
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url, accept):
        key = hashlib.sha256(f"{accept}|{url}".encode()).hexdigest()
        return os.path.join(self.cache_dir, key[:2], key + ".json")

    def get(self, path, params=None, accept="application/vnd.github+json", follow=True):
        url = path if path.startswith("https://") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if not url.startswith(API + "/"):
            raise ValueError(f"refusing to call a non-GitHub-API URL with the client: {url}")
        resp = self._get_once(url, accept)
        self._note(resp.fetched_at)
        hops = 0
        first = url
        while follow and resp.status in (301, 302, 307, 308) and hops < 3:
            location = resp.headers.get("location", "")
            if not location.startswith(API + "/"):
                break
            hops += 1
            resp = self._get_once(location, accept)
            resp.redirected_from = first
            self._note(resp.fetched_at)
        return resp

    def _note(self, fetched_at):
        if fetched_at:
            self.first_fetch = min(self.first_fetch or fetched_at, fetched_at)
            self.last_fetch = max(self.last_fetch or fetched_at, fetched_at)

    def _get_once(self, url, accept):
        cache_file = self._cache_path(url, accept) if self.cache_dir else None
        if self.use_cache and cache_file and os.path.exists(cache_file):
            with open(cache_file) as f:
                data = json.load(f)
            self.cache_hits += 1
            return Response(**data)
        if "/search/" in url:
            wait = SEARCH_INTERVAL - (time.monotonic() - self._last_search)
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
        for attempt in range(6):
            req = urllib.request.Request(url, headers={
                "Accept": accept,
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            })
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            fetched_at = now_iso()
            try:
                with _opener.open(req, timeout=30) as r:
                    status, headers, body = r.status, dict(r.headers), r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                status, headers = e.code, dict(e.headers or {})
                raw = e.read() if e.fp else b""
                body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"GitHub API unreachable: {url}: {e}") from e
            self.calls += 1
            headers = {k.lower(): v for k, v in headers.items()}
            if status in (403, 429) and self._rate_limited(headers, body):
                self._wait_for_reset(headers, attempt)
                continue
            if status >= 500 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            keep = {k: headers[k] for k in ("location", "link", "x-ratelimit-remaining") if k in headers}
            resp = Response(url, status, keep, body, fetched_at)
            if self.verbose:
                print(f"  GET {url} -> {status}")
            # Do not cache errors that may be transient.
            if cache_file and (status < 500) and status not in (403, 429):
                os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                tmp = cache_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(resp.to_cache(), f)
                os.replace(tmp, cache_file)
            return resp
        raise RuntimeError(f"gave up after repeated rate limits or errors: {url}")

    @staticmethod
    def _rate_limited(headers, body):
        if headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers:
            return True
        return "rate limit" in body.lower()

    def _wait_for_reset(self, headers, attempt):
        if "retry-after" in headers:
            delay = int(headers["retry-after"]) + 1
        elif headers.get("x-ratelimit-reset"):
            delay = max(1, int(headers["x-ratelimit-reset"]) - int(time.time()) + 2)
        else:
            delay = 60 * (attempt + 1)
        print(f"  rate limited, waiting {delay}s")
        time.sleep(delay)

    def paginate(self, path, params=None, limit=1000):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url = API + path + "?" + urllib.parse.urlencode(params)
        items = []
        while url and len(items) < limit:
            resp = self.get(url)
            if resp.status != 200:
                return items, resp.status
            page = resp.json()
            if isinstance(page, dict) and "items" in page:
                page = page["items"]
            items.extend(page)
            url = _next_link(resp.headers.get("link", ""))
        return items[:limit], 200


def _next_link(link_header):
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) >= 2 and 'rel="next"' in section[1]:
            return section[0].strip().strip("<>")
    return None
