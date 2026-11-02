#!/usr/bin/env python3
"""Shrink a Lean source tree's gzip size without changing any elaborated term.

GitHub's Contents API (used by the LeanEval audit archiver) rejects the encrypted source
tarball somewhere between 29 and 42 MB, while gzip only sees repeats within 32 KB. In
generated certificate files the same long numerals repeat far apart. For each numeral in a
file's code (not in comments or strings) that repeats and either has 30+ digits or repeats
beyond gzip's window often enough to pay for its own line, this declares once, after the
imports,

    local notation:max (name := <unique>) "¤<k>" => <numeral>

and writes the token at each occurrence. A notation is a macro: the parser hands the
elaborator exactly the numeral syntax, so declarations, kernel terms and proofs are the
same. `--check` verifies that expanding the tokens gives back the original file byte for
byte, apart from the inserted notation lines.

usage: repack_numerals.py SRC_DIR OUT_DIR [--check]
"""
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

MIN_DIGITS = 12
WINDOW = 32768  # gzip back-reference window
MARK = "-- repack: numerals shared below are `local notation` macros (byte-identical terms)\n"
NUM = re.compile(r"\d+")
IDENT = re.compile(r"[\w.'!?⁰-₟ᵢ-ᵪ]")


def code_spans(text):
    """Yield (start, end) of code regions: outside comments, strings and char literals."""
    i, n, start = 0, len(text), 0
    while i < n:
        c = text[i]
        if text.startswith("--", i):
            yield start, i
            j = text.find("\n", i)
            i = n if j < 0 else j
            start = i
        elif text.startswith("/-", i):
            yield start, i
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/-", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("-/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            start = i
        elif c == '"':
            yield start, i
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            start = i
        else:
            i += 1
    yield start, n


def numerals(text):
    """(start, end, digits) of standalone numerals with MIN_DIGITS+ digits in code."""
    out = []
    for a, b in code_spans(text):
        for m in NUM.finditer(text, a, b):
            s, e = m.span()
            if e - s < MIN_DIGITS:
                continue
            before = text[s - 1] if s else " "
            after = text[e] if e < len(text) else " "
            if IDENT.match(before) or IDENT.match(after):
                continue  # part of an identifier, a decimal, a hex literal or a field access
            out.append((s, e, m.group()))
    return out


def header_end(text):
    """Offset just after the last top-of-file `import` line."""
    end = 0
    for m in re.finditer(r"^(?:public\s+)?import\s+[^\n]*\n", text, re.M):
        end = m.end()
    return end


def token(k):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        k, r = divmod(k, 36)
        s = digits[r] + s
        if not k:
            return "¤" + s


def rewrite(text, module):
    if "¤" in text:
        raise ValueError(f"{module}: already contains ¤")
    found = numerals(text)
    counts = Counter(d for _, _, d in found)
    last, far = {}, Counter()
    for s, _, d in found:
        if d in last and s - last[d] > WINDOW:
            far[d] += 1  # a repeat gzip cannot reference
        last[d] = s
    # compressed bytes saved by the far repeats, minus the notation line's own cost
    shared = [d for d, k in counts.items() if k >= 2 and (len(d) >= 30 or far[d] * (0.42 * len(d) - 3) > 0.42 * len(d) + 30)]
    if not shared:
        return text
    shared.sort(key=lambda d: -len(d) * counts[d])
    tok = {d: token(i) for i, d in enumerate(shared)}
    slug = "repack_" + re.sub(r"\W", "_", module)
    lines = [MARK] + [f'local notation:max (name := {slug}_{i}) "{tok[d]}" => {d}\n'
                      for i, d in enumerate(shared)]
    parts, last = [], 0
    for s, e, d in found:
        if d in tok:
            parts += [text[last:s], tok[d]]
            last = e
    parts.append(text[last:])
    body = "".join(parts)
    h = header_end(text)
    if h > min((s for s, _, d in found if d in tok), default=len(text)):
        raise ValueError(f"{module}: shared numeral inside the import header")
    return body[:h] + "".join(lines) + body[h:]


def expand(text):
    """Inverse of rewrite: drop notation lines, put the numerals back."""
    if MARK not in text:
        return text
    defs = dict(re.findall(r'^local notation:max \(name := [^)]*\) "(¤[0-9a-z]+)" => (\d+)\n', text, re.M))
    text = re.sub(r'^local notation:max \(name := [^)]*\) "¤[0-9a-z]+" => \d+\n', "", text, flags=re.M)
    text = text.replace(MARK, "", 1)
    return re.sub(r"¤[0-9a-z]+", lambda m: defs[m.group()], text)


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    check = "--check" in sys.argv
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, symlinks=True)
    changed = saved = 0
    for path in sorted(out.rglob("*.lean")):
        rel = path.relative_to(out)
        module = ".".join(rel.with_suffix("").parts)
        old = path.read_text()
        new = rewrite(old, module)
        if new != old:
            if check and expand(new) != old:
                raise SystemExit(f"round trip failed: {rel}")
            path.write_text(new)
            changed += 1
            saved += len(old.encode()) - len(new.encode())
    print(f"files rewritten: {changed}, raw bytes saved: {saved}")


if __name__ == "__main__":
    main()
