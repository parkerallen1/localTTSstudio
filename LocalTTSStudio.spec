# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

qwen_datas, qwen_binaries, qwen_hiddenimports = collect_all('qwen_tts')

a = Analysis(
    ['app_launcher.py'],
    pathex=[],
    binaries=[('ffmpeg', '.'), ('venv/lib/python3.10/site-packages/torch/lib/libomp.dylib', '.')] + qwen_binaries,
    datas=[('static', 'static')] + qwen_datas,
    hiddenimports=['main', 'huggingface_hub', 'huggingface_hub.utils', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'starlette.background'] + qwen_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['webview', 'PyQt5', 'matplotlib', 'notebook', 'pandas', 'sphinx', 'IPython', 'jedi', 'docutils', 'babel', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LocalTTSStudio',
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
    name='LocalTTSStudio',
)
app = BUNDLE(
    coll,
    name='Local TTS Studio.app',
    icon='local-tts-logo-new.icns',
    bundle_identifier='com.localtts.studio',
)
