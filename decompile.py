#!/usr/bin/env python3
"""Decompile a Broforce install's Assembly-CSharp.dll into Source/ with ilspycmd."""

import argparse
import base64
import hashlib
import http.client
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ILSPY_VERSION = "11.2.0.10050"
ILSPY_PACKAGES = {
    "linux": (
        "ilspycmd.linux-x64",
        "SOBz+jnHm5QbmvYjcWQqI6Uw0MiVnE2O8vXZ+PaPyl72+8crifnO+lTqJp+rj6b/++z1QdQusCs1g/39lC7XSg==",
        "ilspycmd",
    ),
    "windows": (
        "ilspycmd.win-x64",
        "QITaa5f/zbGd+qX0LRLAUaEhPTChwGnCtQs53g6j5OjaxPGmRmNHj4Zs7us02ZTSbX6BGup73osr0TVwS+x+Dg==",
        "ilspycmd.exe",
    ),
}
NUGET_URL = "https://api.nuget.org/v3-flatcontainer/{id}/{version}/{id}.{version}.nupkg"

SOURCE_FLAGS = ["-p", "--nested-directories", "-lv", "CSharp7_3", "--ignore-decompilation-errors"]
IL_FLAGS = ["-il"]

REFERENCE_MVID = "4952e11f-34bd-45eb-9d34-a304cfbdda7b"
DLL_NAME = "Assembly-CSharp.dll"
STEAM_APP_DIR = "Broforce"
MANAGED_DIRS = ("Broforce_beta_Data/Managed", "Broforce_Data/Managed")

REPO_DIR = Path(__file__).resolve().parent
SOURCE_DIR = REPO_DIR / "Source"
LAYOUT_MAP = REPO_DIR / "layout-map.txt"
UNSORTED = "Unsorted"
MANIFEST = ".layout-manifest"
CSPROJ = "Assembly-CSharp.csproj"
PROPS = "Directory.Build.props"
PROJECT_GUID = "{3F2B6C41-8E0D-4A5F-9B27-C61D0E4A7B93}"

DOCS_URL = "https://raw.githubusercontent.com/alexneargarder/Broforce-Docs/HEAD/Assembly-CSharp.xml"
XML_NAME = "Assembly-CSharp.xml"

QUOTE = {'"': "&quot;"}
# Characters MSBuild would read as property, item or escape syntax in a path.
MSBUILD_ESCAPES = {c: "%%%02X" % ord(c) for c in "%$@;"}


class Fail(Exception):
    pass


def platform_key():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        raise Fail("macOS is unsupported: ilspycmd publishes no standalone macOS build.")
    if sys.platform.startswith("linux"):
        return "linux"
    raise Fail("unsupported platform: " + sys.platform)


def cache_dir(plat):
    if plat == "windows":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise Fail("LOCALAPPDATA is not set")
        return Path(base) / "broforce-source"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "broforce-source"


# --- Step 1: install discovery ---------------------------------------------

def steam_roots(plat):
    if plat == "windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam") as key:
                return [Path(winreg.QueryValueEx(key, "InstallPath")[0])]
        except OSError:
            return []
    home = Path.home()
    flatpak = home / ".var/app/com.valvesoftware.Steam"
    return [
        home / ".local/share/Steam",
        home / ".steam/steam",
        flatpak / ".local/share/Steam",
        flatpak / "data/Steam",
    ]


def library_folders(vdf_path):
    text = vdf_path.read_text(encoding="utf-8", errors="replace")
    paths = []
    for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
        paths.append(Path(m.group(1).replace("\\\\", "\\")))
    return paths


def find_install(plat):
    for root in steam_roots(plat):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        for lib in library_folders(vdf):
            game = lib / "steamapps" / "common" / STEAM_APP_DIR
            if game.is_dir():
                return game
        raise Fail("Broforce is in none of the Steam libraries %s lists; pass --game <install dir>" % vdf)
    raise Fail("no Steam library lists Broforce; pass --game <install dir>")


def find_managed(game):
    for rel in MANAGED_DIRS:
        managed = game / rel
        if (managed / DLL_NAME).is_file():
            return managed
    raise Fail("no %s under %s in %s" % (DLL_NAME, " or ".join(MANAGED_DIRS), game))


