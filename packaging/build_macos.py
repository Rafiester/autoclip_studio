"""Build the native macOS AutoClipStudio.app launcher and optional DMG image."""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING))

from make_icns import build_icns


INFO_PLIST = {
    "CFBundleName": "AutoClip Studio",
    "CFBundleDisplayName": "AutoClip Studio",
    "CFBundleIdentifier": "com.autoclip.studio",
    "CFBundleVersion": "1.0.0",
    "CFBundleShortVersionString": "1.0.0",
    "CFBundlePackageType": "APPL",
    "CFBundleExecutable": "AutoClipStudio",
    "CFBundleIconFile": "autoclip.icns",
    "LSMinimumSystemVersion": "13.0",
    "LSArchitecturePriority": ["arm64"],
    "NSHighResolutionCapable": True,
    "NSSupportsAutomaticGraphicsSwitching": True,
}


def build_app_bundle(output_dir: Path, *, compile_binary: bool | None = None) -> Path:
    app_bundle = output_dir / "AutoClipStudio.app"
    contents = app_bundle / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    icns_path = ROOT / "autoclip_studio" / "assets" / "autoclip.icns"
    build_icns(icns_path)
    shutil.copy2(icns_path, resources_dir / "autoclip.icns")

    wordmark = ROOT / "autoclip_studio" / "assets" / "AutoClipTrans.png"
    if wordmark.is_file():
        shutil.copy2(wordmark, resources_dir / "AutoClipTrans.png")

    (resources_dir / "checkout_root.txt").write_text(str(ROOT.resolve()), encoding="utf-8")
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(INFO_PLIST, stream)

    should_compile = (sys.platform == "darwin") if compile_binary is None else compile_binary
    binary = macos_dir / "AutoClipStudio"
    if should_compile:
        launcher_src = ROOT / "packaging" / "macos" / "Launcher.m"
        subprocess.run(
            [
                "clang", "-fobjc-arc", "-O2", "-arch", "arm64",
                "-framework", "Cocoa",
                str(launcher_src), "-o", str(binary),
            ],
            check=True,
        )
        os.chmod(binary, 0o755)
        print(f"Compiled native Apple Silicon app launcher: {app_bundle}", flush=True)
    else:
        print(
            f"Staged macOS .app bundle resources at {app_bundle} "
            "(run on macOS with Xcode CLT to compile Mach-O arm64 binary)",
            flush=True,
        )
    return app_bundle


def build_dmg(app_bundle: Path, dmg_path: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("DMG creation requires macOS hdiutil.")
    dmg_path.parent.mkdir(parents=True, exist_ok=True)
    if dmg_path.exists():
        dmg_path.unlink()
    subprocess.run(
        [
            "hdiutil", "create", "-volname", "AutoClip Studio",
            "-srcfolder", str(app_bundle), "-ov", "-format", "UDZO", str(dmg_path),
        ],
        check=True,
    )
    print(f"Built DMG package: {dmg_path}", flush=True)
    return dmg_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--dmg", action="store_true", help="Also build AutoClipStudio.dmg (macOS only).")
    args = parser.parse_args()
    app = build_app_bundle(args.output_dir)
    if args.dmg:
        build_dmg(app, args.output_dir / "AutoClipStudio.dmg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
