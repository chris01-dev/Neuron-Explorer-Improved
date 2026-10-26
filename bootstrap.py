"""NeuronExplorer.exe bootstrap: everything a fresh machine needs before the window can open.

First run: unpacks the bundled app (when installed to a standalone folder), fetches uv if the machine
has none, and runs `uv sync` to install Python + the CUDA stack (a few GB, one time). Then it starts
the desktop window and stays until the window is closed. Extra command-line arguments pass through
to desktop.py (e.g. --smoke N for testing).
"""
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

APP_FILES = ("pyproject.toml", "uv.lock", "desktop.py", ".python-version", "README.md")
APP_DIRS = ("explorer", "sweep", "scripts")


def base_dir():
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    here = os.path.dirname(sys.executable)
    if all(os.path.exists(os.path.join(here, f)) for f in ("pyproject.toml", "explorer")):
        return here  # dropped into an existing checkout: manage it in place
    home = os.path.join(os.environ.get("LOCALAPPDATA", here), "NeuronExplorerImproved")
    os.makedirs(home, exist_ok=True)
    data = getattr(sys, "_MEIPASS", None)
    if data and not os.path.exists(os.path.join(home, "desktop.py")):
        print(f"[setup] unpacking the app into {home} ...")
        for name in APP_FILES:
            src = os.path.join(data, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(home, name))
        for name in APP_DIRS:
            src, dst = os.path.join(data, name), os.path.join(home, name)
            if os.path.isdir(src) and not os.path.isdir(dst):
                shutil.copytree(src, dst)
        print("[setup] unpacked.")
    return home


def ensure_uv(home):
    uv = shutil.which("uv")
    if uv:
        return uv
    local = os.path.join(home, "uv.exe")
    if os.path.exists(local):
        return local
    print("[setup] downloading uv (the Python manager, ~20 MB) ...")
    url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
    with urllib.request.urlopen(url, timeout=300) as r:
        with zipfile.ZipFile(io.BytesIO(r.read())) as z:
            for n in z.namelist():
                if n.endswith("uv.exe"):
                    with z.open(n) as src, open(local, "wb") as out:
                        shutil.copyfileobj(src, out)
    print("[setup] uv downloaded.")
    return local


def ensure_env(home, uv):
    venv_py = os.path.join(home, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        return venv_py
    print("[setup] first run: installing the environment (PyTorch CUDA and friends, a few GB) ...")
    print("[setup] this can take 5-15 minutes depending on your connection. It only happens once.")
    env = dict(os.environ, UV_PYTHON_DOWNLOADS="automatic")
    subprocess.run([uv, "sync"], cwd=home, env=env)
    if not os.path.exists(venv_py):
        print("[setup] install did not finish; scroll up for the error.")
        input("press Enter to close")
        sys.exit(1)
    return venv_py


def main():
    home = base_dir()
    print(f"[start] Neuron Explorer - {home}")
    try:
        uv = ensure_uv(home)
        venv_py = ensure_env(home, uv)
    except Exception as e:
        print(f"[setup] failed: {e!r}")
        input("press Enter to close")
        sys.exit(1)
    code = subprocess.run([venv_py, "desktop.py", *sys.argv[1:]], cwd=home).returncode
    if code != 0:
        print(f"[start] the window exited with code {code}.")
        input("press Enter to close")


if __name__ == "__main__":
    main()
