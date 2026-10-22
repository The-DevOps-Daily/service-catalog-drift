import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import drift  # noqa: E402
from fake_world import NOW, ORG, FakeGitHub, FakeLinks, key  # noqa: E402

FIXTURES = [os.path.join(ROOT, "fixtures", "catalog-info.yaml"),
            os.path.join(ROOT, "fixtures", "template", "catalog-info.yaml")]


def run_fixture(redact=False, world=None, users_are_logins=False):
    gh = FakeGitHub(world)
    checker = drift.Checker(gh, FakeLinks(gh), redact=redact, now=NOW, users_are_logins=users_are_logins)
    entries, counts, _ = drift.run(drift.load_local(FIXTURES, None), checker, {"component"})
    return {e["name"]: e for e in entries}, counts, gh


class FixtureCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e, cls.counts, cls.gh = run_fixture()

    def status(self, name, check=None):
        e = self.e[name]
        return e["status"] if check is None else e["checks"][check]["status"]

    def test_counts(self):
        self.assertEqual(len(self.e), 6)
        self.assertEqual(self.counts["templates"], 1)
        self.assertEqual(self.counts["other_kinds"], 2)  # the Group and the API are not checked

    def test_healthy_entry(self):
        self.assertEqual(self.status("payments-api"), "ok")
        self.assertEqual(self.status("payments-api", "owner"), "ok")
        self.assertEqual(self.status("payments-api", "codeowners"), "ok")
        # one link answers, the dashboard cannot be checked from outside: not drift
        self.assertEqual(self.status("payments-api", "links"), "ok")
        self.assertIn("1 could not be checked", self.e["payments-api"]["checks"]["links"]["reason"])

    def test_deleted_team(self):
        # From outside, an unresolvable team is a warning on the owner (it may only lack
        # write access) and drift on the CODEOWNERS file, which GitHub says is broken.
        self.assertEqual(self.status("billing-worker", "owner"), "warn")
        self.assertEqual(self.status("billing-worker", "codeowners"), "drift")
        self.assertEqual(self.status("billing-worker"), "drift")

    def test_departed_user_and_archived_repo(self):
        # without knowing that user: names are GitHub logins, a missing account is only a warning
        self.assertEqual(self.status("legacy-reports", "owner"), "warn")
        self.assertEqual(self.status("legacy-reports", "repo"), "drift")
        self.assertIn("archived", self.e["legacy-reports"]["checks"]["repo"]["reason"])

    def test_renamed_repo_dead_runbook_and_disagreement(self):
        e = self.e["search-indexer"]
        self.assertEqual(e["checks"]["repo"]["status"], "drift")
        self.assertIn("moved to acme-fixture/search", e["checks"]["repo"]["reason"])
        self.assertEqual(e["checks"]["links"]["status"], "drift")
        self.assertIn("1 of 2 links dead", e["checks"]["links"]["reason"])
        # the catalog and CODEOWNERS disagree: a warning, not proof
        self.assertEqual(e["checks"]["codeowners"]["status"], "warn")
        self.assertEqual(e["checks"]["codeowners"]["disagreement"], "different team names")
        self.assertEqual(e["checks"]["owner"]["status"], "unverifiable")

    def test_unknown_owner_kinds(self):
        c = self.e["billing-worker"]["checks"]["codeowners"]
        # only the team GitHub names is unknown, not every owner on the line
        self.assertEqual((c["unknown_teams"], c["unknown_users"]), (1, 0))
        self.assertNotIn("sre", c["reason"])

    def test_unknown_owner_messages(self):
        team = "Unknown owner on line 1: make sure the team @a/b-c exists, is publicly visible, and has write access"
        user = "Unknown owner on line 4: make sure @someone exists and has write access to the repository"
        self.assertEqual(drift.UNKNOWN_OWNER.findall(team), ["@a/b-c"])
        self.assertEqual(drift.UNKNOWN_OWNER.findall(user), ["@someone"])

    def test_idle_experiment_and_bot_commits(self):
        e = self.e["feature-flags"]
        self.assertEqual(e["status"], "warn")
        self.assertEqual(e["checks"]["lifecycle"]["status"], "warn")
        # a Renovate commit two days ago does not count as activity
        self.assertIn("no human commit", e["checks"]["repo"]["reason"])
        # the team is found in another repository's CODEOWNERS and GitHub resolves it there
        self.assertEqual(e["checks"]["owner"]["status"], "ok")
        self.assertIn("acme-fixture/infra", e["checks"]["owner"]["reason"])

    def test_archived_and_deprecated_is_honest(self):
        self.assertEqual(self.status("old-cli"), "ok")

    def test_team_search_is_cached(self):
        searches = [k for k in self.gh.log if k.startswith("/search/code") and "platform" in k]
        self.assertEqual(len(searches), 1)

    def test_only_reads(self):
        # FakeGitHub only has get(); nothing else could have been called
        self.assertTrue(all(isinstance(k, str) for k in self.gh.log))