# --- Step 2: pinned ilspycmd ------------------------------------------------

def sha512_b64(path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode("ascii")


def fetch_package(plat, cache):
    package_id, pinned_hash, _ = ILSPY_PACKAGES[plat]
    cache.mkdir(parents=True, exist_ok=True)
    nupkg = cache / ("%s.%s.nupkg" % (package_id, ILSPY_VERSION))
    if not nupkg.is_file():
        url = NUGET_URL.format(id=package_id, version=ILSPY_VERSION)
        print("Downloading %s" % url, file=sys.stderr)
        part = nupkg.with_name(nupkg.name + ".part")
        try:
            with urllib.request.urlopen(url) as resp, open(part, "wb") as out:
                shutil.copyfileobj(resp, out)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise Fail("download of %s failed: %s" % (url, e))
        if sha512_b64(part) != pinned_hash:
            part.unlink()
            raise Fail("downloaded %s does not match its pinned SHA512" % url)
        os.replace(part, nupkg)
    elif sha512_b64(nupkg) != pinned_hash:
        raise Fail("cached %s does not match its pinned SHA512; delete it and rerun" % nupkg)
    return nupkg


def extract_tool(plat, nupkg, dest):
    package_id, _, exe_name = ILSPY_PACKAGES[plat]
    rid = package_id.split(".", 1)[1]
    member = "tools/any/%s/%s" % (rid, exe_name)
    with zipfile.ZipFile(nupkg) as z:
        names = [n for n in z.namelist() if n.startswith("tools/any/%s/" % rid)]
        if member not in names:
            raise Fail("%s holds no %s" % (nupkg, member))
        for name in names:
            z.extract(name, dest)
    exe = dest / member
    if plat != "windows":
        exe.chmod(0o755)
    return exe


# --- DLL metadata: MVID and base chains -----------------------------------

# Column widths: an int is a fixed width, "S"/"G"/"B" a heap index, a table number in a
# 1-tuple a row index, and a name a coded index (tag bits, tables).
CODED = {
    "TypeDefOrRef": (2, (0x02, 0x01, 0x1B)),
    "HasConstant": (2, (0x04, 0x08, 0x17)),
    "HasCustomAttribute": (5, (0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00, 0x0E, 0x17, 0x14,
                               0x11, 0x1A, 0x1B, 0x20, 0x23, 0x26, 0x27, 0x28, 0x2A, 0x2C, 0x2B)),
    "HasFieldMarshal": (1, (0x04, 0x08)),
    "HasDeclSecurity": (2, (0x02, 0x06, 0x20)),
    "MemberRefParent": (3, (0x02, 0x01, 0x1A, 0x06, 0x1B)),
    "HasSemantics": (1, (0x14, 0x17)),
    "MethodDefOrRef": (1, (0x06, 0x0A)),
    "MemberForwarded": (1, (0x04, 0x06)),
    "Implementation": (2, (0x26, 0x23, 0x27)),
    "CustomAttributeType": (3, (0x06, 0x0A)),
    "ResolutionScope": (2, (0x00, 0x1A, 0x23, 0x01)),
    "TypeOrMethodDef": (1, (0x02, 0x06)),
}
TABLES = {
    0x00: (2, "S", "G", "G", "G"),
    0x01: ("ResolutionScope", "S", "S"),
    0x02: (4, "S", "S", "TypeDefOrRef", (0x04,), (0x06,)),
    0x03: ((0x04,),),
    0x04: (2, "S", "B"),
    0x05: ((0x06,),),
    0x06: (4, 2, 2, "S", "B", (0x08,)),
    0x07: ((0x08,),),
    0x08: (2, 2, "S"),
    0x09: ((0x02,), "TypeDefOrRef"),
    0x0A: ("MemberRefParent", "S", "B"),
    0x0B: (2, "HasConstant", "B"),
    0x0C: ("HasCustomAttribute", "CustomAttributeType", "B"),
    0x0D: ("HasFieldMarshal", "B"),
    0x0E: (2, "HasDeclSecurity", "B"),
    0x0F: (2, 4, (0x02,)),
    0x10: (4, (0x04,)),
    0x11: ("B",),
    0x12: ((0x02,), (0x14,)),
    0x13: ((0x14,),),
    0x14: (2, "S", "TypeDefOrRef"),
    0x15: ((0x02,), (0x17,)),
    0x16: ((0x17,),),
    0x17: (2, "S", "B"),
    0x18: (2, (0x06,), "HasSemantics"),
    0x19: ((0x02,), "MethodDefOrRef", "MethodDefOrRef"),
    0x1A: ("S",),
    0x1B: ("B",),
    0x1C: (2, "MemberForwarded", "S", (0x1A,)),
    0x1D: (4, (0x04,)),
    0x1E: (4, 4),
    0x1F: (4,),
    0x20: (4, 2, 2, 2, 2, 4, "B", "S", "S"),
    0x21: (4,),
    0x22: (4, 4, 4),
    0x23: (2, 2, 2, 2, 4, "B", "S", "S", "B"),
    0x24: (4, (0x23,)),
    0x25: (4, 4, 4, (0x23,)),
    0x26: (4, "S", "B"),
    0x27: (4, 4, "S", "S", "Implementation"),
    0x28: (4, 4, "S", "Implementation"),
    0x29: ((0x02,), (0x02,)),
    0x2A: (2, 2, "TypeOrMethodDef", "S"),
    0x2B: ("MethodDefOrRef", "B"),
    0x2C: ((0x2A,), "TypeDefOrRef"),
}
TYPEDEF, TYPEREF, TYPESPEC, NESTEDCLASS = 0x02, 0x01, 0x1B, 0x29


class Metadata:
    """The ECMA-335 metadata of a PE file: heaps and table rows."""

    def __init__(self, data):
        if data[:2] != b"MZ":
            raise ValueError("no MZ header")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("no PE signature")
        coff = pe + 4
        n_sections, = struct.unpack_from("<H", data, coff + 2)
        opt_size, = struct.unpack_from("<H", data, coff + 16)
        opt = coff + 20
        magic, = struct.unpack_from("<H", data, opt)
        dirs = opt + (96 if magic == 0x10B else 112)
        cli_rva, cli_size = struct.unpack_from("<II", data, dirs + 14 * 8)
        if not cli_rva:
            raise ValueError("no CLI header")
        sections = []
        for i in range(n_sections):
            s = opt + opt_size + i * 40
            vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, s + 8)
            sections.append((vaddr, max(vsize, rsize), rptr))

        def offset(rva):
            for vaddr, size, rptr in sections:
                if vaddr <= rva < vaddr + size:
                    return rva - vaddr + rptr
            raise ValueError("RVA 0x%x outside every section" % rva)

        cli = offset(cli_rva)
        md_rva, _ = struct.unpack_from("<II", data, cli + 8)
        md = offset(md_rva)
        if struct.unpack_from("<I", data, md)[0] != 0x424A5342:
            raise ValueError("no metadata signature")
        ver_len, = struct.unpack_from("<I", data, md + 12)
        p = md + 16 + ver_len
        n_streams, = struct.unpack_from("<H", data, p + 2)
        p += 4
        streams = {}
        for _ in range(n_streams):
            s_off, s_size = struct.unpack_from("<II", data, p)
            end = data.index(b"\0", p + 8)
            name = data[p + 8:end].decode("ascii")
            p = (end + 4) & ~3
            streams[name] = md + s_off
        self.data = data
        self.strings = streams["#Strings"]
        self.guids = streams["#GUID"]
        self.blobs = streams.get("#Blob", 0)
        tables = streams.get("#~") or streams["#-"]
        heap_sizes = data[tables + 6]
        valid, = struct.unpack_from("<Q", data, tables + 8)
        p = tables + 24
        self.rows = {}
        for t in range(64):
            if valid >> t & 1:
                self.rows[t], = struct.unpack_from("<I", data, p)
                p += 4
        if heap_sizes & 0x40:
            p += 4
        heap = {"S": 0x01, "G": 0x02, "B": 0x04}

        def width(col):
            if isinstance(col, int):
                return col
            if col in heap:
                return 4 if heap_sizes & heap[col] else 2
            if isinstance(col, tuple):
                return 4 if self.rows.get(col[0], 0) >= 1 << 16 else 2
            bits, targets = CODED[col]
            return 4 if max(self.rows.get(t, 0) for t in targets) >= 1 << (16 - bits) else 2

        self.layout = {}
        for t in sorted(self.rows):
            if t not in TABLES:
                raise ValueError("unknown metadata table 0x%02x" % t)
            widths = [width(c) for c in TABLES[t]]
            self.layout[t] = (p, widths, sum(widths))
            p += self.rows[t] * sum(widths)
        if p > len(data):
            raise ValueError("metadata tables run past the end of the file")

    def row(self, table, index):
        start, widths, size = self.layout[table]
        if not 1 <= index <= self.rows[table]:
            raise ValueError("row %d outside table 0x%02x" % (index, table))
        p = start + (index - 1) * size
        values = []
        for w in widths:
            values.append(int.from_bytes(self.data[p:p + w], "little"))
            p += w
        return values

    def string(self, index):
        start = self.strings + index
        return self.data[start:self.data.index(b"\0", start)].decode("utf-8")

    def guid(self, index):
        g = self.guids + (index - 1) * 16
        return uuid.UUID(bytes_le=bytes(self.data[g:g + 16]))

    def blob(self, index):
        length, p = compressed_uint(self.data, self.blobs + index)
        return self.data[p:p + length]


