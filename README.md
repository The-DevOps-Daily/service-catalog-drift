# service-catalog-drift

Check a Backstage service catalog against GitHub and report what has drifted: owners that no longer exist, repositories that moved or were archived, CODEOWNERS that disagrees, dead runbook and dashboard links, and lifecycles that do not match the repository.

```bash
# your own catalog file(s)
python3 drift.py catalog-info.yaml --repo your-org/your-repo --check-links

# the public sample the write-up measured
python3 sample.py                                   # stage 1: find files, pick the sample
python3 drift.py --sample sample/sample-2026-10-22.json --check-links --redact --json results/run-<time>.json
python3 summarize.py results/run-<time>.json        # stage 3: the numbers
```

It needs Python 3.10 or later and nothing else: the YAML reader, the GitHub client and the link checker use only the standard library. The token comes from `GITHUB_TOKEN` or a logged-in `gh`, and it is only ever sent to `api.github.com`.

Exit code: `0` no drift, `1` drift found or a catalog file that does not parse (or warnings, with `--fail-on warn`), `2` the run could not check what it was given (an entry failed, a file was unreadable, or nothing was checked). That makes it usable as a CI step: see [examples/catalog-drift.yml](examples/catalog-drift.yml), which reads CODEOWNERS at the pull request's head with `--ref`.

Write-up: [A Service Catalog Nobody Updates Can Be Worse Than None](https://devops-daily.com/posts/service-catalog-drift)

## Result, 2026-10-22

217 Component entries from root `catalog-info.yaml` files in 214 repositories of 30 public GitHub organizations (8 random repositories per org, seed 20261022), checked from outside the orgs:

| | Entries | Share |
| --- | ---: | ---: |
| At least one drift: GitHub contradicts the entry or its CODEOWNERS | 21 | 9.7% |
| ...in the catalog entry itself | 9 | 4.1% |
| ...only in the CODEOWNERS file next to it | 12 | 5.5% |
| Only warnings | 62 | 28.6% |
| Nothing found | 134 | 61.8% |
| Owner could not be checked from outside | 159 | 73.3% |

Where the catalog file is also covered by a CODEOWNERS rule (73 entries), the two agree, and every owner resolves, in 37 (50.7%). In 14, CODEOWNERS names owners GitHub cannot resolve; in 22, the two name different owners (in 7 of those one team name contains the other, which may be the same team). Drift was 8.0% for catalog files changed in the last year and 11.4% for older ones (Fisher's exact p = 0.49, which also assumes independent entries), so no clear trend. Of 152 links, 2 were dead and 18 could not be checked. Details, method and caveats are in the write-up; the raw results are in [results/](results/).

## What it checks

Every check is read-only and reports `ok`, `drift`, `warn`, `unverifiable` or `n/a`, with a reason. `drift` means GitHub contradicts the record: the catalog entry, or the CODEOWNERS file next to it. A warning is worth a look but proves nothing on its own.

| Check | Drift | Warn | Unverifiable |
| --- | --- | --- | --- |
| owner | no `spec.owner`; with org access, a team that does not exist or has no members; with `--users-are-github-logins`, a user with no GitHub account or not in the org | a team GitHub cannot resolve in the CODEOWNERS files that name it (from outside); a `user:` with no GitHub account of that name; owner not a user or group | a team no CODEOWNERS file names; a user who is not a public member; a name that is not a GitHub login; a group in a namespace other than `default` or the org's name |
| repo | renamed or transferred (the slug redirects); archived while the catalog does not say `deprecated`; a `github.com/project-slug` that is not `owner/repo` | no human commit on the default branch in `--stale-days` (365), or only bot commits among the last 30; a root catalog file that describes another repository; with org access, not found with this token | not visible from outside (deleted or private) |
| codeowners | the CODEOWNERS rule for the catalog file names owners GitHub's validation cannot resolve | CODEOWNERS and the catalog name different owners | GitHub's validation was not readable |
| links | 404 or 410; a GitHub file missing at an existing ref; a repository section (issues, wiki, discussions) that is turned off | | 401, 403, 429, 5xx, timeouts, redirect loops, no public DNS, non-public addresses, refs that may contain a slash, GitHub pages below a repository |
| lifecycle | | no `spec.lifecycle`; `production` or `experimental` with no human commit in a year | |

How owners are checked depends on what the token can see. The checker assumes Backstage group names are GitHub team slugs, which is what Backstage's GitHub org provider produces; groups in other namespaces are not mapped.

- **With access to the org** (a token that can list the org's teams), teams and membership are read directly.
- **From outside**, teams are not visible. The checker looks for the team in the repository's CODEOWNERS file, then in other CODEOWNERS files in the org (one code search per team), and asks GitHub's CODEOWNERS validation (`GET /repos/{owner}/{repo}/codeowners/errors`) whether the team resolves. A team that resolves exists. A team that does not resolve may be deleted, renamed, secret, or without write access to that repository, so the owner check only warns; the CODEOWNERS check reports the broken file as drift. A team that no CODEOWNERS file names stays `unverifiable`.

GitHub answers and link results are cached in `.cache/` per token for `--cache-hours` (24 by default); `--no-cache` forces fresh requests. Use `--cache-hours 0` for a scheduled audit.

Links are only fetched with `--check-links`, because they are written by whoever wrote the catalog file. With it, each link gets a `HEAD`, then a `GET` if `HEAD` does not answer with 2xx or 3xx, with a 10 second timeout, one request per second per host, no `Authorization` header and no cookies. The host name is resolved once and every address it resolves to must be a public address; the request then goes to that pinned address, with the original name in the `Host` header and in TLS SNI, so a second DNS answer cannot redirect it. Every redirect hop is resolved and checked the same way. `--allow-private` lifts the public-address rule for a catalog you trust, such as your own, on a network where internal links resolve. Links to github.com files and repositories are checked through the API instead: the ref must exist before a missing file counts as dead.

Values from catalog files never go into an API path unchecked: owner and repository names must match `^[A-Za-z0-9_.-]{1,100}$` and cannot be `.` or `..`, file path segments are quoted one by one, and anything else is reported as an invalid reference instead of being fetched.

## Files

| File | What it does |
| --- | --- |
| `sample.py` | Stage 1. Code search for `filename:catalog-info.yaml path:/` (files up to 20,000 bytes), split by file size to get past the 1,000-result cap; keeps organization-owned repositories; keeps orgs with at least 20 such repositories, a website on their profile and a repository with 10 or more stars; draws a seeded random sample of 8 repositories from each |
| `crosscheck_yaml.py` | Method check: compares the YAML reader with PyYAML on every sampled file |
| `drift.py` | Stage 2. The checker. Reads local files or a sample, writes a report and, with `--json`, every entry with every check and reason |
| `summarize.py` | Stage 3. Aggregates a result file into the numbers in the write-up |
| `catalog.py` | YAML subset reader, entity references, CODEOWNERS parsing and matching |
| `ghapi.py` | Read-only GitHub client: disk cache, rate-limit waits, no automatic redirects |
| `links.py` | The link checker |
| `fixtures/` | A made-up catalog with one of each kind of drift, used by the tests |
| `tests/` | `python3 -m unittest discover -s tests -t .` (no network) |

## Privacy

The public run uses `--redact`: user logins become `user:<redacted>`, user handles in CODEOWNERS become `@<user>`, emails become `<email>`, owner and team names become a hash with a key that is random per run and never written, and link paths are replaced by a hash. The committed results name organizations, repositories and services, never a person. Repositories owned by a personal account are counted in the population file but not listed. Raw API responses stay in `.cache/`, which is not committed.

## License

MIT
