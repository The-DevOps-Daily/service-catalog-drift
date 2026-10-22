#!/usr/bin/env python3
"""Method check: does the standard-library YAML reader agree with PyYAML?

  pip install pyyaml && python3 crosscheck_yaml.py sample/sample-<date>.json

Reads every sampled file (from the cache when drift.py already fetched it) and
compares the fields drift.py uses. Prints and writes results/yaml-crosscheck-<date>.json.
"""

import base64
import json
import os
import sys

import yaml

import catalog
from drift import valid_slug
from ghapi import GitHub
from links import quote_path

FIELDS = (("kind",), ("metadata", "name"), ("metadata", "links"), ("metadata", "annotations"),
          ("spec", "owner"), ("spec", "lifecycle"), ("spec", "type"))


def pick(doc, path):
    for k in path:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


def view(docs):
    return [tuple(json.dumps(pick(d, f), sort_keys=True, default=str) for f in FIELDS)
            for d in docs if isinstance(d, dict) and d.get("kind")]


def main():
    sample_path = sys.argv[1]
    with open(sample_path) as f:
        sample = json.load(f)
    gh = GitHub()
    agree, differ, ours_failed, both_failed, pyyaml_failed = [], [], [], [], []
    for s in sample["files"]:
        quoted = quote_path(s["path"])
        if not valid_slug(s["repo"]) or quoted is None:
            continue
        r = gh.get(f"/repos/{s['repo']}/contents/{quoted}")
        if r.status != 200:
            continue
        text = base64.b64decode(r.json()["content"]).decode("utf-8", "replace")
        name = f"{s['repo']}:{s['path']}"
        try:
            theirs = view(list(yaml.safe_load_all(text)))
        except yaml.YAMLError:
            theirs = None
        try:
            ours = view(catalog.parse_yaml_documents(text))
        except (catalog.YamlError, ValueError, IndexError):
            ours = None
        if theirs is None and ours is None:
            both_failed.append(name)
        elif theirs is None:
            pyyaml_failed.append(name)
        elif ours is None:
            ours_failed.append(name)
        elif ours == theirs:
            agree.append(name)
        else:
            differ.append(name)
    out = {"sample": os.path.basename(sample_path), "files": len(sample["files"]),
           "agree": len(agree), "differ": differ, "ours_failed": ours_failed,
           "pyyaml_failed": pyyaml_failed, "both_failed": both_failed,
           "fields": [".".join(f) for f in FIELDS]}
    print(json.dumps({k: (len(v) if isinstance(v, list) and k != "fields" else v) for k, v in out.items()}, indent=1))
    for k in ("differ", "ours_failed", "pyyaml_failed", "both_failed"):
        for n in out[k]:
            print(f"  {k}: {n}")
    here = os.path.dirname(os.path.abspath(__file__))
    day = sample_path.rsplit("sample-", 1)[-1].removesuffix(".json")
    with open(os.path.join(here, "results", f"yaml-crosscheck-{day}.json"), "w") as f:
        json.dump(out, f, indent=1)
    return 1 if differ or ours_failed else 0


if __name__ == "__main__":
    sys.exit(main())