class Redaction(unittest.TestCase):
    def test_no_user_login_in_output(self):
        e, _, _ = run_fixture(redact=True)
        text = json.dumps(e)
        self.assertNotIn("former-engineer", text)
        self.assertEqual(e["legacy-reports"]["owner"], "user:<redacted>")
        self.assertNotIn("grafana.acme-fixture.example/d/payments", text)
        # owner and team names are hashed too: a bare owner name can be a person's login
        self.assertNotIn("group:platform", text)
        self.assertNotIn("acme-fixture/discovery", text)
        self.assertRegex(e["feature-flags"]["owner"], r"^group:<[0-9a-f]{6}>$")
        # the same name gets the same placeholder within one run
        self.assertEqual(e["feature-flags"]["owner"], e["old-cli"]["owner"])

    def test_emails_in_owner(self):
        r = drift.Redactor(True)
        self.assertEqual(r.owner("someone@example.com"), "<email>")
        self.assertEqual(r.codeowner("someone@example.com"), "<email>")
        self.assertEqual(r.codeowner("@someone"), "@<user>")


class UsersAreLogins(unittest.TestCase):
    def test_missing_account_is_drift_when_declared(self):
        e, _, _ = run_fixture(users_are_logins=True)
        self.assertEqual(e["legacy-reports"]["checks"]["owner"]["status"], "drift")


class FullAccessMode(unittest.TestCase):
    """With a token that can see the org's teams, membership is checked directly."""

    def world(self):
        from fake_world import WORLD
        w = dict(WORLD)
        w[key(f"/orgs/{ORG}/teams", {"per_page": 1})] = (200, [{"slug": "payments"}])
        w[key(f"/orgs/{ORG}/teams/payments")] = (200, {"slug": "payments"})
        w[key(f"/orgs/{ORG}/teams/payments/members", {"per_page": 1})] = (200, [{"login": "x"}])
        w[key(f"/orgs/{ORG}/teams/platform")] = (200, {"slug": "platform"})
        w[key(f"/orgs/{ORG}/teams/platform/members", {"per_page": 1})] = (200, [])
        w[key(f"/repos/{ORG}/legacy-reports")] = (404, {"message": "Not Found"})
        return w

    def test_full_mode(self):
        e, _, gh = run_fixture(world=self.world())
        self.assertEqual(e["payments-api"]["checks"]["owner"]["status"], "ok")
        self.assertEqual(e["billing-worker"]["checks"]["owner"]["status"], "drift")
        self.assertIn("no team", e["billing-worker"]["checks"]["owner"]["reason"])
        self.assertIn("no members", e["feature-flags"]["checks"]["owner"]["reason"])
        # seeing the org's teams does not mean seeing every private repository: a 404 is a warning
        self.assertEqual(e["legacy-reports"]["checks"]["repo"]["status"], "warn")
        self.assertIn("not found with this token", e["legacy-reports"]["checks"]["repo"]["reason"])
        self.assertFalse(any(k.startswith("/search/code") for k in gh.log))


class CopiedCatalogFile(unittest.TestCase):
    def test_root_file_pointing_at_another_repo_warns(self):
        gh = FakeGitHub()
        checker = drift.Checker(gh, FakeLinks(gh), now=NOW)
        entries, _, _ = drift.run(drift.load_local(FIXTURES[:1], f"{ORG}/service-template", root=os.path.join(ROOT, "fixtures")), checker, {"component"})
        e = {x["name"]: x for x in entries}
        self.assertEqual(e["payments-api"]["checks"]["repo"]["status"], "warn")
        self.assertIn("points at acme-fixture/payments-api", e["payments-api"]["checks"]["repo"]["reason"])
        # a rename is still reported as a move, not as a copy
        self.assertIn("moved to", e["search-indexer"]["checks"]["repo"]["reason"])


