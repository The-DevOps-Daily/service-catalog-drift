#!/usr/bin/env python3
"""Stage 1: find public catalog-info.yaml files and pick a sample.

Uses GitHub code search (filename:catalog-info.yaml path:/), split by file size
so that no single query passes the 1,000-result cap. Keeps repositories owned
by an organization (not forks), keeps organizations that publish the file in
many repositories and look like real organizations rather than test or demo
orgs (a website on the profile and a repository with some stars), and draws a
seeded random sample of repositories from each.

Writes sample/population-<date>.json (the sampling frame) and
sample/sample-<date>.json. Only organization and repository names and file
paths are stored; organizations that are not kept are stored as a hash.
"""

import argparse
import hashlib
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timezone

from ghapi import GitHub

QUERY = "filename:catalog-info.yaml path:/"
CAP = 1000


def count(gh, q):
    r = gh.get("/search/code", {"q": q, "per_page": 1})
    if r.status != 200:
        raise SystemExit(f"search failed ({r.status}): {r.body[:200]}")
    return r.json()["total_count"]


def partitions(gh, lo, hi):
    """Split [lo, hi] byte sizes until each range has at most CAP results."""
    q = f"{QUERY} size:{lo}..{hi}"
    n = count(gh, q)
    if n <= CAP or lo == hi:
        return [(q, n)]
    mid = (lo + hi) // 2
    return partitions(gh, lo, mid) + partitions(gh, mid + 1, hi)


def collect(gh, q):
    items, status = gh.paginate("/search/code", {"q": q}, limit=CAP)
    if status != 200:
        raise SystemExit(f"search page failed ({status}) for {q}")
    return items


def org_profile(gh, org):
    """Website and most-starred repository: a proxy for a real organization, not a test org."""
    meta = gh.get(f"/orgs/{org}").json() or {}
    r = gh.get("/search/repositories", {"q": f"org:{org}", "sort": "stars", "per_page": 1})
    items = (r.json() or {}).get("items") or [{}]
    return {"website": bool(meta.get("blog")), "top_stars": items[0].get("stargazers_count") or 0}


def anon(org):
    return "sha256:" + hashlib.sha256(org.lower().encode()).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-repos", type=int, default=20,
                    help="an org needs this many repos with a root catalog-info.yaml to be eligible")
    ap.add_argument("--min-stars", type=int, default=10,
                    help="and a repository with at least this many stars, and a website on its profile")
    ap.add_argument("--orgs", type=int, default=0, help="organizations to sample (0: all that qualify)")
    ap.add_argument("--per-org", type=int, default=8, help="repositories to sample per organization")
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--max-size", type=int, default=20000)
    ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample"))
    args = ap.parse_args()

    gh = GitHub()
    os.makedirs(args.outdir, exist_ok=True)

    total = count(gh, QUERY)
    parts = partitions(gh, 0, args.max_size)
    print(f"{QUERY}: {total} results, {len(parts)} size ranges")
    seen = {}
    for q, n in parts:
        if n == 0:
            continue
        items = collect(gh, q)
        print(f"  {q}: {n} reported, {len(items)} fetched")
        for it in items:
            repo = it["repository"]
            seen[repo["full_name"].lower()] = {
                "repo": repo["full_name"],
                "org": repo["owner"]["login"],
                "owner_type": repo["owner"]["type"],
                "fork": repo.get("fork", False),
                "path": it["path"],
                "blob_sha": it["sha"],
            }
    searched = {"first": gh.first_fetch, "last": gh.last_fetch}
    day = gh.first_fetch[:10]
    allrepos = sorted(seen.values(), key=lambda x: x["repo"].lower())
    user_owned = sum(1 for p in allrepos if p["owner_type"] != "Organization")
    forks = sum(1 for p in allrepos if p["owner_type"] == "Organization" and p["fork"])
    orgs = Counter(p["org"] for p in allrepos if p["owner_type"] == "Organization" and not p["fork"])

    eligible = []
    for org in sorted((o for o, n in orgs.items() if n >= args.min_repos), key=str.lower):
        prof = org_profile(gh, org)
        kept = prof["website"] and prof["top_stars"] >= args.min_stars
        eligible.append({"org": org, "repos": orgs[org], **prof, "kept": kept})
    kept = [e["org"] for e in eligible if e["kept"]]

    rng = random.Random(args.seed)
    chosen = sorted(rng.sample(kept, args.orgs) if args.orgs and args.orgs < len(kept) else kept, key=str.lower)
    frame = [p for p in allrepos if p["org"] in set(chosen) and p["owner_type"] == "Organization" and not p["fork"]]
    files = []
    for org in chosen:
        repos = [p for p in frame if p["org"] == org]
        files.extend(rng.sample(repos, min(args.per_org, len(repos))))

    counts = {
        "reported_total": total,
        "unique_repos_fetched": len(allrepos),
        "user_owned_repos": user_owned,
        "org_owned_forks": forks,
        "org_owned_repos_not_forks": sum(orgs.values()),
        "orgs": len(orgs),
        "orgs_with_min_repos": len(eligible),
        "orgs_kept": len(kept),
    }
    # Test, demo and personal orgs are often named after a person: those that are
    # not kept are recorded by a hash of their name only.
    eligible_out = [e if e["kept"] else {**e, "org": anon(e["org"])} for e in eligible]
    with open(os.path.join(args.outdir, f"population-{day}.json"), "w") as f:
        json.dump({"query": QUERY, "searched": searched, **counts,
                   "size_ranges": [{"query": q, "reported": n} for q, n in parts],
                   "frame": frame}, f, indent=1)
    sample = {
        "query": QUERY,
        "searched": searched,
        **counts,
        "rule": f"org owns at least {args.min_repos} non-fork repos with a root catalog-info.yaml, "
                f"has a website on its GitHub profile, and has a repo with at least {args.min_stars} stars",
        "eligible_orgs": eligible_out,
        "seed": args.seed,
        "chosen_orgs": chosen,
        "per_org": args.per_org,
        "files": files,
    }
    out = os.path.join(args.outdir, f"sample-{day}.json")
    with open(out, "w") as f:
        json.dump(sample, f, indent=1)
    print(f"{len(allrepos)} repos: {user_owned} personal, {forks} forks, {sum(orgs.values())} in {len(orgs)} orgs; "
          f"{len(eligible)} orgs with >= {args.min_repos}, {len(kept)} kept; "
          f"sampled {len(files)} files from {len(chosen)} orgs")
    print(f"wrote {out} ({gh.calls} API calls)")


if __name__ == "__main__":
    sys.exit(main())