def compressed_uint(data, p):
    b = data[p]
    if b & 0x80 == 0:
        return b, p + 1
    if b & 0xC0 == 0x80:
        return (b & 0x3F) << 8 | data[p + 1], p + 2
    return (b & 0x1F) << 24 | data[p + 1] << 16 | data[p + 2] << 8 | data[p + 3], p + 4


def read_metadata(dll, reader):
    try:
        return reader(Metadata(Path(dll).read_bytes()))
    except (ValueError, KeyError, IndexError, struct.error) as e:
        raise Fail("%s is not a .NET assembly this script can read: %s" % (dll, e))


def read_mvid(dll):
    def mvid(md):
        if not md.rows.get(0x00):
            raise ValueError("no Module table")
        index = md.row(0x00, 1)[2]
        if not index:
            raise ValueError("Module row has no MVID")
        return str(md.guid(index))
    return read_metadata(dll, mvid)


def simple_name(name):
    return re.sub(r"`\d+$", "", name)


def read_base_chains(dll):
    """Map each global-namespace top-level type's name to its own name and its bases' names, nearest first.

    Names drop the generic arity; a generic base is named by its definition; an external base ends the chain.
    """
    def chains(md):
        n_types = md.rows.get(TYPEDEF, 0)
        nested = {md.row(NESTEDCLASS, i)[0] for i in range(1, md.rows.get(NESTEDCLASS, 0) + 1)}
        defs = [None] + [md.row(TYPEDEF, i) for i in range(1, n_types + 1)]

        def base(index):
            coded = defs[index][3]
            tag, row = coded & 3, coded >> 2
            if not row:
                return None
            if tag == 2:
                sig = md.blob(md.row(TYPESPEC, row)[0])
                if sig[:1] != b"\x15" or sig[1:2] not in (b"\x11", b"\x12"):
                    raise ValueError("TypeSpec %d as a base is not a generic instance" % row)
                coded, _ = compressed_uint(sig, 2)
                tag, row = coded & 3, coded >> 2
            if tag == 0:
                return row, simple_name(md.string(defs[row][1]))
            if tag == 1:
                return None, simple_name(md.string(md.row(TYPEREF, row)[1]))
            raise ValueError("base of TypeDef %d is not a TypeDef or TypeRef" % index)

        result = {}
        for i in range(1, n_types + 1):
            if i in nested or md.string(defs[i][2]):
                continue
            name = md.string(defs[i][1])
            if name == "<Module>":
                continue
            chain = [simple_name(name)]
            seen = {i}
            step = base(i)
            while step:
                row, base_name = step
                chain.append(base_name)
                if row is None or row in seen:
                    break
                seen.add(row)
                step = base(row)
            result.setdefault(chain[0], []).append(chain)
        return result
    return read_metadata(dll, chains)


