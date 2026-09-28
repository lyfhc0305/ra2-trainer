# Build on Windows: python -m PyInstaller --noconfirm RA2Trainer.spec
from PyInstaller.utils.hooks import collect_dynamic_libs

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=collect_dynamic_libs('capstone'),
    datas=[],
    hiddenimports=['capstone'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unicorn'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='RA2Trainer',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
