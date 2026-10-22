# Results

| File | What it is |
| --- | --- |
| `run-20261022T085822Z.json` | The run the write-up quotes: every entry, every check, every reason. Made with `--check-links --redact`. GitHub answered between 08:36:04 and 08:58:49 UTC on 2026-10-22 (`github_responses_fetched`); the answers came from the fresh 08:36 pass described below, cached per token. Links were checked between 08:37:03 and 08:56:49 UTC |
| `summary-20261022T085822Z.json` | `summarize.py` on that run |
| `yaml-crosscheck-2026-10-22.json` | The standard-library YAML reader against PyYAML on all 240 sampled files: 239 agree on every field the checker reads, 1 file is invalid YAML for both |
| `run-20261022T081013Z.json`, `summary-20261022T081013Z.json` | Superseded: the run before the second review. Same headline (21 drift entries), but it counted a team that GitHub could not resolve as owner drift, took a `github.com/project-slug` without an owner as the file's own repository, and treated any page below an existing repository as a working link |
| `run-20261022T075730Z.json`, `summary-20261022T075730Z.json` | Superseded: made before the YAML reader rejected a key inside a multi-line value, so one invalid file (sendbird/sendbird-platform-sdk-typescript) was read as an entry. 218 entries, 21 drift |

Runs that left no file, kept here so the history is complete:

- 07:31 UTC: stopped after about 100 entries on a link that contained a space. The link checker now percent-encodes such links, and an error in one entry no longer stops the run.
- 07:42 UTC: finished, but printed owner names that were not marked as users (one of them was a person's login, one an email address) and counted a user ID from another directory as a missing GitHub account. Its file was deleted because of the personal data. Owner and team names are now hashed, and a missing account is a warning unless you pass `--users-are-github-logins`.
- 07:59 and 08:00 UTC: same counts as the 08:10 run, replaced when the link checker moved to pinned public addresses.
- 08:36 UTC: the first run after the second review, with an empty cache, so every GitHub answer and link result was fetched fresh. Replaced at 08:58 by a fix that reads the CODEOWNERS of the catalog file's own repository when its `github.com/project-slug` is invalid (one entry's CODEOWNERS check went from n/a to ok, and its lifecycle from unverifiable to ok).

Hand check, 2026-10-22: the two links reported dead (a Storybook page and a vulnerability report page on a vendor's site) also answer 404 to `curl -L` with a browser User-Agent.

Every run file carries `code_sha256` (the checker's source) and `input_sha256` (the sample file).