def sha256_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- Steps 4 and 5: decompile, IL, LF ---------------------------------------

def run_ilspy(exe, args, cwd):
    cmd = [str(exe)] + args
    try:
        result = subprocess.run(cmd, cwd=str(cwd))
    except OSError as e:
        raise Fail("cannot run %s: %s" % (exe, e))
    if result.returncode != 0:
        raise Fail("ilspycmd exited %d: %s" % (result.returncode, " ".join(cmd)))


# Native frame offsets in ilspycmd's error comments differ between its Linux and Windows builds.
FRAME_OFFSET = re.compile(rb"^([ \t]*//[ \t]+at .*) \+ 0x[0-9a-fA-F]+$", re.MULTILINE)


def normalize(root):
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            new = FRAME_OFFSET.sub(rb"\1", data.replace(b"\r\n", b"\n"))
            if new != data:
                path.write_bytes(new)


# --- Step 3: XML docs -------------------------------------------------------

def fetch_docs(dest):
    print("Downloading %s" % DOCS_URL, file=sys.stderr)
    try:
        with urllib.request.urlopen(DOCS_URL, timeout=120) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except (OSError, http.client.HTTPException) as e:
        raise Fail("download of %s failed: %s; pass --docs <file> to inline a local copy, "
                   "or --no-docs to inline none" % (DOCS_URL, e))


