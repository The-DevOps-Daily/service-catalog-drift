import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import catalog  # noqa: E402

try:
    import yaml  # only for the cross-check; the checker itself does not need it
except ImportError:
    yaml = None


DOC = """\
# leading comment
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: 'pay-ments'   # quoted, with a comment
  title: "It's \\"quoted\\""
  description: >-
    A folded
    description: with a colon

    and a second paragraph
  tags: [python, 'web app', "api"]
  links:
    - url: https://example.com/runbook#section
      title: Runbook # not part of the title
      icon: dashboard
    -   url: https://example.com/a
        title: indented more
    - {url: 'https://example.com/b', title: flow map}
  annotations:
    github.com/project-slug: acme/pay-ments
    "quoted.key/x": value
    backstage.io/techdocs-ref: dir:.
spec:
  type: service
  owner: group:default/payments
  lifecycle: production
  system:
  dependsOn:
  - resource:default/db
  - component:auth
  providesApis: []
  notes: |
    line one
      indented # kept
    line three
---
kind: Group
metadata: {name: payments, namespace: default}
...
"""


class Yaml(unittest.TestCase):
    def test_document(self):
        docs = catalog.parse_yaml_documents(DOC)
        self.assertEqual(len(docs), 2)
        d = docs[0]
        self.assertEqual(d["metadata"]["name"], "pay-ments")
        self.assertEqual(d["metadata"]["title"], 'It\'s "quoted"')
        self.assertEqual(d["metadata"]["links"][0]["title"], "Runbook")
        self.assertEqual(d["metadata"]["links"][0]["url"], "https://example.com/runbook#section")
        self.assertEqual(d["metadata"]["links"][1]["title"], "indented more")
        self.assertEqual(d["metadata"]["links"][2]["url"], "https://example.com/b")
        self.assertEqual(d["metadata"]["annotations"]["quoted.key/x"], "value")
        self.assertIsNone(d["spec"]["system"])
        self.assertEqual(d["spec"]["dependsOn"], ["resource:default/db", "component:auth"])
        self.assertEqual(d["spec"]["notes"], "line one\n  indented # kept\nline three\n")
        self.assertEqual(docs[1], {"kind": "Group", "metadata": {"name": "payments", "namespace": "default"}})

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_matches_pyyaml(self):
        self.assertEqual(catalog.parse_yaml_documents(DOC), [d for d in yaml.safe_load_all(DOC) if d is not None])
        with open(os.path.join(ROOT, "fixtures", "catalog-info.yaml")) as f:
            text = f.read()
        self.assertEqual(catalog.parse_yaml_documents(text), [d for d in yaml.safe_load_all(text) if d is not None])

    def test_key_inside_plain_scalar_is_an_error(self):
        # A real file in the sample: description at the wrong level swallows annotations
        text = "kind: Component\nmetadata:\n  name: x\ndescription: y\n  annotations:\n    a: b\n"
        ents, err = catalog.entities(text)
        self.assertEqual(ents, [])
        self.assertIn("mapping values", err)
        self.assertEqual(catalog.parse_yaml_documents("a: foo\n  - bar\n"), [{"a": "foo - bar"}])
        self.assertEqual(catalog.parse_yaml_documents("a: foo\n  http://x.y/z\n"), [{"a": "foo http://x.y/z"}])

    def test_refuses_anchors(self):
        ents, err = catalog.entities("kind: Component\nmetadata: &m\n  name: x\nother: *m\n")
        self.assertEqual(ents, [])
        self.assertIn("not supported", err)

    def test_entities_skip_non_entities(self):
        ents, err = catalog.entities("---\n# empty\n---\nfoo: bar\n---\nkind: Component\nmetadata:\n  name: a\n")
        self.assertIsNone(err)
        self.assertEqual([e["metadata"]["name"] for e in ents], ["a"])


class Refs(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(catalog.parse_ref("group:default/k6-frontend"),
                         {"kind": "group", "namespace": "default", "name": "k6-frontend"})
        self.assertEqual(catalog.parse_ref("payments")["kind"], "group")
        self.assertEqual(catalog.parse_ref("user:jdoe")["kind"], "user")
        self.assertIsNone(catalog.parse_ref(""))

    def test_github_login(self):
        self.assertTrue(catalog.is_github_login("octo-cat1"))
        self.assertFalse(catalog.is_github_login("jane.doe"))
        self.assertFalse(catalog.is_github_login("-bad"))
        self.assertFalse(catalog.is_github_login("a--b"))
        self.assertFalse(catalog.is_github_login("x" * 40))


class Codeowners(unittest.TestCase):
    RULES = catalog.parse_codeowners("""\
# comment
*                 @acme/everyone
*.yaml            @acme/config   # trailing comment
/docs/            @acme/docs
apps/**/catalog-info.yaml @acme/apps
/catalog-info.yaml @acme/platform @someone
""")

    def owners(self, path):
        r = catalog.owners_for(self.RULES, path)
        return r["owners"] if r else None

    def test_last_match_wins(self):
        self.assertEqual(self.owners("catalog-info.yaml"), ["@acme/platform", "@someone"])
        self.assertEqual(self.owners("deploy/values.yaml"), ["@acme/config"])
        self.assertEqual(self.owners("docs/index.md"), ["@acme/docs"])
        self.assertEqual(self.owners("src/docs/x.md"), ["@acme/everyone"])  # /docs/ is anchored
        self.assertEqual(self.owners("apps/a/b/catalog-info.yaml"), ["@acme/apps"])
        self.assertEqual(self.owners("src/main.py"), ["@acme/everyone"])

    def test_comment_stripped(self):
        self.assertEqual(self.RULES[1]["owners"], ["@acme/config"])


if __name__ == "__main__":
    unittest.main()
