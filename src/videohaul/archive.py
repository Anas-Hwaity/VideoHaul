from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import zipfile


def extract_zip_safely(bundle: zipfile.ZipFile, destination: str | Path) -> None:
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for member in bundle.infolist():
        name = str(member.filename or "").replace("\\", "/")
        path = PurePosixPath(name)
        windows_path = PureWindowsPath(name)
        if not name or path.is_absolute() or windows_path.is_absolute() or bool(windows_path.drive) or any(part in {"", ".", ".."} for part in path.parts):
            raise RuntimeError("Archive contains an unsafe path")
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise RuntimeError("Archive contains a symbolic link")
        target = root.joinpath(*path.parts).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("Archive entry escapes the extraction directory") from exc
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
