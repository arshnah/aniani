# PyInstaller spec for a standalone aniani.exe -- no Python/pip needed
# on the target machine. Built on windows-latest via GitHub Actions
# (.github/workflows/build-windows.yml), since PyInstaller can't
# cross-compile a Windows binary from Linux.
#
# gui.py and the sources/player modules resolve each other via a
# runtime sys.path.insert() + plain `import X`, not package-relative
# imports -- PyInstaller's static analyzer doesn't execute that, it
# just needs sources/ and player/ on pathex to find the files by name.
import os

ROOT = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(ROOT, "gui.py")],
    pathex=[ROOT, os.path.join(ROOT, "sources"), os.path.join(ROOT, "player")],
    hiddenimports=[
        "anidb_source", "yuma_source", "nyaa_source", "_vendor_pyanimecli",
        "vlc_backend", "mpv_backend", "torrent_backend",
        "platform_utils", "discord_presence", "state", "download_manager", "rpc_daemon",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="aniani",
    console=False,  # GUI app -- no terminal window
    # bundling a.binaries/a.datas directly into EXE (no COLLECT step) is
    # what makes this a single-file exe, not a --onefile kwarg
)
