from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import zipfile

from .archive import extract_zip_safely
from .paths import BIN_DIR, CACHE_DIR, TOOLS_DIR, ensure_directories
from .system_path import ensure_current_path

YTDLP_WIN_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_WIN_CHECKSUM_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
DENO_WIN_X64_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
DENO_WIN_ARM64_URL = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-pc-windows-msvc.zip"
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_WIN_CHECKSUM_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
MANIFEST_PATH = TOOLS_DIR / "managed-tools.json"
INSTALL_HINTS = {
    "yt-dlp": "Install it with your package manager (for example: brew install yt-dlp, or pipx install yt-dlp) and make sure it is on PATH.",
    "ffmpeg": "Install it with your package manager (for example: brew install ffmpeg, or sudo apt install ffmpeg).",
    "deno": "It is optional; some sites such as YouTube need it. Install it from https://deno.com (for example: brew install deno).",
}
IS_WINDOWS = os.name == "nt"
_TOOLS_LOCK = threading.RLock()


def _run(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    flags = 0x08000000 if IS_WINDOWS else 0
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="backslashreplace", timeout=timeout, creationflags=flags)


def _valid(executable: Path | str, args: list[str]) -> bool:
    try:
        result = _run([str(executable), *args])
        return result.returncode == 0
    except Exception:
        return False


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for piece in str(value).strip().split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _valid_deno(executable: Path | str) -> bool:
    try:
        result = _run([str(executable), "--version"])
        if result.returncode != 0:
            return False
        lines = (result.stdout or "").splitlines()
        first = lines[0] if lines else ""
        version = first.split()[1] if len(first.split()) >= 2 else ""
        return _version_tuple(version) >= (2, 3, 0)
    except Exception:
        return False


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "VideoHaul/0.1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