# --- Step 6: placement ------------------------------------------------------

RULE_KINDS = ("class", "base", "prefix")
RULE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RULE_FOLDER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
PREFIX_BOUNDARY = re.compile(r"[A-Z0-9_]|\Z")


def read_layout_map(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise Fail("cannot read %s: %s" % (path, e))
    rules = {kind: {} for kind in RULE_KINDS}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if (len(fields) != 3 or fields[0] not in rules or not RULE_KEY.match(fields[1])
                or not RULE_FOLDER.match(fields[2])):
            raise Fail("%s line %d is not a kind, key and folder separated by spaces or tabs, with kind class, "
                       "base or prefix, an identifier key and one folder name" % (path.name, number))
        kind, key, folder = fields
        if key in rules[kind]:
            raise Fail("%s line %d repeats the %s rule for %s" % (path.name, number, kind, key))
        rules[kind][key] = folder
    return rules


def folder_for(name, chain, rules):
    if name in rules["class"]:
        return rules["class"][name]
    for ancestor in chain:
        if ancestor in rules["base"]:
            return rules["base"][ancestor]
    prefixes = [key for key in rules["prefix"]
                if name.startswith(key) and PREFIX_BOUNDARY.match(name, len(key))]
    if prefixes:
        return rules["prefix"][max(prefixes, key=len)]
    return UNSORTED


def plan_layout(out, rules, chains):
    """Map each output file's `/`-separated path under `out` to its path under Source/."""
    written = {p.name.lower(): p.name for p in out.iterdir() if p.is_dir()}
    folders = {folder for kind in rules.values() for folder in kind.values()} | {UNSORTED}
    clashes = ["%s (ilspycmd wrote %s/)" % (f, written[f.lower()])
               for f in sorted(folders) if f.lower() in written]
    if clashes:
        raise Fail("%s folders collide with ilspycmd's namespace folders: %s"
                   % (LAYOUT_MAP.name, ", ".join(clashes)))
    placement = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out).as_posix()
        # ilspycmd names a root file by its type's name without arity, all arities in one file.
        if "/" not in rel and path.suffix == ".cs":
            chain = chains.get(path.stem, [[path.stem]])[0]
            placement[rel] = folder_for(path.stem, chain, rules) + "/" + rel
        else:
            placement[rel] = rel
    return placement


# --- Step 7: project files --------------------------------------------------

