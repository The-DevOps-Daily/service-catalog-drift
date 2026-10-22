#!/usr/bin/env python3
"""Check a Backstage service catalog against GitHub and report drift.

  python3 drift.py catalog-info.yaml [more files]   # local catalog files
  python3 drift.py --sample sample/sample-<date>.json --redact --json results/run.json

Every check is read-only. Each one reports ok, drift, warn, unverifiable or
n/a, with a reason. Exit code: 0 no drift, 1 drift found (or warnings with
--fail-on warn), 2 usage or API error.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import catalog
from ghapi import GitHub, now_iso
from links import DEAD, OK, SKIPPED, UNVERIFIABLE, LinkChecker

LIFECYCLES = {"experimental", "production", "deprecated"}
CHECKS = ("owner", "repo", "codeowners", "links", "lifecycle")
UNKNOWN_OWNER = re.compile(r"make sure (?:the team |the user )?(@[^\s,]+|[^\s,@]+@[^\s,]+) exists")
BOT_NAMES = re.compile(r"(\[bot\]$|^dependabot|^renovate|^github-actions|^snyk-bot|^mergify)", re.I)


def days_since(iso, now):
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (now - t).days


def result(status, reason):
    return {"status": status, "reason": reason}


class Redactor:
    """Keeps people out of the output.

    User logins and emails become placeholders. Owner and team names are replaced
    by a keyed hash, because a bare owner name is sometimes a person's login: the
    key is random per run and never written, so equal names still look equal
    within one result file but cannot be looked up.
    """

    def __init__(self, enabled):
        self.enabled = enabled
        self._key = os.urandom(16)

    def _h(self, name):
        return hmac.new(self._key, name.lower().encode(), hashlib.sha256).hexdigest()[:6]

    def owner(self, ref):
        if not self.enabled or not ref:
            return ref
        if "@" in ref:
            return "<email>"
        parsed = catalog.parse_ref(ref)
        if parsed and parsed["kind"] == "user":
            return "user:<redacted>"
        if parsed:
            return f"{parsed['kind']}:<{self._h(parsed['name'])}>"
        return "<redacted>"

    def team(self, org, name):
        return f"@{org}/{name}" if not self.enabled else f"@{org}/<{self._h(name)}>"

    def codeowner(self, handle):
        if not self.enabled:
            return handle
        if "@" in handle and not handle.startswith("@"):
            return "<email>"
        if handle.startswith("@") and "/" in handle:
            org, name = handle[1:].split("/", 1)
            return self.team(org, name)
        return "@<user>"

    def url(self, url):
        if not self.enabled:
            return url
        p = urllib.parse.urlsplit(url)
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        return f"{p.scheme}://{p.hostname or ''}/<path {digest}>"


class Checker:
    def __init__(self, gh, links, stale_days=365, redact=False, now=None, org_search=True,
                 users_are_logins=False):
        self.gh = gh
        self.users_are_logins = users_are_logins
        self.org_search = org_search
        self._team_search = {}
        self.links = links
        self.stale_days = stale_days
        self.r = Redactor(redact)
        self.now = now or datetime.now(timezone.utc)
        self._org_access = {}
        self._repo_cache = {}
        self._codeowners_cache = {}

    # ------------------------------------------------------------- helpers

    def full_access(self, org):
        """Can this token see the org's teams and private membership?"""
        key = org.lower()
        if key not in self._org_access:
            r = self.gh.get(f"/orgs/{org}/teams", {"per_page": 1})
            self._org_access[key] = r.status == 200
        return self._org_access[key]

    def repo_info(self, slug):
        key = slug.lower()
        if key in self._repo_cache:
            return self._repo_cache[key]
        r = self.gh.get(f"/repos/{slug}")
        info = {"status": r.status, "fetched_at": r.fetched_at}
        if r.status == 200:
            j = r.json()
            info.update(full_name=j["full_name"], archived=j.get("archived", False),
                        default_branch=j.get("default_branch"), pushed_at=j.get("pushed_at"),
                        private=j.get("private", False))
            info["moved_from"] = slug if j["full_name"].lower() != slug.lower() else None
            c = self.gh.get(f"/repos/{j['full_name']}/commits", {"sha": j.get("default_branch"), "per_page": 30})
            if c.status == 200 and c.json():
                commits = c.json()
                info["last_commit"] = commits[0]["commit"]["committer"]["date"]
                human = next((x for x in commits if not _is_bot(x)), None)
                info["last_human_commit"] = human["commit"]["committer"]["date"] if human else None
                info["human_seen_in_last_30"] = human is not None
            elif c.status == 409:
                info["empty"] = True
        self._repo_cache[key] = info
        return info

    def codeowners(self, slug, branch):
        key = slug.lower()
        if key in self._codeowners_cache:
            return self._codeowners_cache[key]
        found = None
        for path in catalog.CODEOWNERS_PATHS:
            r = self.gh.get(f"/repos/{slug}/contents/{path}", {"ref": branch} if branch else None)
            if r.status == 200 and isinstance(r.json(), dict) and r.json().get("content") is not None:
                text = base64.b64decode(r.json()["content"]).decode("utf-8", "replace")
                found = {"path": path, "rules": catalog.parse_codeowners(text)}
                break
        if found:
            e = self.gh.get(f"/repos/{slug}/codeowners/errors", {"ref": branch} if branch else None)
            unknown = set()
            if e.status == 200:
                for err in (e.json() or {}).get("errors", []):
                    if "unknown owner" in (err.get("kind", "") + err.get("message", "")).lower():
                        # "make sure @user exists and ..." or "make sure the team @org/team exists, ..."
                        named = re.findall(UNKNOWN_OWNER, err.get("message", ""))
                        unknown.update(h.lower() for h in named)
                        if not named:
                            unknown.update(_owners_on_line(err.get("source", "")))
            found["unknown"] = unknown
            found["errors_status"] = e.status
        self._codeowners_cache[key] = found
        return found

    # -------------------------------------------------------------- checks

    def check_owner(self, owner_ref, org, co):
        if not owner_ref:
            return result("drift", "no spec.owner")
        ref = catalog.parse_ref(owner_ref)
        shown = self.r.owner(owner_ref)
        if ref["kind"] not in ("group", "user"):
            return result("warn", f"owner {shown} is not a group or a user")
        name = ref["name"]
        full = self.full_access(org)
        if ref["kind"] == "user":
            if not catalog.is_github_login(name):
                return result("unverifiable", f"{shown} is not a GitHub login (users likely come from another directory)")
            u = self.gh.get(f"/users/{name}")
            if u.status == 404:
                # An ID from an identity provider can look exactly like a GitHub login
                # (abc12345), so a 404 only proves drift when the org says users are logins.
                if self.users_are_logins:
                    return result("drift", f"{shown}: no GitHub account with this login (deleted or renamed)")
                return result("warn", f"{shown}: no GitHub account with this login "
                                      "(deleted, renamed, or a user from another directory)")
            if u.status != 200:
                return result("unverifiable", f"{shown}: GitHub answered {u.status}")
            if full:
                m = self.gh.get(f"/orgs/{org}/members/{name}", follow=False)
                if m.status == 204:
                    return result("ok", f"{shown} is a member of {org}")
                return result("drift", f"{shown} is not a member of {org}")
            m = self.gh.get(f"/orgs/{org}/public_members/{name}")
            if m.status == 204:
                return result("ok", f"{shown} is a public member of {org}")
            return result("unverifiable", f"{shown} is not a public member of {org} (membership may be private)")
        # group
        team = self.r.team(org, name)
        if full:
            t = self.gh.get(f"/orgs/{org}/teams/{urllib.parse.quote(name)}")
            if t.status == 404:
                return result("drift", f"no team {team}")
            if t.status != 200:
                return result("unverifiable", f"team lookup answered {t.status}")
            mem = self.gh.get(f"/orgs/{org}/teams/{urllib.parse.quote(name)}/members", {"per_page": 1})
            if mem.status == 200 and mem.json() == []:
                return result("drift", f"team {team} has no members")
            return result("ok", f"team {team} exists")
        handle = f"@{org}/{name}".lower()
        verdict = _team_verdict(co, handle, team, "this repository")
        if verdict is None and self.org_search:
            verdict = self.team_via_org_codeowners(org, name)
        if verdict is not None:
            return verdict
        return result("unverifiable", f"team {team}: not named in any CODEOWNERS we could read, "
                                      "and teams are only visible to org members")

    def team_via_org_codeowners(self, org, name):
        """Find the team in another CODEOWNERS file in the org and ask GitHub whether it resolves."""
        key = (org.lower(), name.lower())
        if key in self._team_search:
            return self._team_search[key]
        handle = f"@{org}/{name}".lower()
        r = self.gh.get("/search/code", {"q": f'"{org}/{name}" filename:CODEOWNERS org:{org}', "per_page": 20})
        verdict = None
        if r.status == 200:
            unknown_in = []
            for it in (r.json() or {}).get("items", [])[:8]:
                repo, path = it["repository"]["full_name"], it["path"]
                if path not in catalog.CODEOWNERS_PATHS or it["repository"].get("fork"):
                    continue
                info = self.repo_info(repo)
                if info.get("status") != 200 or info.get("archived"):
                    continue
                co = self.codeowners(info["full_name"], info.get("default_branch"))
                v = _team_verdict(co, handle, self.r.team(org, name), info["full_name"]) \
                    if co and co["path"] == path else None
                if v and v["status"] == "ok":
                    verdict = v
                    break
                if v and v["status"] == "drift":
                    unknown_in.append(v)
            if verdict is None and unknown_in:
                verdict = unknown_in[0]
        self._team_search[key] = verdict
        return verdict

    def check_repo(self, slug, lifecycle):
        info = self.repo_info(slug)
        if info["status"] == 404:
            owner = slug.split("/")[0]
            if self.full_access(owner):
                return result("drift", f"{slug} not found"), info
            return result("unverifiable", f"{slug} not visible (deleted, or private)"), info
        if info["status"] != 200:
            return result("unverifiable", f"{slug}: GitHub answered {info['status']}"), info
        if info.get("moved_from"):
            return result("drift", f"{slug} moved to {info['full_name']}"), info
        if info["archived"]:
            if lifecycle == "deprecated":
                return result("ok", f"{info['full_name']} archived, and the catalog says deprecated"), info
            return result("drift", f"{info['full_name']} is archived, catalog says lifecycle {lifecycle or 'unset'}"), info
        if info.get("empty"):
            return result("warn", f"{info['full_name']} has no commits"), info
        age = days_since(info.get("last_human_commit"), self.now)
        if info.get("last_human_commit") is None and info.get("last_commit"):
            return result("warn", f"last 30 commits on {info['default_branch']} are all from bots"), info
        if age is not None and age > self.stale_days:
            return result("warn", f"no human commit on {info['default_branch']} for {age} days"), info
        return result("ok", f"{info['full_name']} active ({age} days since a human commit)"), info

    def check_codeowners(self, owner_ref, org, co, file_path):
        if co is None:
            return result("n/a", "no CODEOWNERS file")
        rule = catalog.owners_for(co["rules"], file_path)
        if not rule or not rule["owners"]:
            return result("n/a", f"no CODEOWNERS rule covers {file_path}")
        owners = rule["owners"]
        shown = ", ".join(self.r.codeowner(o) for o in owners)
        unknown = [o for o in owners if o.lower() in co.get("unknown", set())]
        ref = catalog.parse_ref(owner_ref) if owner_ref else None
        expected = None
        if ref and ref["kind"] == "group":
            expected = f"@{org}/{ref['name']}".lower()
        elif ref and ref["kind"] == "user":
            expected = f"@{ref['name']}".lower()
        if unknown:
            out = result("drift", f"{co['path']} line {rule['line']} names owners GitHub cannot resolve: "
                                  + ", ".join(self.r.codeowner(o) for o in unknown))
            out["unknown_teams"] = sum(1 for o in unknown if "/" in o)
            out["unknown_users"] = len(unknown) - out["unknown_teams"]
            return out
        if expected and expected in (o.lower() for o in owners):
            return result("ok", f"{co['path']} agrees: {shown}")
        if expected:
            # Two sources disagree. One of them is probably stale, but a Backstage group
            # and a GitHub team can hold the same people under different names.
            out = result("warn", f"disagree: catalog says {self.r.owner(owner_ref)}, {co['path']} says {shown}")
            out["disagreement"] = disagreement_kind(ref, owners)
            return out
        return result("n/a", "no catalog owner to compare")

    def check_links(self, urls):
        if not urls:
            return result("n/a", "no links"), []
        details, dead, unver = [], [], []
        for u in urls:
            res = self.links.check(u["url"])
            details.append({"title": u.get("title"), "kind": u["kind"], "url": self.r.url(u["url"]),
                            "result": res["result"], "reason": res["reason"], "checked_at": res.get("checked_at")})
            if res["result"] == DEAD:
                dead.append(f"{u['kind']} {self.r.url(u['url'])} ({res['reason']})")
            elif res["result"] == UNVERIFIABLE:
                unver.append(u)
        checked = [d for d in details if d["result"] in (OK, DEAD)]
        if dead:
            return result("drift", f"{len(dead)} of {len(urls)} links dead: " + "; ".join(dead)), details
        if not checked:
            if all(d["result"] == SKIPPED for d in details):
                return result("n/a", "no http(s) links"), details
            return result("unverifiable", f"none of {len(urls)} links could be checked from outside"), details
        note = f", {len(unver)} could not be checked" if unver else ""
        return result("ok", f"{len(checked)} of {len(urls)} links answer{note}"), details

    def check_lifecycle(self, lifecycle, info):
        if not lifecycle:
            return result("warn", "no spec.lifecycle")
        if lifecycle not in LIFECYCLES:
            return result("n/a", f"custom lifecycle '{lifecycle}'")
        if not info or info.get("status") != 200:
            return result("unverifiable", "repository not readable")
        if info.get("archived"):
            return result("ok" if lifecycle == "deprecated" else "n/a", "archived repository (see repo check)")
        age = days_since(info.get("last_human_commit"), self.now)
        if age is not None and age > self.stale_days and lifecycle in ("production", "experimental"):
            return result("warn", f"{lifecycle}, but no human commit for {age} days")
        return result("ok", f"{lifecycle}")

    # ---------------------------------------------------------------- entry

    def check_entity(self, ent, source):
        meta = ent.get("metadata") or {}
        spec = ent.get("spec") or {}
        ann = meta.get("annotations") or {}
        owner_ref = spec.get("owner") if isinstance(spec.get("owner"), str) else None
        lifecycle = str(spec.get("lifecycle")).strip().lower() if spec.get("lifecycle") is not None else None
        slug, slug_from = repo_slug(ann, source.get("repo"))
        org = slug.split("/")[0] if slug else (source.get("repo") or "/").split("/")[0]
        out = {
            "source": f"{source.get('repo') or '-'}:{source['path']}",
            "org": org,
            "name": meta.get("name"),
            "kind": ent.get("kind"),
            "type": spec.get("type"),
            "lifecycle": lifecycle,
            "owner": self.r.owner(owner_ref),
            "owner_kind": (catalog.parse_ref(owner_ref) or {}).get("kind") if owner_ref else None,
            "repo": slug,
            "repo_from": slug_from,
            "checks": {},
        }
        info, co = None, None
        if slug:
            repo_res, info = self.check_repo(slug, lifecycle)
            out["checks"]["repo"] = repo_res
            if info.get("status") == 200:
                co = self.codeowners(info["full_name"], info.get("default_branch"))
                out["last_human_commit"] = info.get("last_human_commit")
                src = source.get("repo")
                if repo_res["status"] == "ok" and src and info["full_name"].lower() != src.lower() \
                        and source["path"].count("/") == 0:
                    # A root catalog file that describes another repository: often a copy
                    # from a template repo that nobody edited, sometimes intended.
                    out["checks"]["repo"] = result("warn", f"the catalog file in {src} points at {info['full_name']}")
        else:
            out["checks"]["repo"] = result("n/a", "no github.com/project-slug and no source repository")
        out["checks"]["owner"] = self.check_owner(owner_ref, org, co)
        same_repo = source.get("repo") and slug and info and info.get("status") == 200 and \
            info["full_name"].lower() == source["repo"].lower()
        file_path = source["path"] if same_repo else "catalog-info.yaml"
        out["checks"]["codeowners"] = self.check_codeowners(owner_ref, org, co, file_path)
        links_res, link_details = self.check_links(entity_links(meta, ann))
        out["checks"]["links"] = links_res
        out["links"] = link_details
        out["checks"]["lifecycle"] = self.check_lifecycle(lifecycle, info)
        statuses = [c["status"] for c in out["checks"].values()]
        out["status"] = "drift" if "drift" in statuses else "warn" if "warn" in statuses else "ok"
        if source.get("file_last_commit"):
            out["catalog_file_last_commit"] = source["file_last_commit"]
            out["catalog_file_age_days"] = days_since(source["file_last_commit"], self.now)
        return out


