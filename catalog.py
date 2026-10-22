"""Parse Backstage catalog files and CODEOWNERS with the standard library only.

The YAML reader covers what catalog-info.yaml files use: block mappings and
sequences, flow lists and maps on one line, quoted and plain scalars, block
scalars (| and >), comments and multiple documents. It does not support
anchors, aliases or tags, and raises YamlError on them so that a file is
reported as unparsed instead of read wrongly.
"""

import re


class YamlError(ValueError):
    pass


# ---------------------------------------------------------------- YAML subset

def strip_comment(line):
    if line.lstrip().startswith("#"):
        return ""
    quote = None
    prev = " "
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if quote == "'" and c == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and c == "\\":
                i += 2
                continue
            elif c == quote:
                quote = None
        else:
            if c in "'\"" and prev in " \t[{,:-":
                quote = c
            elif c == "#" and prev in " \t":
                return line[:i].rstrip()
        prev = c
        i += 1
    return line.rstrip()


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def split_documents(text):
    docs, cur = [], []
    for raw in text.replace("\r\n", "\n").replace("\t", "    ").split("\n"):
        if re.match(r"^---(\s|$)", raw):
            docs.append(cur)
            rest = raw[3:].strip()
            cur = [rest] if rest and not rest.startswith("#") else []
        elif re.match(r"^\.\.\.(\s|$)", raw):
            docs.append(cur)
            cur = []
        elif raw.startswith("%"):
            continue
        else:
            cur.append(raw)
    docs.append(cur)
    return docs


def parse_yaml_documents(text):
    out = []
    for lines in split_documents(text):
        p = _Parser(lines)
        value = p.parse()
        if value is not None:
            out.append(value)
    return out


_KEY_RE = re.compile(r"""^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^'"\s#][^#]*?))\s*:(?:\s+|$)""")


def _split_key(text):
    m = _KEY_RE.match(text)
    if not m:
        return None
    if m.group(1) is not None:
        key = _unescape_double(m.group(1))
    elif m.group(2) is not None:
        key = m.group(2).replace("''", "'")
    else:
        key = m.group(3).strip()
        if key.startswith(("[", "{", "&", "*", "!", "|", ">")):
            return None
    return key, text[m.end():]


