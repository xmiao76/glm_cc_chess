"""
package.py — Build the distributable zip for GLM CC Chess.

Usage:
    python package.py [--version VERSION]

Defaults:
    VERSION is read from release/readme.txt (last non-empty line under "VERSION").
    Output: release/GLM_CC_Chess_v<VERSION>.zip

The zip contains:
    GLM_CC_Chess.exe
    readme.txt
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

RELEASE_DIR = Path(__file__).parent / "release"
EXE_NAME = "GLM_CC_Chess.exe"
README_NAME = "readme.txt"


def read_version_from_readme(readme_path: Path) -> str:
    """Extract the version string from the readme VERSION section."""
    text = readme_path.read_text(encoding="utf-8")
    match = re.search(r"^VERSION\s*\n[-=]+\s*\n([\d.]+)", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    raise ValueError(
        f"Could not find VERSION in {readme_path}. "
        "Expected a 'VERSION' heading followed by a version number."
    )


def build_zip(version: str) -> Path:
    exe_path = RELEASE_DIR / EXE_NAME
    readme_path = RELEASE_DIR / README_NAME

    missing = [p for p in (exe_path, readme_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing required files: " + ", ".join(str(p) for p in missing)
        )

    zip_name = f"GLM_CC_Chess_v{version}.zip"
    zip_path = RELEASE_DIR / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe_path, EXE_NAME)
        zf.write(readme_path, README_NAME)

    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Package GLM CC Chess for distribution.")
    parser.add_argument(
        "--version",
        help="Version string (e.g. 1.0.0). Defaults to the value in release/readme.txt.",
    )
    args = parser.parse_args()

    readme_path = RELEASE_DIR / README_NAME
    if not readme_path.exists():
        print(f"ERROR: {readme_path} not found.", file=sys.stderr)
        sys.exit(1)

    version = args.version or read_version_from_readme(readme_path)
    print(f"Packaging version {version} ...")

    try:
        zip_path = build_zip(version)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Created: {zip_path}")
    print(f"  Added: {EXE_NAME}")
    print(f"  Added: {README_NAME}")


if __name__ == "__main__":
    main()
