from __future__ import annotations

import importlib.util
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


BACKEND_ZIP_URL = "https://growthintel-2.vercel.app/backend-api-live.zip"


def _ensure_backend_source() -> Path:
    target = Path(tempfile.gettempdir()) / "growthintel_backend_live"
    for existing_main in target.rglob("app/main.py"):
        return existing_main.parents[1]

    target.mkdir(parents=True, exist_ok=True)
    archive_path = target / "backend-api-live.zip"
    urllib.request.urlretrieve(BACKEND_ZIP_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(target)
    for extracted_main in target.rglob("app/main.py"):
        return extracted_main.parents[1]
    return target


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
