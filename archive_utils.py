from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = ROOT / "outputs" / "_archive"


def archive_existing_path(path: Path) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        relative = Path(path.name)

    archived_path = ARCHIVE_ROOT / timestamp / relative
    archived_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, archived_path)
    return archived_path


def archive_existing_tree(path: Path) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        relative = Path(path.name)

    archived_path = ARCHIVE_ROOT / timestamp / relative
    archived_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, archived_path, dirs_exist_ok=True)
    return archived_path