class CoverageAndExitCodes(unittest.TestCase):
    def test_exit_codes(self):
        drift_entry = {"status": "drift"}
        ok_entry = {"status": "ok"}
        self.assertEqual(drift.exit_code([ok_entry], {"unparsed": 0}), 0)
        self.assertEqual(drift.exit_code([drift_entry], {}), 1)
        self.assertEqual(drift.exit_code([ok_entry], {"unparsed": 1}), 1)  # a file that does not parse fails CI
        self.assertEqual(drift.exit_code([ok_entry], {"errors": 1}), 2)    # an entry that could not be checked
        self.assertEqual(drift.exit_code([], {}), 2)                       # nothing was checked at all

    def test_unreadable_validation_never_says_ok(self):
        from fake_world import WORLD
        w = dict(WORLD)
        w[key(f"/repos/{ORG}/payments-api/codeowners/errors", {"ref": "main"})] = (500, {})
        e, _, _ = run_fixture(world=w)
        self.assertEqual(e["payments-api"]["checks"]["codeowners"]["status"], "unverifiable")

    def test_invalid_slug_is_drift_but_the_file_is_still_checked(self):
        gh = FakeGitHub()
        c = drift.Checker(gh, None, now=NOW)
        ent = {"kind": "Component", "metadata": {"name": "payments-api",
                                                 "annotations": {"github.com/project-slug": "payments-api"}},
               "spec": {"owner": "group:payments", "lifecycle": "production"}}
        e = c.check_entity(ent, {"repo": f"{ORG}/payments-api", "path": "catalog-info.yaml"})
        self.assertEqual(e["checks"]["repo"]["status"], "drift")
        self.assertEqual(e["checks"]["codeowners"]["status"], "ok")  # CODEOWNERS read from the file's repository
        self.assertEqual(e["checks"]["lifecycle"]["status"], "ok")

    def test_groups_in_other_namespaces_are_not_mapped(self):
        gh = FakeGitHub()
        c = drift.Checker(gh, None, now=NOW)
        self.assertEqual(c.check_owner("group:hr-directory/payments", ORG, None)["status"], "unverifiable")


class PublicMode404(unittest.TestCase):
    def test_404_without_access_is_unverifiable(self):
        from fake_world import WORLD
        w = dict(WORLD)
        w[key(f"/repos/{ORG}/legacy-reports")] = (404, {"message": "Not Found"})
        e, _, _ = run_fixture(world=w)
        self.assertEqual(e["legacy-reports"]["checks"]["repo"]["status"], "unverifiable")


class ExitCode(unittest.TestCase):
    def test_exit_code_and_report(self):
        gh = FakeGitHub()
        checker = drift.Checker(gh, FakeLinks(gh), now=NOW)
        entries, counts, _ = drift.run(drift.load_local(FIXTURES, None), checker, {"component"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            drift.print_report(entries, counts)
        out = buf.getvalue()
        self.assertIn("6 entries from 1 files", out)
        self.assertIn("drift 3 (50.0%)", out)


class RepoSlug(unittest.TestCase):
    def test_sources(self):
        self.assertEqual(drift.repo_slug({"github.com/project-slug": "a/b"}, None), ("a/b", "github.com/project-slug"))
        self.assertEqual(drift.repo_slug({"backstage.io/source-location": "url:https://github.com/a/b/tree/main/"}, None)[0], "a/b")
        self.assertEqual(drift.repo_slug({}, "x/y"), ("x/y", "file location"))
        # an explicit annotation that is not owner/repo is reported, not replaced by the file's repository
        self.assertEqual(drift.repo_slug({"github.com/project-slug": "${{ values.x }}"}, "x/y"), (None, "invalid"))
        # source-location must really be on github.com
        self.assertEqual(drift.repo_slug({"backstage.io/source-location": "url:https://evil.example/github.com/a/b"},
                                         None), (None, None))

    def test_link_kinds(self):
        self.assertEqual(drift.link_kind("On-call runbook", "https://x"), "runbook")
        self.assertEqual(drift.link_kind("Metrics", "https://grafana.x/d/1"), "dashboard")
        self.assertEqual(drift.link_kind("Docs", "https://x"), "docs")
        self.assertEqual(drift.link_kind("Slack", "https://acme.slack.com/archives/C1"), "chat")
        # the hostname does not decide the kind: a chat link on a grafana host is not a dashboard
        self.assertEqual(drift.link_kind("Community Slack Channel", "https://slack.grafana.com/"), "chat")
        self.assertEqual(drift.link_kind("Home", "https://grafana.example.com/"), "other")


if __name__ == "__main__":
    unittest.main()
