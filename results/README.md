# Results

| File | What it is |
| --- | --- |
| `run-20261022T081013Z.json` | The run the write-up quotes: every entry, every check, every reason. Made with `--check-links --redact`. GitHub answers in it were fetched between 07:30:53 and 08:13:51 UTC on 2026-10-22 (`github_responses_fetched`; most came from the cache of the earlier passes listed below). Links other than github.com ones were checked fresh in this run, 08:10 to 08:13 UTC |
| `summary-20261022T081013Z.json` | `summarize.py` on that run |
| `yaml-crosscheck-2026-10-22.json` | The standard-library YAML reader against PyYAML on all 240 sampled files: 239 agree on every field the checker reads, 1 file is invalid YAML for both |
| `run-20261022T075730Z.json`, `summary-20261022T075730Z.json` | Superseded: made before the YAML reader rejected a key inside a multi-line value, so one invalid file (sendbird/sendbird-platform-sdk-typescript) was read as an entry. 218 entries, 21 drift |

Runs that left no file, kept here so the history is complete:

- 07:31 UTC: stopped after about 100 entries on a link that contained a space. The link checker now percent-encodes such links, and an error in one entry no longer stops the run.
- 07:42 UTC: finished, but printed owner names that were not marked as users (one of them was a person's login, one an email address) and counted a user ID from another directory as a missing GitHub account. Its file was deleted because of the personal data. Owner and team names are now hashed, and a missing account is a warning unless you pass `--users-are-github-logins`.
- 07:59 and 08:00 UTC: same counts as the quoted run. The 08:00 run also had the fix for GitHub's team error message (see the write-up). Both were replaced when the link checker moved to pinned public addresses; the link cache was cleared so the quoted run checked every link again with the new code. The counts did not change.

Every run file carries `code_sha256` (the checker's source) and `input_sha256` (the sample file).
