# -*- coding: utf-8 -*-
"""Build the installable PropHandoff .zip.

Usage:
    python build_zip.py            -> zip the version read from bl_info
    python build_zip.py --bump     -> bump the patch number, rewrite __init__.py, zip

The version lives in `prop_handoff/__init__.py` (bl_info["version"]): single
source of truth, it names the zip (`prop_handoff_v<X.Y.Z>.zip`).

The zip contains the `prop_handoff/` **folder** (the only form accepted by
"Install from Disk"). Separators are forced to "/": PowerShell's
`Compress-Archive` writes backslashes that violate the ZIP specification.
`__pycache__`, `.pyc` and version-control files are excluded. The content is
listed and the CRC verified after writing.
"""

import argparse
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(HERE, "prop_handoff")
INIT_FILE = os.path.join(ADDON_DIR, "__init__.py")

EXCLUDED_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", "~", ".swp")
EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}

VERSION_PATTERN = re.compile(r'("version"\s*:\s*)\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)')


def read_version():
    """bl_info["version"] -> (x, y, z)."""
    with open(INIT_FILE, encoding="utf-8") as handle:
        match = VERSION_PATTERN.search(handle.read())
    if match is None:
        sys.exit("Version not found in %s" % INIT_FILE)
    return tuple(int(group) for group in match.groups()[1:])


def write_version(version):
    """Rewrite bl_info["version"] in __init__.py."""
    with open(INIT_FILE, encoding="utf-8") as handle:
        source = handle.read()
    updated, count = VERSION_PATTERN.subn(r"\g<1>(%d, %d, %d)" % version, source, count=1)
    if count != 1:
        sys.exit("Cannot rewrite the version in %s" % INIT_FILE)
    with open(INIT_FILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)


def collect_files():
    """Files to ship, sorted for a reproducible zip."""
    collected = []
    for root, directories, filenames in os.walk(ADDON_DIR):
        directories[:] = sorted(d for d in directories if d not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILES or filename.endswith(EXCLUDED_SUFFIXES):
                continue
            collected.append(os.path.join(root, filename))
    return collected


def build(version):
    """Write the zip and return its path."""
    destination = os.path.join(HERE, "prop_handoff_v%d.%d.%d.zip" % version)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in collect_files():
            arcname = os.path.relpath(path, HERE).replace(os.sep, "/")
            archive.write(path, arcname)
    return destination


def verify(destination):
    """List the content and check the CRC of every entry."""
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        entries = archive.namelist()
    if bad is not None:
        sys.exit("Invalid CRC: %s" % bad)
    if not any(entry.startswith("prop_handoff/") for entry in entries):
        sys.exit("The zip does not contain the prop_handoff/ folder")
    if any("\\" in entry for entry in entries):
        sys.exit("Non-compliant separators in the zip")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bump", action="store_true",
                        help="bump the patch number before zipping")
    arguments = parser.parse_args()

    version = read_version()
    if arguments.bump:
        version = (version[0], version[1], version[2] + 1)
        write_version(version)
        print("Version -> %d.%d.%d" % version)

    destination = build(version)
    entries = verify(destination)
    print("%s (%d bytes, %d files, CRC OK)"
          % (os.path.basename(destination), os.path.getsize(destination), len(entries)))
    for entry in entries:
        print("   ", entry)


if __name__ == "__main__":
    main()