def disagreement_kind(ref, owners):
    teams = [o.split("/", 1)[1].lower().replace("_", "-") for o in owners if o.startswith("@") and "/" in o]
    if ref["kind"] == "user":
        return "catalog names a person, CODEOWNERS a team" if teams else "different people"
    if not teams:
        return "catalog names a team, CODEOWNERS people"
    name = ref["name"].lower().replace("_", "-")
    if any(name in t or t in name for t in teams):
        return "one team name contains the other"
    return "different team names"


def _team_verdict(co, handle, shown, where):
    """ok or drift if this CODEOWNERS file names the team, else None."""
    if not co or not any(handle == o.lower() for rule in co["rules"] for o in rule["owners"]):
        return None
    if co.get("errors_status") != 200:
        return None
    if handle in co["unknown"]:
        return result("drift", f"GitHub cannot resolve {shown} in {co['path']} of {where} "
                               "(team deleted, renamed, secret, or without write access)")
    return result("ok", f"{shown} resolves in {co['path']} of {where} (GitHub validates CODEOWNERS teams)")


def _is_bot(commit):
    author = commit.get("author") or {}
    if author.get("type") == "Bot" or BOT_NAMES.search(author.get("login") or ""):
        return True
    name = ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
    return bool(BOT_NAMES.search(name))


