"""Environment setup for generate_tiles.ipynb.

Installs the Python packages the notebook needs and, on Colab/Linux, Blender.
Kept free of third-party imports so it can run before anything is installed.
"""

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

BLENDER_VER = "4.1.1"

# pip requirement -> importable module name
BASE_PACKAGES = {
    "shapely": "shapely",
    "trimesh": "trimesh",
    "geopandas": "geopandas",
    "pyproj": "pyproj",
    "matplotlib": "matplotlib",
    "ipyleaflet": "ipyleaflet",
    "ipywidgets": "ipywidgets",
}

OUTPUT_DIRS = ("raw_tiles", "processed_tiles", "old_meshes")


def install_packages(dtcc_core_commit):
    """pip install any missing package, with dtcc-core pinned to a commit."""
    packages = dict(BASE_PACKAGES)
    dtcc = f"git+https://github.com/dtcc-platform/dtcc-core.git@{dtcc_core_commit}"
    packages[dtcc] = "dtcc_core"

    missing = [pkg for pkg, module in packages.items() if importlib.util.find_spec(module) is None]
    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], check=True)
        importlib.invalidate_caches()
    else:
        print("All required packages are already installed.")


def install_blender(version=BLENDER_VER):
    """Make `blender` available on PATH, downloading it on Colab/Linux."""
    found = shutil.which("blender")
    if found:
        print(f"Blender found: {found}")
        return
    if sys.platform != "linux":
        print(f"Blender not found on PATH. Please install Blender {version} and ensure it is on your PATH.")
        return

    minor = ".".join(version.split(".")[:2])
    url = f"https://download.blender.org/release/Blender{minor}/blender-{version}-linux-x64.tar.xz"
    root = Path("/content") if Path("/content").is_dir() else Path.home()
    archive = root / "blender.tar.xz"
    print(f"Installing Blender {version}…")
    subprocess.run(["wget", "-q", url, "-O", str(archive)], check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(root)], check=True)
    archive.unlink(missing_ok=True)

    binary = root / f"blender-{version}-linux-x64" / "blender"
    link = Path("/usr/local/bin/blender")
    try:
        if not link.exists():
            link.symlink_to(binary)
    except PermissionError:
        # Not root (e.g. local Linux): put the folder on PATH for this kernel.
        os.environ["PATH"] = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"
    print(f"Blender installed: {shutil.which('blender') or binary}")


def make_dirs(dirs=OUTPUT_DIRS):
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def setup(dtcc_core_commit, blender=True):
    """Everything section 2 of the notebook needs before importing the pipeline."""
    install_packages(dtcc_core_commit)
    if blender:
        install_blender()
    make_dirs()
