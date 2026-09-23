# -*- coding: utf-8 -*-
"""Build the extensions.blender.org package of PropHandoff.

Usage:
    python build_extension.py                 -> build/prop_handoff_extension/ + prop_handoff_extension.zip
    python build_extension.py --validate      -> also run `blender --command extension validate`
    python build_extension.py --tagline "..." -> override the manifest tagline (64 characters max)
    python build_extension.py --blender <exe> -> Blender executable used for the validation

The source folder `prop_handoff/` is never modified: it keeps its `bl_info`
for the legacy "Install from Disk" workflow (see build_zip.py). This script
derives the extension from it:

  * `__init__.py` with the `bl_info` dictionary removed (extensions carry
    their metadata in `blender_manifest.toml`);
  * `operators.py`, `panel.py`, `utils.py`, `README.md` copied as is;
  * `LICENSE` (full GPL-3.0-or-later text) copied from the project root;
  * `blender_manifest.toml` generated with the version read from `bl_info`
    (`(0, 3, 2)` -> "0.3.2"), the single source of truth for the version.

The zip contains the *content* of the build folder (manifest at the root),
which is what the platform and "Install from Disk" (extensions) expect.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(HERE, "prop_handoff")
BUILD_DIR = os.path.join(HERE, "build", "prop_handoff_extension")
ZIP_PATH = os.path.join(HERE, "prop_handoff_extension.zip")

#: Files shipped in the extension (relative to the source folder).
SHIPPED_FILES = ("__init__.py", "operators.py", "panel.py", "utils.py", "README.md")
#: Optional folders copied from the source folder when present (icons, assets…).
OPTIONAL_ENTRIES = ("icons", "assets")
#: License text shipped in the extension: project root first, then the
#: source folder.
LICENSE_CANDIDATES = (os.path.join(HERE, "LICENSE"), os.path.join(SOURCE_DIR, "LICENSE"))

VERSION_PATTERN = re.compile(r'"version"\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)')
BL_INFO_PATTERN = re.compile(r"^bl_info\s*=\s*\{.*?^\}\n\n?", re.DOTALL | re.MULTILINE)

#: The platform validator refuses taglines longer than 64 characters or
#: ending with punctuation (strict validation).
DEFAULT_TAGLINE = "Move props between hands without touching constraints or keys"
#: Project page shown by the platform (optional field, but the repository is
#: where the issues and the releases live).
DEFAULT_WEBSITE = "https://github.com/glbaugustin-netizen/prop-handoff"

MANIFEST_TEMPLATE = '''schema_version = "1.0.0"
id = "prop_handoff"
version = "{version}"
name = "PropHandoff"
tagline = "{tagline}"
maintainer = "Augustin"
type = "add-on"
blender_version_min = "4.2.0"
license = ["SPDX:GPL-3.0-or-later"]
tags = ["Animation", "Rigging"]
website = "{website}"
'''


def read_version(init_source):
    """bl_info["version"] of the source `__init__.py` -> "x.y.z"."""
    match = VERSION_PATTERN.search(init_source)
    if match is None:
        sys.exit("bl_info version not found in %s" % os.path.join(SOURCE_DIR, "__init__.py"))
    return "%s.%s.%s" % match.groups()


def strip_bl_info(init_source):
    """`__init__.py` without its `bl_info` dictionary; the docstring sentence
    that mentions it is rewritten."""
    stripped, count = BL_INFO_PATTERN.subn("", init_source, count=1)
    if count != 1:
        sys.exit("bl_info dictionary not found in the source __init__.py")
    stripped = stripped.replace(
        "This module only holds `bl_info` and the register/unregister orchestration.",
        "This module only holds the register/unregister orchestration (the\n"
        "extension metadata lives in blender_manifest.toml).")
    return stripped


def build(tagline, website):
    """Populate the build folder and write the zip. Returns (version, files)."""
    with open(os.path.join(SOURCE_DIR, "__init__.py"), encoding="utf-8") as handle:
        init_source = handle.read()
    version = read_version(init_source)
    if len(tagline) > 64 or not tagline[-1].isalnum():
        print("warning: tagline is %d characters / ends with %r — the validator will refuse it"
              % (len(tagline), tagline[-1]))

    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR)

    written = []
    with open(os.path.join(BUILD_DIR, "__init__.py"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(strip_bl_info(init_source))
    written.append("__init__.py")
    for name in SHIPPED_FILES[1:]:
        shutil.copyfile(os.path.join(SOURCE_DIR, name), os.path.join(BUILD_DIR, name))
        written.append(name)
    for name in OPTIONAL_ENTRIES:
        source = os.path.join(SOURCE_DIR, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(BUILD_DIR, name),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            written.append(name + "/")
    license_path = next((path for path in LICENSE_CANDIDATES if os.path.isfile(path)), None)
    if license_path is None:
        sys.exit("LICENSE text not found (expected %s)" % LICENSE_CANDIDATES[0])
    shutil.copyfile(license_path, os.path.join(BUILD_DIR, "LICENSE"))
    written.append("LICENSE")
    with open(os.path.join(BUILD_DIR, "blender_manifest.toml"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(MANIFEST_TEMPLATE.format(version=version, tagline=tagline, website=website))
    written.append("blender_manifest.toml")

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, directories, filenames in os.walk(BUILD_DIR):
            directories[:] = sorted(d for d in directories if d != "__pycache__")
            for filename in sorted(filenames):
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(root, filename)
                archive.write(path, os.path.relpath(path, BUILD_DIR).replace(os.sep, "/"))
    with zipfile.ZipFile(ZIP_PATH) as archive:
        bad = archive.testzip()
        entries = archive.namelist()
    if bad is not None:
        sys.exit("Invalid CRC: %s" % bad)
    if "blender_manifest.toml" not in entries:
        sys.exit("blender_manifest.toml is not at the root of the zip")
    return version, entries


def find_blender(explicit):
    """Blender executable: explicit path, `blender` on PATH, or the Microsoft
    Store launcher alias."""
    if explicit:
        return explicit
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    alias = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "blender-launcher.exe")
    return alias if os.path.isfile(alias) else None


def validate(blender):
    """Run `blender --command extension validate <zip>` and return its output.

    The Microsoft Store launcher does not forward the console output of the
    Blender it starts: when nothing comes back, the same command handler is
    run from a Python script that redirects the output into a log file.
    """
    command = [blender, "--command", "extension", "validate", ZIP_PATH]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "cannot run %s: %s" % (blender, error)
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        return result.returncode, output

    log_path = os.path.join(tempfile.gettempdir(), "prop_handoff_extension_validate.log")
    runner = os.path.join(tempfile.gettempdir(), "prop_handoff_extension_validate.py")
    with open(runner, "w", encoding="utf-8") as handle:
        handle.write(
            "import os, sys\n"
            "log = %r\n"
            "fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
            "os.dup2(fd, 1); os.dup2(fd, 2)\n"
            "sys.stdout = os.fdopen(1, 'w', buffering=1, encoding='utf-8', errors='replace')\n"
            "sys.stderr = sys.stdout\n"
            "from bl_pkg import bl_extension_cli\n"
            "code = bl_extension_cli.cli_extension_handler(['validate', %r])\n"
            "print('exit code:', code)\n"
            "sys.stdout.flush()\n" % (log_path, ZIP_PATH))
    if os.path.exists(log_path):
        os.remove(log_path)
    try:
        subprocess.run([blender, "--background", "--factory-startup", "--python", runner],
                       capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "cannot run %s: %s" % (blender, error)
    if not os.path.exists(log_path):
        return None, "no output from %s" % blender
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        output = handle.read()
    code_match = re.search(r"exit code: (\d+)", output)
    return (int(code_match.group(1)) if code_match else None), output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tagline", default=DEFAULT_TAGLINE, help="manifest tagline (64 characters max)")
    parser.add_argument("--validate", action="store_true", help="run `blender --command extension validate`")
    parser.add_argument("--blender", default="", help="Blender executable for the validation")
    parser.add_argument("--website", default=DEFAULT_WEBSITE, help="manifest website (project page)")
    arguments = parser.parse_args()

    version, entries = build(arguments.tagline, arguments.website)
    print("version %s -> %s" % (version, os.path.relpath(ZIP_PATH, HERE)))
    for entry in entries:
        print("   ", entry)

    if arguments.validate:
        blender = find_blender(arguments.blender)
        if blender is None:
            print("Blender not found on this machine: validation skipped "
                  "(run: blender --command extension validate prop_handoff_extension.zip)")
            return
        code, output = validate(blender)
        print("\n$ blender --command extension validate prop_handoff_extension.zip")
        print(output.rstrip())
        if code is not None:
            print("validation %s (exit code %d)" % ("OK" if code == 0 else "FAILED", code))


if __name__ == "__main__":
    main()
