# -*- mode: python ; coding: utf-8 -*-

from imageio_ffmpeg import get_ffmpeg_exe


ffmpeg_exe = get_ffmpeg_exe()

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[(ffmpeg_exe, 'ffmpeg')],
    datas=[('icon/Icon2.png', 'icon')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['imageio_ffmpeg'],
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
    name='USB Track Order',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon/Icon2.ico'],
    version='version_info.txt',
)