def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def rewrite_csproj(path):
    """Rewrite ilspycmd's SDK-style project for SDK globbing and HintPaths under $(ManagedDir)."""
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError) as e:
        raise Fail("cannot read ilspycmd's %s: %s" % (path.name, e))
    sdk = root.get("Sdk")
    if not sdk:
        raise Fail("ilspycmd's %s is not an SDK-style project" % path.name)
    props, items = {}, {}
    for group in root:
        if local_name(group.tag) == "PropertyGroup":
            for prop in group:
                props.setdefault(local_name(prop.tag), prop.text or "")
        elif local_name(group.tag) == "ItemGroup":
            for item in group:
                kind = local_name(item.tag)
                if kind == "Compile":
                    continue
                meta = []
                for m in item:
                    value = m.text or ""
                    if kind == "Reference" and local_name(m.tag) == "HintPath":
                        value = "$(ManagedDir)/" + re.split(r"[\\/]", value)[-1]
                    meta.append((local_name(m.tag), value))
                attrs = tuple(item.attrib.items())
                items[(kind,) + attrs] = meta
    props.pop("ProjectGuid", None)
    items.setdefault(("Reference", ("Include", "UnityEngine")),
                     [("HintPath", "$(ManagedDir)/UnityEngine.dll")])

    lines = ['<Project Sdk="%s">' % escape(sdk, QUOTE), "  <PropertyGroup>",
             "    <ProjectGuid>%s</ProjectGuid>" % PROJECT_GUID]
    lines += ["    <%s>%s</%s>" % (name, escape(value), name) for name, value in props.items()]
    lines.append("  </PropertyGroup>")
    if items:
        lines.append("  <ItemGroup>")
        for key in sorted(items):
            kind, attrs = key[0], "".join(' %s="%s"' % (k, escape(v, QUOTE)) for k, v in key[1:])
            if items[key]:
                lines.append("    <%s%s>" % (kind, attrs))
                lines += ["      <%s>%s</%s>" % (n, escape(v), n) for n, v in items[key]]
                lines.append("    </%s>" % kind)
            else:
                lines.append("    <%s%s />" % (kind, attrs))
        lines.append("  </ItemGroup>")
    lines.append("</Project>")
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def build_props(managed):
    value = "".join(MSBUILD_ESCAPES.get(c, c) for c in str(managed))
    return ("<Project>\n  <PropertyGroup>\n    <ManagedDir>%s</ManagedDir>\n"
            "  </PropertyGroup>\n</Project>\n" % escape(value))


# --- Step 8: manifest -------------------------------------------------------

def read_manifest():
    path = SOURCE_DIR / MANIFEST
    if not path.is_file():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        if "\\" in line or ":" in line or any(part in ("", ".", "..") for part in line.split("/")):
            raise Fail("%s line %d, %s, is not a path inside %s; fix or delete the manifest"
                       % (path, number, line, SOURCE_DIR))
        entries.append(line)
    return entries


def prune(old_entries, written):
    """Delete the previous run's files this run did not write, and the folders that leaves empty."""
    keep = {os.path.normcase(p) for p in written}
    folders = set()
    for entry in old_entries:
        if os.path.normcase(entry) in keep or entry.rsplit("/", 1)[-1].lower() == "agents.md":
            continue
        target = SOURCE_DIR / entry
        if target.is_file() or target.is_symlink():
            target.unlink()
        folder = target.parent
        while folder != SOURCE_DIR:
            folders.add(folder)
            folder = folder.parent
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


# --- Step 9: BUILD.md -------------------------------------------------------

