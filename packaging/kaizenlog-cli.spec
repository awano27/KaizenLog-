# -*- mode: python ; coding: utf-8 -*-
"""One-folder CLI build; output directory is selected by build-portable.ps1."""

import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parent
PACKAGE_DATAS = collect_data_files("kaizenlog") + collect_data_files("tzdata")
DATAS = PACKAGE_DATAS + [
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "LICENSE"), "."),
]
HIDDENIMPORTS = collect_submodules("kaizenlog") + collect_submodules("requests") + collect_submodules("tzdata")


a = Analysis(
    [str(ROOT / "packaging" / "portable_cli.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)


def write_analysis_inventory(analysis):
    destination = os.environ.get("KAIZENLOG_MODULE_INVENTORY_PATH")
    if not destination:
        return
    modules = sorted({entry[0] for entry in analysis.pure if entry and isinstance(entry[0], str)})
    Path(destination).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "analysis": "PyInstaller.Analysis.pure",
                "entrypoint": "cli/kaizenlog.exe",
                "modules": modules,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


write_analysis_inventory(a)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kaizenlog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="kaizenlog",
)
