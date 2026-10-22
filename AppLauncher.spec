# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, SPECPATH)
from launcher_icon import write_run_icon

write_run_icon(Path(SPECPATH))

a = Analysis(
    ['app_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('log4j2-console.xml', '.'), ('app_icon.ico', '.')],
    hiddenimports=['yaml', 'javaproperties'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AppLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.ico'],
)
