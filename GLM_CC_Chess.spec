# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GLM CC Chess."""

block_cipher = None

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/fonts', 'fonts'),
    ],
    hiddenimports=['src.board', 'src.moves', 'src.game', 'src.engine',
                   'src.gui', 'src.gui_textinput', 'src.clipboard_util',
                   'src.uci', 'src.lichess_client', 'src.lichess_controller'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GLM_CC_Chess',
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
    icon=None,
)