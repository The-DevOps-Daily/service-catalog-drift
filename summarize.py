#!/usr/bin/env python3
"""Stage 3: turn a drift.py result file into the numbers the write-up quotes.

  python3 summarize.py results/run-<time>.json

Prints the summary and writes results/summary-<same name>.json next to it.
Only aggregates and organization, repository and service names are written.
"""

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

CHECKS = ("owner", "repo", "codeowners", "links", "lifecycle")
AGE_BUCKETS = ((0, 180, "under 6 months"), (180, 365, "6 to 12 months"),
               (365, 730, "1 to 2 years"), (730, 10 ** 6, "over 2 years"))


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def fisher_two_sided(a, b, c, d):
    """Fisher's exact test for the 2x2 table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    total = math.comb(n1 + n2, k)

    def p(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / total
    observed = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9)))


def reason_class(check, c):
    r = c["reason"]
    if c["status"] not in ("drift", "warn"):
        return None
    if check == "owner":
        if r.startswith("no spec.owner"):
            return "no owner field"
        if "no GitHub account" in r:
            return "owner user: no GitHub account with that name"
        if "cannot resolve" in r:
            return "owner team does not resolve on GitHub"
        if "not a member" in r:
            return "owner user not in org"
        if "no team" in r or "no members" in r:
            return "owner team missing or empty"
        return "owner other"
    if check == "repo":
        if "moved to" in r:
            return "repo renamed or transferred"
        if "archived" in r:
            return "repo archived, not marked deprecated"
        if "not found" in r:
            return "repo not found"
        if "no human commit" in r or "all from bots" in r:
            return "no human commit in a year"
        if "points at" in r:
            return "root catalog file describes another repo"
        return "repo other"
    if check == "codeowners":
        if r.startswith("disagree"):
            return "catalog and CODEOWNERS disagree"
        if "cannot resolve" in r:
            return "CODEOWNERS names an owner GitHub cannot resolve"
        return "codeowners other"
    if check == "links":
        return "dead link"
    if check == "lifecycle":
        if "no spec.lifecycle" in r:
            return "no lifecycle field"
        return "lifecycle says active, no human commit in a year"
    return f"{check} other"


def summarize(run):
    entries = run["entries"]
    n = len(entries)
    out = {
        "source_run": {k: run[k] for k in ("started_at", "finished_at", "stale_days", "api_calls", "link_requests")},
        "file_counts": run["file_counts"],
        "entries": n,
        "orgs": len({e["org"].lower() for e in entries}),
        "repos": len({(e["repo"] or "").lower() for e in entries}),
        "status": dict(Counter(e["status"] for e in entries)),
    }
    out["drift_pct"] = pct(out["status"].get("drift", 0), n)
    out["warn_pct"] = pct(out["status"].get("warn", 0), n)
    out["drift_or_warn_pct"] = pct(out["status"].get("drift", 0) + out["status"].get("warn", 0), n)

    per_check = {}
    for check in CHECKS:
        counts = Counter(e["checks"].get(check, {}).get("status", "n/a") for e in entries)
        verifiable = counts.get("ok", 0) + counts.get("drift", 0) + counts.get("warn", 0)
        per_check[check] = {
            "counts": dict(counts),
            "verifiable": verifiable,
            "drift_pct_of_all": pct(counts.get("drift", 0), n),
            "drift_pct_of_verifiable": pct(counts.get("drift", 0), verifiable),
            "warn_pct_of_verifiable": pct(counts.get("warn", 0), verifiable),
        }
    out["per_check"] = per_check

    reasons = Counter()
    entries_by_reason = defaultdict(set)
    for i, e in enumerate(entries):
        for check in CHECKS:
            c = e["checks"].get(check)
            if not c:
                continue
            rc = reason_class(check, c)
            if rc:
                reasons[(check, c["status"], rc)] += 1
                entries_by_reason[rc].add(i)
    out["reasons"] = [{"check": k[0], "status": k[1], "reason": k[2], "entries": v, "pct": pct(v, n)}
                      for k, v in reasons.most_common()]

    # Owner verification detail
    owner_kinds = Counter(e.get("owner_kind") or "none" for e in entries)
    out["owner_kinds"] = dict(owner_kinds)
    ov = Counter()
    for e in entries:
        c = e["checks"]["owner"]
        how = "unverifiable"
        if c["status"] in ("ok", "drift"):
            if "CODEOWNERS" in c["reason"] and "this repository" in c["reason"]:
                how = "via this repo's CODEOWNERS"
            elif "CODEOWNERS" in c["reason"]:
                how = "via another repo's CODEOWNERS"
            elif "GitHub account" in c["reason"] or "public member" in c["reason"]:
                how = "via GitHub user API"
            else:
                how = "other"
        ov[(how, c["status"])] += 1
    out["owner_verification"] = [{"how": k[0], "status": k[1], "entries": v} for k, v in sorted(ov.items())]

    # Where the catalog and CODEOWNERS both name an owner for the catalog file
    two = [e["checks"]["codeowners"] for e in entries if e["checks"]["codeowners"]["status"] in ("ok", "warn", "drift")]
    out["catalog_vs_codeowners"] = {
        "entries_with_both": len(two),
        "agree_and_resolve": sum(1 for c in two if c["status"] == "ok"),
        "agree_and_resolve_pct": pct(sum(1 for c in two if c["status"] == "ok"), len(two)),
        "disagree": dict(Counter(c.get("disagreement", "?") for c in two if c["status"] == "warn")),
        "unknown_owner_entries": sum(1 for c in two if c["status"] == "drift"),
        "unknown_owner_entries_with_teams": sum(1 for c in two if c["status"] == "drift" and c.get("unknown_teams")),
        "unknown_owner_entries_with_users": sum(1 for c in two if c["status"] == "drift" and c.get("unknown_users")),
    }

    # Links
    link_results = Counter()
    link_reasons = Counter()
    dead_by_kind = Counter()
    all_by_kind = Counter()
    entries_with_links = 0
    for e in entries:
        ls = e.get("links") or []
        if ls:
            entries_with_links += 1
        for link in ls:
            link_results[link["result"]] += 1
            all_by_kind[link["kind"]] += 1
            if link["result"] == "dead":
                dead_by_kind[link["kind"]] += 1
            if link["result"] in ("unverifiable", "dead"):
                link_reasons[re.sub(r"\(.*\)", "", link["reason"]).strip()] += 1
    total_links = sum(link_results.values())
    checked = link_results.get("ok", 0) + link_results.get("dead", 0)
    out["links"] = {
        "entries_with_links": entries_with_links,
        "links": total_links,
        "results": dict(link_results),
        "dead_pct_of_checked": pct(link_results.get("dead", 0), checked),
        "by_kind": {k: {"links": all_by_kind[k], "dead": dead_by_kind.get(k, 0)} for k in all_by_kind},
        "unverifiable_or_dead_reasons": dict(link_reasons.most_common()),
    }

    # Catalog file age against drift
    ages = sorted(e["catalog_file_age_days"] for e in entries if e.get("catalog_file_age_days") is not None)
    if ages:
        out["catalog_file_age_days"] = {"median": ages[len(ages) // 2], "min": ages[0], "max": ages[-1],
                                        "n": len(ages)}
        buckets = []
        for lo, hi, label in AGE_BUCKETS:
            sub = [e for e in entries if e.get("catalog_file_age_days") is not None
                   and lo <= e["catalog_file_age_days"] < hi]
            d = sum(1 for e in sub if e["status"] == "drift")
            w = sum(1 for e in sub if e["status"] == "warn")
            buckets.append({"bucket": label, "entries": len(sub), "drift": d, "warn": w,
                            "drift_pct": pct(d, len(sub)), "drift_or_warn_pct": pct(d + w, len(sub))})
        out["by_catalog_file_age"] = buckets
        young = [e for e in entries if e.get("catalog_file_age_days") is not None and e["catalog_file_age_days"] < 365]
        old = [e for e in entries if e.get("catalog_file_age_days") is not None and e["catalog_file_age_days"] >= 365]
        yd = sum(1 for e in young if e["status"] == "drift")
        od = sum(1 for e in old if e["status"] == "drift")
        out["drift_by_age_test"] = {
            "under_1_year": {"entries": len(young), "drift": yd, "pct": pct(yd, len(young))},
            "1_year_or_more": {"entries": len(old), "drift": od, "pct": pct(od, len(old))},
            "fisher_two_sided_p": round(fisher_two_sided(yd, len(young) - yd, od, len(old) - od), 3),
        }

    # Per organization, to show whether one org drives the result
    per_org = []
    for org in sorted({e["org"].lower() for e in entries}):
        sub = [e for e in entries if e["org"].lower() == org]
        d = sum(1 for e in sub if e["status"] == "drift")
        w = sum(1 for e in sub if e["status"] == "warn")
        per_org.append({"org": org, "entries": len(sub), "drift": d, "warn": w, "drift_pct": pct(d, len(sub))})
    out["per_org"] = per_org
    out["lifecycle_values"] = dict(Counter(e.get("lifecycle") or "none" for e in entries))
    return out


def print_summary(s):
    print(f"{s['entries']} entries, {s['orgs']} orgs, {s['repos']} repos "
          f"(run {s['source_run']['started_at']} to {s['source_run']['finished_at']})")
    st = s["status"]
    print(f"  drift {st.get('drift', 0)} ({s['drift_pct']}%), warn {st.get('warn', 0)} ({s['warn_pct']}%), "
          f"ok {st.get('ok', 0)}; drift or warn {s['drift_or_warn_pct']}%")
    print("\nper check (drift % of all entries / of entries the check could verify):")
    for check, c in s["per_check"].items():
        print(f"  {check:10} {c['counts']}  drift {c['drift_pct_of_all']}% / {c['drift_pct_of_verifiable']}% "
              f"of {c['verifiable']}")
    print("\nreasons:")
    for r in s["reasons"]:
        print(f"  {r['entries']:4} ({r['pct']:5}%)  {r['status']:5} {r['reason']}")
    print("\nowner verification:")
    for o in s["owner_verification"]:
        print(f"  {o['entries']:4}  {o['how']:32} {o['status']}")
    lk = s["links"]
    print(f"\nlinks: {lk['links']} on {lk['entries_with_links']} entries, {lk['results']}, "
          f"dead {lk['dead_pct_of_checked']}% of checked")
    for k, v in sorted(lk["by_kind"].items()):
        print(f"  {k:10} {v['links']:4} links, {v['dead']} dead")
    for k, v in lk["unverifiable_or_dead_reasons"].items():
        print(f"  {v:4}  {k}")
    if "by_catalog_file_age" in s:
        a = s["catalog_file_age_days"]
        print(f"\ncatalog file last changed: median {a['median']} days ago (n={a['n']})")
        for b in s["by_catalog_file_age"]:
            print(f"  {b['bucket']:15} {b['entries']:4} entries, drift {b['drift_pct']}%, "
                  f"drift or warn {b['drift_or_warn_pct']}%")
    cv = s["catalog_vs_codeowners"]
    print(f"\ncatalog vs CODEOWNERS: {cv['entries_with_both']} entries with both; agree and resolve "
          f"{cv['agree_and_resolve']} ({cv['agree_and_resolve_pct']}%); unknown owners {cv['unknown_owner_entries']} "
          f"(teams {cv['unknown_owner_entries_with_teams']}, users {cv['unknown_owner_entries_with_users']})")
    for k, v in cv["disagree"].items():
        print(f"  disagree: {v:3}  {k}")
    if "drift_by_age_test" in s:
        t = s["drift_by_age_test"]
        print(f"drift by file age: under 1 year {t['under_1_year']}, 1 year or more {t['1_year_or_more']}, "
              f"Fisher p = {t['fisher_two_sided_p']}")
    print("\nper org:")
    for o in s["per_org"]:
        print(f"  {o['org']:28} {o['entries']:4} entries, drift {o['drift']} ({o['drift_pct']}%), warn {o['warn']}")
    print(f"\nlifecycle values: {s['lifecycle_values']}")
    print(f"owner kinds: {s['owner_kinds']}")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    with open(path) as f:
        run = json.load(f)
    s = summarize(run)
    print_summary(s)
    out = os.path.join(os.path.dirname(path), "summary-" + os.path.basename(path).removeprefix("run-"))
    with open(out, "w") as f:
        json.dump(s, f, indent=1)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
