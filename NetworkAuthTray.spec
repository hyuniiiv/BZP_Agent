# -*- mode: python ; coding: utf-8 -*-
import os

# 읽기전용 업데이트 토큰이 있으면 번들에 포함 (없으면 비인증 시도 — 공개 저장소일 때만 동작)
_datas = [('update_token.txt', '.')] if os.path.exists('update_token.txt') else []


a = Analysis(
    ['network_auth_tray.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=['credentials_dialog', 'git_sync', 'lab_dev', 'settings_store', 'settings_dialog', 'version', 'updater', 'pystray._win32', 'PIL._tkinter_finder'],
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
    [],
    exclude_binaries=True,
    name='BZP_Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BZP_Agent',
)
