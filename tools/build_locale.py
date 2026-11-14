#!/usr/bin/env python3
"""Extract UI strings and compile translations (v0.1.10). No gettext tools needed.

  tools/build_locale.py extract   # scan extension/*.js for _('...') -> extension/locale/camtone.pot,
                                  # and add any missing msgid to each extension/locale/<lang>/LC_MESSAGES/*.po
  tools/build_locale.py compile   # .po -> .mo next to it (what GNOME Shell loads)

Source language is English. Translators edit the .po files only.
"""
from __future__ import annotations

import ast
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
LOCALE = EXT / "locale"
DOMAIN = "camtone"
CALL = re.compile(r"""\b_\((['"])((?:\\.|(?!\1).)*)\1\)""")


def js_unescape(s: str) -> str:
    return ast.literal_eval('"' + s.replace('"', '\\"').replace("\\'", "'") + '"')


def extract() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for js in sorted(EXT.glob("*.js")):
        for n, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            for m in CALL.finditer(line):
                found.setdefault(js_unescape(m.group(2)), []).append(f"{js.name}:{n}")
    return found


def po_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def parse_po(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    msgid = msgstr = None
    cur = None
    def flush():
        if msgid is not None and msgstr is not None:
            entries[msgid] = msgstr
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("msgid "):
            flush(); msgid, msgstr, cur = ast.literal_eval(line[6:]), None, "id"
        elif line.startswith("msgstr "):
            msgstr, cur = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"'):
            val = ast.literal_eval(line)
            if cur == "id":
                msgid += val
            elif cur == "str":
                msgstr += val
    flush()
    return entries


HEADER = (
    'Project-Id-Version: camtone\\n'
    'Language: {lang}\\n'
    'MIME-Version: 1.0\\n'
    'Content-Type: text/plain; charset=UTF-8\\n'
    'Content-Transfer-Encoding: 8bit\\n'
)


def write_po(path: Path, strings: dict[str, list[str]], trans: dict[str, str], lang: str | None) -> None:
    out = ['msgid ""', 'msgstr ""']
    for part in HEADER.format(lang=lang or "").split("\\n"):
        if part:
            out.append(f'"{part}\\n"')
    out.append("")
    for msgid, refs in strings.items():
        out.append("#: " + " ".join(refs))
        out.append(f"msgid {po_quote(msgid)}")
        out.append(f"msgstr {po_quote(trans.get(msgid, '') if lang else '')}")
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


def cmd_extract() -> None:
    strings = extract()
    write_po(LOCALE / f"{DOMAIN}.pot", strings, {}, None)
    for po in sorted(LOCALE.glob("*/LC_MESSAGES/*.po")):
        lang = po.parent.parent.name
        write_po(po, strings, parse_po(po), lang)  # drops obsolete ids, adds new ones (empty msgstr)
        missing = [k for k in strings if not parse_po(po).get(k)]
        print(f"{po.relative_to(ROOT)}: {len(strings)} strings, {len(missing)} untranslated")
    print(f"{len(strings)} strings found")


def write_mo(path: Path, entries: dict[str, str]) -> None:
    items = sorted((k, v) for k, v in entries.items() if v or k == "")
    ids = b"\0".join(k.encode() for k, _ in items) + b"\0"
    strs = b"\0".join(v.encode() for _, v in items) + b"\0"
    n = len(items)
    keystart = 28 + n * 16
    valstart = keystart + len(ids)
    koffsets, voffsets, pos = [], [], 0
    for k, _ in items:
        koffsets.append((len(k.encode()), keystart + pos)); pos += len(k.encode()) + 1
    pos = 0
    for _, v in items:
        voffsets.append((len(v.encode()), valstart + pos)); pos += len(v.encode()) + 1
    data = struct.pack("<Iiiiiii", 0x950412DE, 0, n, 28, 28 + n * 8, 0, 0)
    for ln, off in koffsets:
        data += struct.pack("<II", ln, off)
    for ln, off in voffsets:
        data += struct.pack("<II", ln, off)
    path.write_bytes(data + ids + strs)


def cmd_compile() -> None:
    for po in sorted(LOCALE.glob("*/LC_MESSAGES/*.po")):
        entries = parse_po(po)
        mo = po.with_suffix(".mo")
        write_mo(mo, entries)
        print(f"{mo.relative_to(ROOT)}: {sum(1 for v in entries.values() if v)} translated")


if __name__ == "__main__":
    {"extract": cmd_extract, "compile": cmd_compile}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