def _owners_on_line(line):
    return {o.lower() for o in line.split()[1:] if o.startswith("@")}


def repo_slug(ann, source_repo):
    slug = ann.get("github.com/project-slug")
    if isinstance(slug, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", slug.strip()):
        return slug.strip(), "github.com/project-slug"
    for key in ("backstage.io/source-location", "backstage.io/managed-by-location"):
        v = ann.get(key)
        if isinstance(v, str):
            m = re.search(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", v)
            if m:
                return f"{m.group(1)}/{m.group(2)}", key
    if source_repo:
        return source_repo, "file location"
    return None, None


LINK_KINDS = (
    ("runbook", re.compile(r"runbook|playbook|on-?call|incident", re.I)),
    ("dashboard", re.compile(r"dashboard|grafana|datadog|kibana|newrelic|new relic|monitor|metrics|status", re.I)),
    ("chat", re.compile(r"slack|teams\.microsoft|discord|chat|gitter|zulip|matrix", re.I)),
    ("docs", re.compile(r"doc|wiki|readme|confluence|notion|guide|handbook|manual|reference|api", re.I)),
)


def link_kind(title, url, key=""):
    text = f"{key} {title or ''} {url}"
    for kind, rx in LINK_KINDS:
        if rx.search(text):
            return kind
    return "other"


def entity_links(meta, ann):
    links, seen = [], set()
    for link in meta.get("links") or []:
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            url = link["url"].strip()
            if url and url not in seen:
                seen.add(url)
                links.append({"url": url, "title": link.get("title"),
                              "kind": link_kind(link.get("title"), url, link.get("type") or "")})
    for key, v in ann.items():
        if key in ("backstage.io/source-location", "backstage.io/managed-by-location",
                   "backstage.io/managed-by-origin-location", "backstage.io/view-url", "backstage.io/edit-url"):
            continue
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("url:"):
                v = v[4:]
            if re.match(r"^https?://\S+$", v) and v not in seen:
                seen.add(v)
                links.append({"url": v, "title": key, "kind": link_kind(key, v, key)})
    return links


# ------------------------------------------------------------------- inputs

def load_local(paths, repo):
    for p in paths:
        with open(p) as f:
            yield {"repo": repo, "path": os.path.basename(p) if repo else p, "text": f.read()}


def load_sample(gh, sample_file):
    with open(sample_file) as f:
        sample = json.load(f)
    for s in sample["files"]:
        r = gh.get(f"/repos/{s['repo']}/contents/{urllib.parse.quote(s['path'])}")
        if r.status != 200:
            yield {"repo": s["repo"], "path": s["path"], "text": None, "error": f"file not readable ({r.status})"}
            continue
        body = r.json()
        text = base64.b64decode(body["content"]).decode("utf-8", "replace") if body.get("content") is not None else ""
        c = gh.get(f"/repos/{s['repo']}/commits", {"path": s["path"], "per_page": 1})
        last = c.json()[0]["commit"]["committer"]["date"] if c.status == 200 and c.json() else None
        yield {"repo": s["repo"], "path": s["path"], "text": text, "blob_sha": body.get("sha"),
               "file_last_commit": last, "fetched_at": r.fetched_at}


def _code_hash():
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in ("drift.py", "catalog.py", "links.py", "ghapi.py"):
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def _input_hash(sample, files):
    h = hashlib.sha256()
    for p in ([sample] if sample else []) + list(files or []):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def _link_window(entries):
    times = sorted(t for e in entries for t in (x.get("checked_at") for x in e.get("links", [])) if t)
    return {"first": times[0], "last": times[-1]} if times else None


def is_template(text):
    return "${{" in text or "{{ " in text or "{%" in text


# ------------------------------------------------------------------- report

def print_report(entries, files, quiet=False):
    order = {"drift": 0, "warn": 1, "ok": 2}
    for e in sorted(entries, key=lambda x: (order[x["status"]], x["source"], x["name"] or "")):
        if quiet and e["status"] == "ok":
            continue
        print(f"{e['status'].upper():5}  {e['name']}  ({e['source']})  owner {e['owner'] or '-'}")
        for name in CHECKS:
            c = e["checks"].get(name)
            if c and (c["status"] in ("drift", "warn") or not quiet):
                print(f"         {name:10} {c['status']:12} {c['reason']}")
    total = len(entries)
    n = {s: sum(1 for e in entries if e["status"] == s) for s in ("drift", "warn", "ok")}
    print()
    print(f"{total} entries from {files['read']} files ({files['templates']} templates, "
          f"{files['unparsed']} unparsed, {files['unreadable']} unreadable skipped)")
    if files.get("errors"):
        print(f"  {files['errors']} entries could not be checked because of an error (see the JSON file)")
    if total:
        print(f"  drift {n['drift']} ({100 * n['drift'] / total:.1f}%), warn {n['warn']}, ok {n['ok']}")
        for name in CHECKS:
            counts = {}
            for e in entries:
                s = e["checks"].get(name, {}).get("status", "n/a")
                counts[s] = counts.get(s, 0) + 1
            print(f"  {name:10} " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def run(sources, checker, kinds, progress=False):
    entries, file_log = [], []
    counts = {"read": 0, "templates": 0, "unparsed": 0, "unreadable": 0, "other_kinds": 0}
    for src in sources:
        rec = {"repo": src.get("repo"), "path": src["path"], "blob_sha": src.get("blob_sha"),
               "fetched_at": src.get("fetched_at"), "file_last_commit": src.get("file_last_commit")}
        if src.get("text") is None:
            counts["unreadable"] += 1
            rec["skipped"] = src.get("error")
            file_log.append(rec)
            continue
        if is_template(src["text"]):
            counts["templates"] += 1
            rec["skipped"] = "template placeholders"
            file_log.append(rec)
            continue
        ents, err = catalog.entities(src["text"])
        if err:
            counts["unparsed"] += 1
            rec["skipped"] = f"parse error: {err[:120]}"
            file_log.append(rec)
            continue
        counts["read"] += 1
        picked = [e for e in ents if str(e.get("kind", "")).lower() in kinds]
        counts["other_kinds"] += len(ents) - len(picked)
        rec["entities"] = len(picked)
        file_log.append(rec)
        for ent in picked:
            try:
                entries.append(checker.check_entity(ent, src))
            except Exception as e:  # keep the run; report the entry as not checked
                counts["errors"] = counts.get("errors", 0) + 1
                rec.setdefault("errors", []).append(f"{(ent.get('metadata') or {}).get('name')}: "
                                                    f"{type(e).__name__}: {str(e)[:160]}")
                print(f"  error checking {src.get('repo')}:{src['path']}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if progress:
                print(f"  [{len(entries)}] {entries[-1]['status']:5} {entries[-1]['source']} "
                      f"{entries[-1]['name']}", file=sys.stderr)
    return entries, counts, file_log


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a Backstage catalog against GitHub and report drift.")
    ap.add_argument("files", nargs="*", help="local catalog-info.yaml files")
    ap.add_argument("--repo", help="owner/repo the local files belong to (for CODEOWNERS and the default slug)")
    ap.add_argument("--sample", help="sample file from sample.py; reads each file through the GitHub API")
    ap.add_argument("--kinds", default="Component", help="entity kinds to check (comma separated)")
    ap.add_argument("--stale-days", type=int, default=365)
    ap.add_argument("--redact", action="store_true", help="replace user logins, emails and link paths in the output")
    ap.add_argument("--allow-private", action="store_true", help="also check links on private addresses")
    ap.add_argument("--json", help="write the full result to this file")
    ap.add_argument("--quiet", action="store_true", help="print only entries with drift or warnings")
    ap.add_argument("--fail-on", choices=("drift", "warn"), default="drift")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-org-search", action="store_true",
                    help="do not search the org's other CODEOWNERS files for a team (saves code-search calls)")
    ap.add_argument("--users-are-github-logins", action="store_true",
                    help="your catalog's user: names are GitHub logins, so a missing account is drift")
    args = ap.parse_args(argv)
    if not args.files and not args.sample:
        ap.error("give catalog files or --sample")

    started = now_iso()
    gh = GitHub(use_cache=not args.no_cache)
    if not gh.token:
        print("no GitHub token: set GITHUB_TOKEN or log in with gh", file=sys.stderr)
        return 2
    links = LinkChecker(gh, use_cache=not args.no_cache, allow_private=args.allow_private)
    checker = Checker(gh, links, stale_days=args.stale_days, redact=args.redact,
                      org_search=not args.no_org_search, users_are_logins=args.users_are_github_logins)
    kinds = {k.strip().lower() for k in args.kinds.split(",")}
    sources = load_sample(gh, args.sample) if args.sample else load_local(args.files, args.repo)

    entries, counts, file_log = run(sources, checker, kinds, progress=bool(args.sample))

    print_report(entries, counts, quiet=args.quiet)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({
                "started_at": started,
                "finished_at": now_iso(),
                # Responses can come from the cache: these bound when GitHub actually answered.
                "github_responses_fetched": {"first": gh.first_fetch, "last": gh.last_fetch},
                "links_checked": _link_window(entries),
                "code_sha256": _code_hash(),
                "input_sha256": _input_hash(args.sample, args.files),
                "args": {k: v for k, v in vars(args).items() if k != "files"} | {"files": args.files},
                "stale_days": args.stale_days,
                "api_calls": gh.calls,
                "api_cache_hits": gh.cache_hits,
                "link_requests": links.requests,
                "file_counts": counts,
                "files": file_log,
                "entries": entries,
            }, f, indent=1)
        print(f"wrote {args.json}")
    if any(e["status"] == "drift" for e in entries):
        return 1
    if args.fail_on == "warn" and any(e["status"] == "warn" for e in entries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
