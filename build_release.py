#!/usr/bin/env python3
"""Build a portable Windows x64 ZIP. Run with 64-bit Python 3.11+ on Windows."""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import venv
import zipfile

BASE = Path(__file__).resolve().parent


def read_version(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
            break
    raise ValueError("change_id.py must define __version__ = 'x.y.z'")


def run(command, **kwargs):
    subprocess.run([str(arg) for arg in command], check=True, **kwargs)


def publish_zip(bundle, target):
    """Publish only a complete, CRC-verified archive; preserve an older release on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=target.parent) as temporary:
        pending = Path(temporary) / "release.zip"
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(bundle))
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP integrity check failed")
            if not {"change_id.exe", "config.toml"} <= set(archive.namelist()):
                raise RuntimeError("Release is incomplete")
        os.replace(pending, target)


def build(config_path):
    if (sys.platform != "win32" or struct.calcsize("P") != 8
            or platform.machine().lower() not in {"amd64", "x86_64"}
            or sys.version_info < (3, 11)):
        raise RuntimeError("Build requires Windows x64 and 64-bit Python 3.11+")
    import change_id
    version = read_version(BASE / "change_id.py")
    config_path = config_path.resolve(strict=True)
    config = change_id.load_config(config_path)
    reader = (BASE / config["reader"]["library_path"]).resolve()
    printer_src = (BASE / config["printer"]["library_path"]).resolve()
    printer_project = printer_src.parent
    timini = (BASE / config["printer"]["timini_path"]).resolve()
    for required in (reader / "wcopy_reader.py",
                     printer_project / "pyproject.toml", timini / "timiniprint" / "__init__.py"):
        if not required.is_file():
            raise FileNotFoundError(f"Required source missing: {required}")

    # A fresh environment prevents accidental use of packages from the developer's venv.
    with tempfile.TemporaryDirectory(prefix="change-id-build-") as temporary:
        work = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(work / "venv")
        python = work / "venv" / "Scripts" / "python.exe"
        run([python, "-m", "pip", "install", "PyInstaller>=6,<7", "PyMySQL>=1.1,<2",
             "cryptography>=42", "hidapi>=0.14,<1", str(printer_project) + "[printer]"])
        run([python, "-m", "pip", "check"])
        manifest = work / "cyberwall_timini.json"
        # Use the installed printer library's own revision/cleanliness validation.
        run([python, "-c", (
            "import json,sys; from pathlib import Path; "
            "from cyberwall_id_printer.backends.timini import load_timini,TIMINI_REVISION; "
            "load_timini(sys.argv[1]); "
            "Path(sys.argv[2]).write_text(json.dumps({'revision':TIMINI_REVISION}),encoding='utf-8')"
        ), timini, manifest])
        spec = work / "change_id.spec"
        spec.write_text(f'''
from PyInstaller.utils.hooks import collect_all
packages = ['cyberwall_id_printer', 'timiniprint', 'bleak', 'pymysql', 'cryptography']
datas = [({str(manifest)!r}, '.')]
binaries = []
hiddenimports = ['wcopy_reader', 'hid', 'bleak.backends.winrt.client', 'bleak.backends.winrt.scanner']
for package in packages:
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
a = Analysis([{str(BASE / 'change_id.py')!r}],
             pathex={[str(BASE), str(reader), str(timini)]!r},
             binaries=binaries, datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='change_id', console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='change_id', upx=False)
''', encoding="utf-8")
        # Collection hooks run in subprocesses and must see the local source packages too.
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(reader), str(timini)))
        run([python, "-m", "PyInstaller", "--clean", "--noconfirm",
             "--distpath", work / "dist", "--workpath", work / "work", spec], env=env)
        bundle = work / "dist" / "change_id"
        shutil.copyfile(config_path, bundle / "config.toml")
        exe = bundle / "change_id.exe"
        if not exe.is_file() or not (bundle / "_internal").is_dir():
            raise RuntimeError("PyInstaller did not produce a complete application")
        # Test an independently copied release with no source paths or Python environment.
        smoke = work / "portable-test"
        shutil.copytree(bundle, smoke)
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.upper().startswith(("PYTHON", "CYBERWALL_"))}
        clean_env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        result = subprocess.run([str(smoke / "change_id.exe"), "--self-test"],
                                cwd=work, env=clean_env, capture_output=True, timeout=90)
        marker = ("CHANGE_ID_SELF_TEST_OK " + version).encode("ascii")
        if result.returncode or marker not in result.stdout:
            raise RuntimeError("Portable runtime self-test failed:\n" +
                               (result.stdout + result.stderr).decode("utf-8", errors="replace"))
        target = BASE / "bulds" / f"change_id_v{version}_windows_x64.zip"
        publish_zip(bundle, target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE / "change_id.toml",
                        help="configuration to include as config.toml")
    args = parser.parse_args()
    try:
        target = build(args.config)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"BUILD FAILED: {exc}\nNo new release archive was published.", file=sys.stderr)
        return 1
    print(f"Release ready: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
