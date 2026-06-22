# -*- mode: python ; coding: utf-8 -*-
import glob
from PyInstaller.utils.hooks import collect_all

qwen_datas, qwen_binaries, qwen_hiddenimports = collect_all('qwen_tts')

# Resolve the bundled torch libomp regardless of the venv's Python version
_libomp_matches = glob.glob('venv/lib/python*/site-packages/torch/lib/libomp.dylib')
if not _libomp_matches:
    raise SystemExit("Could not find torch/lib/libomp.dylib under venv/ — is the venv set up?")
_libomp = _libomp_matches[0]

a = Analysis(
    ['app_launcher.py'],
    pathex=[],
    binaries=[('ffmpeg', '.'), (_libomp, '.')] + qwen_binaries,
    datas=[('static', 'static'), ('assets', 'assets')] + qwen_datas,
    hiddenimports=['main', 'huggingface_hub', 'huggingface_hub.utils', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'starlette.background', 'rumps', 'AppKit', 'Foundation', 'objc', 'PyObjCTools'] + qwen_hiddenimports,
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