def build_md(sha256, mvid, is_reference, docs):
    flags = " ".join(SOURCE_FLAGS + ["-r", "<Managed>"])
    il_flags = " ".join(IL_FLAGS + ["-r", "<Managed>"])
    if docs:
        docs_line = "- XML docs: inlined from %s, SHA256 `%s`" % (docs[1], docs[0])
    else:
        docs_line = "- XML docs: not inlined"
    lines = [
        "# Build",
        "",
        "Written by `decompile.py`.",
        "",
        "- DLL SHA256: `%s`" % sha256,
        "- DLL MVID: `%s`" % mvid,
        "- ilspycmd: `%s`" % ILSPY_VERSION,
        "- Source flags: `%s`" % flags,
        "- IL flags: `%s`" % il_flags,
        docs_line,
    ]
    if is_reference:
        lines.append("- Reference build: yes (MVID `%s`)" % REFERENCE_MVID)
    else:
        lines.append("- Reference build: no; the reference build's MVID is `%s`" % REFERENCE_MVID)
        lines += [
            "",
            "**Warning:** this tree is not decompiled from the reference build that the Broforce "
            "documentation is written against, so code here may differ from what the docs describe.",
        ]
    return "\n".join(lines) + "\n"


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Decompile a Broforce install's Assembly-CSharp.dll into Source/ "
        "with a pinned ilspycmd, sorted into folders by layout-map.txt, with a buildable "
        "project and its IL listing as Source/Assembly-CSharp.il.")
    parser.add_argument("--game", type=Path, metavar="DIR",
                        help="Broforce install folder; default: found through the Steam libraries")
    parser.add_argument("--dll", type=Path, metavar="FILE",
                        help="Assembly-CSharp.dll to decompile; default: the install's own")
    docs = parser.add_mutually_exclusive_group()
    docs.add_argument("--docs", type=Path, metavar="FILE",
                      help="Assembly-CSharp.xml to inline as /// comments; default: Broforce-Docs' "
                      "published file, downloaded")
    docs.add_argument("--no-docs", action="store_true", help="inline no documentation")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        plat = platform_key()
        if args.dll is not None and not args.dll.is_file():
            raise Fail("--dll %s is not a file" % args.dll)
        if args.game is not None and not args.game.is_dir():
            raise Fail("--game %s is not a folder" % args.game)
        if args.docs is not None and not args.docs.is_file():
            raise Fail("--docs %s is not a file" % args.docs)
        rules = read_layout_map(LAYOUT_MAP)
        old_manifest = read_manifest()
        game = args.game or find_install(plat)
        managed = find_managed(game).resolve()
        dll = args.dll or managed / DLL_NAME
        mvid = read_mvid(dll)
        dll_sha256 = sha256_hex(dll)
        chains = read_base_chains(dll)

        cache = cache_dir(plat)
        nupkg = fetch_package(plat, cache)
        with tempfile.TemporaryDirectory(dir=str(cache)) as tool_dir, \
                tempfile.TemporaryDirectory() as work:
            exe = extract_tool(plat, nupkg, Path(tool_dir))
            work = Path(work)
            dll_dir = work / "dll"
            dll_dir.mkdir()
            shutil.copyfile(dll, dll_dir / DLL_NAME)
            docs = None
            if not args.no_docs:
                xml = dll_dir / XML_NAME
                if args.docs is not None:
                    shutil.copyfile(args.docs, xml)
                    origin = "a local file"
                else:
                    fetch_docs(xml)
                    origin = "`%s`" % DOCS_URL
                docs = (sha256_hex(xml), origin)
            out = work / "out"
            ref = ["-r", str(managed)]
            # ilspycmd writes the assembly path as given into error comments; a bare name keeps the tree machine-independent.
            run_ilspy(exe, SOURCE_FLAGS + ref + ["-o", str(out), DLL_NAME], dll_dir)
            run_ilspy(exe, IL_FLAGS + ref + ["-o", str(out), DLL_NAME], dll_dir)
            if not (out / "Assembly-CSharp.il").is_file():
                raise Fail("ilspycmd wrote no Assembly-CSharp.il")
            normalize(out)
            placement = plan_layout(out, rules, chains)
            if CSPROJ not in placement:
                raise Fail("ilspycmd wrote no %s" % CSPROJ)
            rewrite_csproj(out / CSPROJ)
            for rel, dest in placement.items():
                target = SOURCE_DIR / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(out / rel, target)

        is_reference = mvid == REFERENCE_MVID
        write_text(SOURCE_DIR / PROPS, build_props(managed))
        write_text(SOURCE_DIR / "BUILD.md", build_md(dll_sha256, mvid, is_reference, docs))
        written = sorted(set(placement.values()) | {PROPS, "BUILD.md"})
        prune(old_manifest, written)
        write_text(SOURCE_DIR / MANIFEST, "".join(p + "\n" for p in written))
    except Fail as e:
        print("decompile.py: error: %s" % e, file=sys.stderr)
        return 1
    unsorted = sum(1 for dest in placement.values() if dest.startswith(UNSORTED + "/"))
    if unsorted:
        print("%d files matched no %s rule and went to %s/" % (unsorted, LAYOUT_MAP.name, UNSORTED))
    print("Wrote %s from %s (MVID %s)" % (SOURCE_DIR, dll, mvid))
    if not is_reference:
        print("decompile.py: warning: %s (MVID %s) is not the reference build, whose MVID is %s"
              % (dll, mvid, REFERENCE_MVID), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