class ChecksumMismatch(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "VideoHaul/0.1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(1024 * 1024).decode("utf-8", errors="replace")


def expected_sha256(checksum_text: str, file_name: str) -> str:
    candidates = []
    for line in str(checksum_text or "").splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith("hash") and ":" in stripped:
            stripped = stripped.split(":", 1)[1].strip()
        parts = stripped.split()
        if not parts:
            continue
        digest = parts[0].lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            continue
        named = parts[-1].lstrip("*") if len(parts) > 1 else ""
        if named == file_name:
            return digest
        if not named:
            candidates.append(digest)
    return candidates[0] if len(candidates) == 1 else ""


def verify_checksum(path: Path, checksum_url: str, file_name: str, fetcher=None) -> str:
    text = (fetcher or _fetch_text)(checksum_url)
    expected = expected_sha256(text, file_name)
    if not expected:
        raise ChecksumMismatch(f"No published SHA-256 checksum was found for {file_name}")
    actual = _file_sha256(path)
    if actual != expected:
        raise ChecksumMismatch(f"{file_name} did not match its published SHA-256 checksum")
    return actual


def _download_cached(url: str, destination: Path, validator=None, checksum_url: str = "", checksum_name: str = "", force: bool = False) -> Path:
    if force:
        destination.unlink(missing_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        if validator is None or validator(destination):
            return destination
        destination.unlink(missing_ok=True)
    _download(url, destination)
    if checksum_url:
        try:
            verify_checksum(destination, checksum_url, checksum_name or url.rsplit("/", 1)[-1])
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    if validator is not None and not validator(destination):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded cache artifact failed validation: {destination.name}")
    return destination


def _read_manifest() -> dict:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    if "yt-dlp" in value and "yt_dlp" not in value:
        value["yt_dlp"] = value["yt-dlp"]
    value.pop("yt-dlp", None)
    return value


def _write_manifest(value: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, MANIFEST_PATH)


def _record(name: str, executable: str | Path, source: str) -> None:
    with _TOOLS_LOCK:
        manifest = _read_manifest()
        manifest[name] = {"path": str(executable), "source": source}
        _write_manifest(manifest)


def _managed_binary(value: str | Path) -> bool:
    try:
        Path(value).resolve().relative_to(BIN_DIR.resolve())
        return True
    except Exception:
        return False


def _system_executable(name: str, args: list[str], validator=None) -> str | None:
    found = shutil.which(name)
    if not found or _managed_binary(found):
        return None
    check = validator or (lambda value: _valid(value, args))
    return found if check(found) else None


def _prepare_bin() -> None:
    ensure_directories()
    ensure_current_path(BIN_DIR)


def ensure_ytdlp(force: bool = False) -> str:
    _prepare_bin()
    system = _system_executable("yt-dlp", ["--version"])
    if system and not force:
        _record("yt_dlp", system, "system")
        return system
    if not IS_WINDOWS:
        raise RuntimeError("yt-dlp is required and was not found. " + INSTALL_HINTS["yt-dlp"])
    managed = BIN_DIR / "yt-dlp.exe"
    if not force and _valid(managed, ["--version"]):
        _record("yt_dlp", managed, "videohaul-bin")
        return str(managed)
    cached = CACHE_DIR / "yt-dlp.exe"
    _download_cached(YTDLP_WIN_URL, cached, lambda value: _valid(value, ["--version"]), YTDLP_WIN_CHECKSUM_URL, "yt-dlp.exe", force=force)
    pending = managed.with_name("yt-dlp.pending.exe")
    pending.unlink(missing_ok=True)
    try:
        shutil.copy2(cached, pending)
        if not _valid(pending, ["--version"]):
            raise RuntimeError("Managed yt-dlp failed validation")
        os.replace(pending, managed)
    finally:
        pending.unlink(missing_ok=True)
    _record("yt_dlp", managed, "videohaul-bin")
    return str(managed)


def ensure_deno(force: bool = False) -> str:
    _prepare_bin()
    system = _system_executable("deno", ["--version"], _valid_deno)
    if system and not force:
        _record("deno", system, "system")
        return system
    if not IS_WINDOWS:
        raise RuntimeError("Deno was not found. " + INSTALL_HINTS["deno"])
    managed = BIN_DIR / "deno.exe"
    if not force and _valid_deno(managed):
        _record("deno", managed, "videohaul-bin")
        return str(managed)
    machine = platform.machine().lower()
    url = DENO_WIN_ARM64_URL if machine in {"arm64", "aarch64"} else DENO_WIN_X64_URL
    archive = CACHE_DIR / ("deno-arm64.zip" if machine in {"arm64", "aarch64"} else "deno-x64.zip")
    _download_cached(url, archive, lambda value: zipfile.is_zipfile(value), url + ".sha256sum", url.rsplit("/", 1)[-1], force=force)
    pending = managed.with_name("deno.pending.exe")
    pending.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            member = next((name for name in bundle.namelist() if name.lower().endswith("deno.exe")), None)
            if not member:
                archive.unlink(missing_ok=True)
                raise RuntimeError("Deno archive did not contain deno.exe")
            with bundle.open(member) as source, pending.open("wb") as output:
                shutil.copyfileobj(source, output)
        if not _valid_deno(pending):
            raise RuntimeError("Managed Deno failed validation")
        os.replace(pending, managed)
    finally:
        pending.unlink(missing_ok=True)
    _record("deno", managed, "videohaul-bin")
    return str(managed)


def _ffmpeg_system() -> tuple[str, str] | None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe and not _managed_binary(ffmpeg) and not _managed_binary(ffprobe) and _valid(ffmpeg, ["-version"]) and _valid(ffprobe, ["-version"]):
        return ffmpeg, ffprobe
    return None


def _install_ffmpeg_archive(archive: Path, managed_ffmpeg: Path, managed_ffprobe: Path) -> None:
    pending_ffmpeg = managed_ffmpeg.with_name("ffmpeg.pending.exe")
    pending_ffprobe = managed_ffprobe.with_name("ffprobe.pending.exe")
    pending_ffmpeg.unlink(missing_ok=True)
    pending_ffprobe.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="videohaul-ffmpeg-") as directory:
            with zipfile.ZipFile(archive) as bundle:
                extract_zip_safely(bundle, directory)
            root = Path(directory)
            source_ffmpeg = next(root.rglob("ffmpeg.exe"), None)
            source_ffprobe = next(root.rglob("ffprobe.exe"), None)
            if not source_ffmpeg or not source_ffprobe:
                raise RuntimeError("FFmpeg archive was incomplete")
            if not _valid(source_ffmpeg, ["-version"]) or not _valid(source_ffprobe, ["-version"]):
                raise RuntimeError("Downloaded FFmpeg archive failed validation")
            shutil.copy2(source_ffmpeg, pending_ffmpeg)
            shutil.copy2(source_ffprobe, pending_ffprobe)
        if not _valid(pending_ffmpeg, ["-version"]) or not _valid(pending_ffprobe, ["-version"]):
            raise RuntimeError("Managed FFmpeg failed validation")
        os.replace(pending_ffmpeg, managed_ffmpeg)
        os.replace(pending_ffprobe, managed_ffprobe)
    finally:
        pending_ffmpeg.unlink(missing_ok=True)
        pending_ffprobe.unlink(missing_ok=True)


def ensure_ffmpeg(force: bool = False) -> str:
    _prepare_bin()
    system = _ffmpeg_system()
    if system and not force:
        directory = str(Path(system[0]).parent)
        _record("ffmpeg", directory, "system")
        return directory
    if not IS_WINDOWS:
        raise RuntimeError("FFmpeg and FFprobe are required and were not found. " + INSTALL_HINTS["ffmpeg"])
    managed_ffmpeg = BIN_DIR / "ffmpeg.exe"
    managed_ffprobe = BIN_DIR / "ffprobe.exe"
    if not force and _valid(managed_ffmpeg, ["-version"]) and _valid(managed_ffprobe, ["-version"]):
        _record("ffmpeg", BIN_DIR, "videohaul-bin")
        return str(BIN_DIR)
    archive = CACHE_DIR / "ffmpeg-release-essentials.zip"
    _download_cached(FFMPEG_WIN_URL, archive, lambda value: zipfile.is_zipfile(value), FFMPEG_WIN_CHECKSUM_URL, "ffmpeg-release-essentials.zip", force=force)
    try:
        _install_ffmpeg_archive(archive, managed_ffmpeg, managed_ffprobe)
    except (zipfile.BadZipFile, RuntimeError):
        archive.unlink(missing_ok=True)
        _download_cached(FFMPEG_WIN_URL, archive, lambda value: zipfile.is_zipfile(value), FFMPEG_WIN_CHECKSUM_URL, "ffmpeg-release-essentials.zip")
        _install_ffmpeg_archive(archive, managed_ffmpeg, managed_ffprobe)
    _record("ffmpeg", BIN_DIR, "videohaul-bin")
    return str(BIN_DIR)


def optional_deno() -> str:
    try:
        return ensure_deno()
    except Exception:
        return ""


def ensure_tools() -> dict[str, str]:
    with _TOOLS_LOCK:
        return {
            "yt_dlp": ensure_ytdlp(),
            "ffmpeg": ensure_ffmpeg(),
            "deno": optional_deno(),
        }


def js_runtime_args(deno_path: str) -> list[str]:
    value = str(deno_path or "").strip()
    return ["--js-runtimes", f"deno:{value}"] if value else []
