from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


BACKEND_ZIP_URL = "https://growthintel.vercel.app/backend-api-live.zip"


def _find_app_main(root: Path) -> Path | None:
    for candidate in root.rglob("main.py"):
        if candidate.parent.name == "app":
            return candidate
    return None


def _ensure_backend_source() -> Path:
    target = Path(tempfile.gettempdir()) / "growthintel_backend_live_v20260721"

    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    archive_path = target / "backend-api-live.zip"
    local_archives = [
        Path("/opt/render/project/src/b.zip"),
        Path("/opt/render/project/src/backend-api-live.zip"),
        Path.cwd().parent / "b.zip",
    ]
    source_archive = next((path for path in local_archives if path.exists()), None)
    if source_archive:
        shutil.copyfile(source_archive, archive_path)
    else:
        urllib.request.urlretrieve(BACKEND_ZIP_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()[:12]
        archive.extractall(target)
    extracted_main = _find_app_main(target)
    if extracted_main and extracted_main.exists():
        return extracted_main.parents[1]
    raise RuntimeError(f"Could not find app/main.py after extracting backend zip. First entries: {names}")


def _load_real_app():
    source_root = _ensure_backend_source()
    real_app_dir = source_root / "app"
    package = sys.modules.get("app")
    if package is not None:
        package.__path__ = [str(real_app_dir)]
        package.__file__ = str(real_app_dir / "__init__.py")
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

    spec = importlib.util.spec_from_file_location(
        "_growthintel_real_app_main",
        real_app_dir / "main.py",
        submodule_search_locations=[str(real_app_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load GrowthIntel backend package.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


app = _load_real_app()
