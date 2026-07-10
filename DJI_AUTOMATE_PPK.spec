# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['\\\\EgnyteDrive\\sunriseengineering\\Survey\\UT\\_Scripts\\Automate UI\\DJIAutomatePPKV2.py'],
    pathex=[],
    binaries=[],
    datas=[('\\\\EgnyteDrive\\sunriseengineering\\Survey\\UT\\_Scripts\\Automate UI\\embed_ppk_metadata.py', '.')],
    hiddenimports=[],
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
    name='DJI_AUTOMATE_PPK',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
