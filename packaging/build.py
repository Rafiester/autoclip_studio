"""Build the source launcher or an optional Windows distribution."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path


PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING.parent))

from scripts.setup import PYTHON, ROOT, TOOLS, UV, download, ensure_runtime, run


def build_launcher(icon: Path) -> None:
    compiler = (Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe")
    if not compiler.is_file():
        raise FileNotFoundError("The Windows .NET Framework x64 C# compiler is unavailable.")
    output = ROOT / "AutoClipStudio.exe"
    launcher = PACKAGING / "launcher"
    staging = ROOT / "build" / "launcher"
    staging.mkdir(parents=True, exist_ok=True)
    executable = staging / output.name
    run(
        compiler, "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
        f"/out:{executable}", f"/win32icon:{icon}",
        "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
        "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
        "/reference:System.Web.Extensions.dll",
        launcher / "Launcher.cs", launcher / "SetupWizard.cs", launcher / "WindowsJob.cs",
    )
    try:
        executable.replace(output)
        print(f"Source launcher built: {output}", flush=True)
    except PermissionError:
        print(f"Source launcher compiled at {executable}; close running AutoClipStudio.exe to replace {output}", flush=True)


def prepare_packaged_tools() -> None:
    helpers = TOOLS / "package"
    download("https://github.com/yt-dlp/yt-dlp/releases/download/2026.11.23/yt-dlp.exe",
             helpers / "yt-dlp.exe", "622ed48023cac1daa52a3bf3a6ac38f03514de214b5f0fa069389fd0f6d17e5c")
    archive = helpers / "deno.zip"
    download("https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-pc-windows-msvc.zip",
             archive, "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf")
    with zipfile.ZipFile(archive) as package:
        package.extract("deno.exe", helpers)
    license_path = helpers / "DENO-LICENSE.md"
    if not license_path.is_file():
        download("https://raw.githubusercontent.com/denoland/deno/v2.9.8/LICENSE.md", license_path)


def find_installer_compiler(configured: Path | None) -> Path:
    if configured is not None:
        if configured.is_file():
            return configured.resolve()
        raise FileNotFoundError(f"Inno Setup compiler not found at {configured}")
    in_path = shutil.which("ISCC.exe")
    if in_path:
        return Path(in_path)
    program_roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")]
    if local := os.environ.get("LOCALAPPDATA"):
        program_roots.append(str(Path(local) / "Programs"))
    for release in ("Inno Setup 7", "Inno Setup 6"):
        for root in filter(None, program_roots):
            candidate = Path(root) / release / "ISCC.exe"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("Install Inno Setup or pass --iscc PATH.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standalone", action="store_true", help="Also build the PyInstaller OneDir distribution.")
    parser.add_argument("--installer", action="store_true", help="Also build the OneDir distribution and installer.")
    parser.add_argument("--iscc", type=Path, help="Inno Setup compiler path.")
    options = parser.parse_args()
    ensure_runtime()
    icon = ROOT / "autoclip_studio" / "assets" / "autoclip.ico"
    run(PYTHON, PACKAGING / "make_icon.py", icon)
    build_launcher(icon)
    if options.standalone or options.installer:
        run(UV, "pip", "install", "--python", PYTHON, "-r", PACKAGING / "requirements-build.txt")
        prepare_packaged_tools()
        run(PYTHON, "-m", "PyInstaller", "--noconfirm", PACKAGING / "AutoClipStudio.spec")
    if options.installer:
        run(find_installer_compiler(options.iscc), PACKAGING / "AutoClipStudio.iss")


if __name__ == "__main__":
    main()
