# service-catalog-drift

Check a Backstage service catalog against GitHub and report what has drifted: owners that no longer exist, repositories that moved or were archived, CODEOWNERS that disagrees, dead runbook and dashboard links, and lifecycles that do not match the repository.

```bash
# your own catalog file(s)
python3 drift.py catalog-info.yaml --repo your-org/your-repo

# the public sample the write-up measured
python3 sample.py                                   # stage 1: find files, pick the sample
python3 drift.py --sample sample/sample-2026-10-22.json --redact --json results/run-<time>.json
python3 summarize.py results/run-<time>.json        # stage 3: the numbers
```

It needs Python 3.10 or later and nothing else: the YAML reader, the GitHub client and the link checker use only the standard library. The token comes from `GITHUB_TOKEN` or a logged-in `gh`, and it is only ever sent to `api.github.com`.

Exit code: `0` no drift, `1` drift found (or warnings, with `--fail-on warn`), `2` usage or API error. That makes it usable as a CI step: see [examples/catalog-drift.yml](examples/catalog-drift.yml).

Write-up: [A Service Catalog Nobody Updates Is Worse Than None](https://devops-daily.com/posts/service-catalog-drift)

## Result, 2026-10-22

217 Component entries from 238 root `catalog-info.yaml` files in 30 public GitHub organizations (8 random repositories per org, seed 20261022), checked from outside the orgs:

| | Entries | Share |
| --- | ---: | ---: |
| At least one provable drift | 21 | 9.7% |
| Only warnings | 60 | 27.6% |
| Nothing found | 136 | 62.7% |
| Owner could not be verified from outside | 158 | 72.8% |

Where the catalog file is also covered by a CODEOWNERS rule (73 entries), the two agree, and every owner resolves, in 37 (50.7%). In 14, CODEOWNERS names owners GitHub cannot resolve; in 22, the two name different owners (7 of those look like the same team under two names). Drift was 7.1% for catalog files changed in the last year and 12.4% for older ones (Fisher's exact p = 0.25, so no clear trend). Of 152 links, 2 were dead and 17 could not be checked. Details, method and caveats are in the write-up; the raw results are in [results/](results/).

## What it checks

Every check is read-only and reports `ok`, `drift`, `warn`, `unverifiable` or `n/a`, with a reason. Only `drift` means the catalog is provably wrong.

| Check | Drift | Warn | Unverifiable |
| --- | --- | --- | --- |
| owner | no `spec.owner`; a `user:` whose GitHub account is gone; a team that GitHub cannot resolve in CODEOWNERS; with org access, a team that does not exist or has no members, or a user who is not a member | owner is not a user or a group | from outside the org: a team that no CODEOWNERS file names, a user who is not a public member, or a user name that is not a GitHub login |
| repo | renamed or transferred (the slug redirects); archived while the catalog does not say `deprecated`; with org access, not found | no human commit on the default branch in `--stale-days` (365); bot commits do not count | not visible from outside (deleted or private) |
| codeowners | the CODEOWNERS rule for the catalog file names an owner GitHub cannot resolve | CODEOWNERS and the catalog name different owners | |
| links | a link answers 404 or 410, or a GitHub file, branch or repository it points at is gone | | 401, 403, 429, 5xx, timeouts, no public DNS, private addresses, GitHub pages that need a signed-in user |
| lifecycle | | no `spec.lifecycle`; `production` or `experimental` with no human commit in a year | |

How owners are checked depends on what the token can see:

- **With access to the org** (a token that can list the org's teams), teams and membership are read directly.
- **From outside**, teams are not visible. The checker looks for the team in the repository's CODEOWNERS file, then in other CODEOWNERS files in the org (one code search per team), and asks GitHub's CODEOWNERS validation (`GET /repos/{owner}/{repo}/codeowners/errors`) whether the team resolves. GitHub reports a team as unknown when it does not exist, was renamed, is secret, or has no write access to that repository. A team that no CODEOWNERS file names stays `unverifiable`.

Links get a `HEAD`, then a `GET` if `HEAD` does not answer with 2xx or 3xx, with a 10 second timeout, one request per second per host, no `Authorization` header and no cookies. Hosts that resolve to private addresses are not contacted unless you pass `--allow-private`. Links to github.com files and repositories are checked through the API instead. Results are cached in `.cache/` (git-ignored), so a re-run does not repeat requests; `--no-cache` forces fresh ones.

## Files

| File | What it does |
| --- | --- |
| `sample.py` | Stage 1. Code search for `filename:catalog-info.yaml path:/`, split by file size to get past the 1,000-result cap; keeps organization-owned repositories; picks orgs with at least 20 such repositories and a seeded random sample of repositories from each |
| `drift.py` | Stage 2. The checker. Reads local files or a sample, writes a report and, with `--json`, every entry with every check and reason |
| `summarize.py` | Stage 3. Aggregates a result file into the numbers in the write-up |
| `catalog.py` | YAML subset reader, entity references, CODEOWNERS parsing and matching |
| `ghapi.py` | Read-only GitHub client: disk cache, rate-limit waits, no automatic redirects |
| `links.py` | The link checker |
| `fixtures/` | A made-up catalog with one of each kind of drift, used by the tests |
| `tests/` | `python3 -m unittest discover -s tests -t .` (no network) |

## Privacy

The public run uses `--redact`: user logins become `user:<redacted>`, user handles in CODEOWNERS become `@<user>`, emails become `<email>`, and link paths are replaced by a hash. The committed results name organizations, repositories and services, never a person. Repositories owned by a personal account are counted in the population file but not listed. Raw API responses stay in `.cache/`, which is not committed.

## License

MIT
