#!/usr/bin/env python3
"""Check patch compatibility against a saved PVE filesystem: check_native.py ROOT."""
import importlib.util
from pathlib import Path
import sys
spec=importlib.util.spec_from_file_location('patcher',Path(__file__).resolve().parents[1]/'scripts/patch.py')
patcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(patcher)
root=Path(sys.argv[1])
for path,kind in patcher.NATIVE_FILES:
 text=(root/path).read_text()
 clean=patcher.transform(text,kind,remove=True)
 applied=patcher.transform(clean,kind)
 assert patcher.transform(applied,kind)==applied,(kind,'not idempotent')
 assert patcher.transform(applied,kind,remove=True)==clean,(kind,'not reversible')
 print('PASS',kind)
