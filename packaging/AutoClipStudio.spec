from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

packaging_dir = Path(SPECPATH)
root = packaging_dir.parent
icon = root / "build" / "assets" / "autoclip.ico"
ffmpeg = root / "tools" / "ffmpeg"
helpers = root / "tools" / "package"
media_files = [
    (str(ffmpeg / "bin" / name), "tools/ffmpeg/bin")
    for name in ("ffmpeg.exe", "ffprobe.exe")
]
media_files += [(str(path), "tools/ffmpeg/bin") for path in (ffmpeg / "bin").glob("*.dll")]
media_files += [(str(helpers / name), "tools/package") for name in ("yt-dlp.exe", "deno.exe")]
media_files += [
    (str(helpers / "DENO-LICENSE.md"), "tools/package"),
    (str(ffmpeg / "LICENSE"), "tools/ffmpeg"),
    (str(ffmpeg / "README.txt"), "tools/ffmpeg"),
]
data = media_files + [
    (str(icon), "assets"),
    (str(root / "docs" / "THIRD_PARTY_NOTICES.md"), "."),
]
for package in ("faster_whisper", "cv2"):
    data += collect_data_files(package)
for package in ("keyring", "faster-whisper", "huggingface-hub"):
    data += copy_metadata(package)

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    datas=data,
    hiddenimports=["keyring.backends.Windows", "ctypes.wintypes"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="AutoClipStudio",
    console=False,
    upx=False,
    icon=str(icon),
    version=str(packaging_dir / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="AutoClipStudio")