def _unescape_double(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "u" and i + 5 < len(s):
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/", "r": "\r", "0": "\0", " ": " "}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def plain_scalar(s):
    s = s.strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
        return float(s)
    if s[0] in "&*!":
        raise YamlError(f"anchors, aliases and tags are not supported: {s[:30]}")
    return s


class _Flow:
    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\n":
            self.i += 1

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            raise YamlError("unexpected end of flow value")
        c = self.s[self.i]
        if c == "[":
            self.i += 1
            items = []
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == "]":
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws()
                if self.s[self.i:self.i + 1] == ",":
                    self.i += 1
                elif self.s[self.i:self.i + 1] != "]":
                    raise YamlError("bad flow sequence")
        if c == "{":
            self.i += 1
            m = {}
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == "}":
                    self.i += 1
                    return m
                k = self.scalar(stop=":,}")
                self.ws()
                if self.s[self.i:self.i + 1] == ":":
                    self.i += 1
                    v = self.value()
                else:
                    v = None
                m[k] = v
                self.ws()
                if self.s[self.i:self.i + 1] == ",":
                    self.i += 1
                elif self.s[self.i:self.i + 1] != "}":
                    raise YamlError("bad flow mapping")
        return self.scalar(stop=",]}")

    def scalar(self, stop):
        self.ws()
        c = self.s[self.i:self.i + 1]
        if c == '"':
            j = self.i + 1
            while j < len(self.s) and self.s[j] != '"':
                j += 2 if self.s[j] == "\\" else 1
            raw = self.s[self.i + 1:j]
            self.i = j + 1
            return _unescape_double(raw)
        if c == "'":
            j = self.i + 1
            while True:
                j = self.s.find("'", j)
                if j < 0:
                    raise YamlError("unterminated quote")
                if self.s[j + 1:j + 2] == "'":
                    j += 2
                    continue
                break
            raw = self.s[self.i + 1:j]
            self.i = j + 1
            return raw.replace("''", "'")
        j = self.i
        while j < len(self.s):
            ch = self.s[j]
            if ch in ",]}":
                break
            if ch == ":" and ":" in stop and (j + 1 >= len(self.s) or self.s[j + 1] in " ,}"):
                break
            j += 1
        raw = self.s[self.i:j]
        self.i = j
        return plain_scalar(raw)


def parse_flow(s):
    f = _Flow(s)
    v = f.value()
    f.ws()
    if f.i != len(f.s):
        raise YamlError(f"trailing text after flow value: {s[f.i:f.i + 20]}")
    return v


def parse_inline(s):
    s = s.strip()
    if not s:
        return None
    if s[0] in "[{":
        return parse_flow(s)
    if s[0] == '"':
        if len(s) < 2 or not s.endswith('"'):
            raise YamlError("unterminated double quote")
        return _unescape_double(s[1:-1])
    if s[0] == "'":
        if len(s) < 2 or not s.endswith("'"):
            raise YamlError("unterminated single quote")
        return s[1:-1].replace("''", "'")
    return plain_scalar(s)


class _Parser:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def _next(self):
        """Index of the next line with content, or None."""
        j = self.i
        while j < len(self.lines):
            if strip_comment(self.lines[j]).strip():
                return j
            j += 1
        return None

    def parse(self):
        j = self._next()
        if j is None:
            return None
        self.i = j
        value = self.block(indent_of(self.lines[j]))
        if self._next() is not None:
            raise YamlError(f"unexpected content at line {self._next() + 1}")
        return value

    def block(self, indent):
        j = self._next()
        if j is None:
            return None
        self.i = j
        text = strip_comment(self.lines[j])[indent:]
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        if _split_key(text) is not None:
            return self.mapping(indent)
        return self.multiline_scalar(indent)

    def multiline_scalar(self, indent):
        parts = []
        while True:
            j = self._next()
            if j is None or indent_of(self.lines[j]) < indent:
                break
            parts.append(strip_comment(self.lines[j]).strip())
            self.i = j + 1
        return parse_inline(" ".join(parts))

    def mapping(self, indent):
        result = {}
        while True:
            j = self._next()
            if j is None:
                break
            line = strip_comment(self.lines[j])
            ind = indent_of(line)
            if ind < indent:
                break
            if ind > indent:
                raise YamlError(f"bad indentation at line {j + 1}")
            text = line[indent:]
            if text == "-" or text.startswith("- "):
                break
            kv = _split_key(text)
            if kv is None:
                raise YamlError(f"expected a key at line {j + 1}: {text[:40]}")
            key, rest = kv
            self.i = j + 1
            result[key] = self.value_after_key(indent, rest)
        return result

    def value_after_key(self, indent, rest):
        rest = rest.strip()
        if rest.startswith(("|", ">")):
            return self.block_scalar(indent, rest)
        if rest:
            if rest[0] in "&*!":
                raise YamlError(f"anchors, aliases and tags are not supported: {rest[:30]}")
            return self.inline_with_continuation(indent, rest)
        j = self._next()
        if j is None:
            return None
        nind = indent_of(self.lines[j])
        ntext = strip_comment(self.lines[j])[nind:]
        if nind > indent:
            return self.block(nind)
        if nind == indent and (ntext == "-" or ntext.startswith("- ")):
            return self.sequence(indent)
        return None

    def inline_with_continuation(self, indent, rest):
        # A quoted or plain scalar may continue on more-indented lines.
        if rest[0] in "[{" and not _balanced(rest):
            parts = [rest]
            while not _balanced(" ".join(parts)):
                j = self._next()
                if j is None:
                    raise YamlError("unterminated flow value")
                parts.append(strip_comment(self.lines[j]).strip())
                self.i = j + 1
            return parse_flow(" ".join(parts))
        if rest[0] in "\"'" and not _quote_closed(rest):
            parts = [rest]
            while not _quote_closed(" ".join(parts)):
                if self.i >= len(self.lines):
                    raise YamlError("unterminated quoted scalar")
                parts.append(self.lines[self.i].strip())
                self.i += 1
            return parse_inline(" ".join(p for p in parts if p))
        parts = [rest]
        if rest[0] not in "\"'[{":
            while True:
                j = self._next()
                if j is None:
                    break
                line = strip_comment(self.lines[j])
                if indent_of(line) <= indent:
                    break
                if _split_key(line.strip()) is not None:
                    # YAML does not allow a key inside a multi-line plain scalar
                    raise YamlError(f"mapping values are not allowed here (line {j + 1})")
                parts.append(line.strip())
                self.i = j + 1
        return parse_inline(" ".join(parts))

    def block_scalar(self, indent, header):
        m = re.match(r"^([|>])([+-]?)(\d?)([+-]?)\s*$", header)
        if not m:
            raise YamlError(f"bad block scalar header: {header}")
        style = m.group(1)
        chomp = m.group(2) or m.group(4)
        raw = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and indent_of(line) <= indent:
                break
            raw.append(line)
            self.i += 1
        content = [ln for ln in raw if ln.strip()]
        if not content:
            return ""
        base = int(m.group(3)) + indent if m.group(3) else min(indent_of(ln) for ln in content)
        body = [ln[base:] if ln.strip() else "" for ln in raw]
        while body and body[-1] == "":
            body.pop()
        trailing = len(raw) - len(body)
        if style == "|":
            text = "\n".join(body)
        else:
            text, prev_blank, prev_more = "", False, False
            for k, ln in enumerate(body):
                if ln == "":
                    text += "\n"
                    prev_blank = True
                    continue
                more = ln.startswith(" ")
                if k == 0 or prev_blank:
                    text += ln
                elif more or prev_more:
                    text += "\n" + ln
                else:
                    text += " " + ln
                prev_blank, prev_more = False, more
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"

    def sequence(self, indent):
        items = []
        while True:
            j = self._next()
            if j is None:
                break
            line = strip_comment(self.lines[j])
            ind = indent_of(line)
            text = line[indent:] if ind >= indent else ""
            if ind != indent or not (text == "-" or text.startswith("- ")):
                break
            content = text[1:].strip()
            if not content:
                self.i = j + 1
                nj = self._next()
                if nj is not None and indent_of(self.lines[nj]) > indent:
                    items.append(self.block(indent_of(self.lines[nj])))
                else:
                    items.append(None)
                continue
            col = indent + 1 + (len(text[1:]) - len(text[1:].lstrip()))
            if content.startswith("- ") or _split_key(content) is not None:
                # Re-read this line as the first line of a nested block.
                self.lines = self.lines[:j] + [" " * col + self.lines[j][col:]] + self.lines[j + 1:]
                self.i = j
                items.append(self.block(col))
            else:
                self.i = j + 1
                items.append(self.inline_with_continuation(indent, content))
        return items


def _balanced(s):
    depth, quote = 0, None
    for c in s:
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
    return depth <= 0


def _quote_closed(s):
    q = s[0]
    i = 1
    while i < len(s):
        if q == '"' and s[i] == "\\":
            i += 2
            continue
        if s[i] == q:
            if q == "'" and s[i + 1:i + 2] == "'":
                i += 2
                continue
            return True
        i += 1
    return False


# ------------------------------------------------------------ catalog entities

def entities(text):
    """Return (entities, error). Each entity is a dict from one YAML document."""
    try:
        docs = parse_yaml_documents(text)
    except (YamlError, IndexError, ValueError) as e:
        return [], str(e)
    return [d for d in docs if isinstance(d, dict) and d.get("kind")], None


def parse_ref(ref, default_kind="group"):
    """Split a Backstage entity reference: [kind:][namespace/]name."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    kind = default_kind
    if ":" in ref and not ref.startswith(("http://", "https://")):
        kind, ref = ref.split(":", 1)
    namespace = "default"
    if "/" in ref:
        namespace, ref = ref.split("/", 1)
    return {"kind": kind.lower(), "namespace": namespace, "name": ref}


GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def is_github_login(name):
    return bool(GITHUB_LOGIN.match(name or ""))


# ------------------------------------------------------------------ CODEOWNERS

CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def parse_codeowners(text):
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s#", line, maxsplit=1)[0].strip()
        parts = line.split()
        pattern, owners = parts[0], parts[1:]
        rules.append({"line": n, "pattern": pattern, "owners": owners})
    return rules


def _pattern_regex(pattern):
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    rx = ""
    i = 0
    while i < len(p):
        if p[i:i + 3] == "**/":
            rx += "(?:.*/)?"
            i += 3
        elif p[i:i + 2] == "**":
            rx += ".*"
            i += 2
        elif p[i] == "*":
            rx += "[^/]*"
            i += 1
        elif p[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(p[i])
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if dir_only else "(?:/.*)?$"
    return re.compile(prefix + rx + suffix)


def owners_for(rules, path):
    """Owners of the last rule that matches path (GitHub's last-match-wins)."""
    match = None
    for r in rules:
        if r["pattern"] == "*" or _pattern_regex(r["pattern"]).match(path):
            match = r
    return match


__all__ = [
    "YamlError", "parse_yaml_documents", "entities", "parse_ref", "is_github_login",
    "CODEOWNERS_PATHS", "parse_codeowners", "owners_for",
]
