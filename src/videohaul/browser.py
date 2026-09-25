from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

from .archive import extract_zip_safely
from .paths import BROWSER_DIR, BROWSER_DIR as MANAGED_BROWSER_ROOT, CACHE_DIR, DATA_ROOT, ensure_directories

BROWSER_MANIFEST = DATA_ROOT / "browser.json"
CFT_METADATA_URL = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
IS_WINDOWS = os.name == "nt"
_BROWSER_LOCK = threading.RLock()


def _run(executable: str | Path) -> subprocess.CompletedProcess:
    flags = 0x08000000 if IS_WINDOWS else 0
    return subprocess.run([str(executable), "--version"], capture_output=True, text=True, encoding="utf-8", errors="backslashreplace", timeout=10, creationflags=flags)


def valid_browser(executable: str | Path) -> bool:
    try:
        path = Path(executable)
        if not path.is_file():
            return False
        result = _run(path)
        return result.returncode == 0
    except Exception:
        return False


def _read_manifest() -> dict:
    try:
        return json.loads(BROWSER_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_manifest(executable: str | Path, source: str) -> None:
    BROWSER_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temporary = BROWSER_MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps({"executable": str(Path(executable).resolve()), "source": source}, indent=2), encoding="utf-8")
    os.replace(temporary, BROWSER_MANIFEST)


def _manifest_browser() -> str | None:
    executable = str(_read_manifest().get("executable") or "")
    return executable if valid_browser(executable) else None


def _path_candidates() -> list[str]:
    names = ["msedge", "msedge.exe", "chrome", "chrome.exe", "chromium", "chromium.exe", "chromium-browser", "google-chrome"]
    values = []
    for name in names:
        found = shutil.which(name)
        if found and found not in values:
            values.append(found)
    return values


def _windows_install_candidates() -> list[Path]:
    values: list[Path] = []
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
    for value in roots:
        if not value:
            continue
        root = Path(value)
        values.extend([
            root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            root / "Google" / "Chrome" / "Application" / "chrome.exe",
            root / "Chromium" / "Application" / "chrome.exe",
        ])
    return values


def playwright_package_root() -> Path | None:
    try:
        import playwright
        return Path(playwright.__file__).resolve().parent
    except Exception:
        return None


def playwright_chromium_revision() -> str | None:
    root = playwright_package_root()
    if root is None:
        return None
    try:
        payload = json.loads((root / "driver" / "package" / "browsers.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    for item in payload.get("browsers") or []:
        if str(item.get("name") or "") == "chromium":
            value = str(item.get("revision") or "").strip()
            return value or None
    return None


def playwright_registry_root() -> Path | None:
    configured = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if configured == "0":
        package = playwright_package_root()
        return package / "driver" / "package" / ".local-browsers" if package is not None else None
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            root = Path(os.environ.get("INIT_CWD") or os.getcwd()) / root
        return root.resolve(strict=False)
    if IS_WINDOWS:
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local:
            return (Path(local) / "ms-playwright").resolve(strict=False)
        return (Path.home() / "AppData" / "Local" / "ms-playwright").resolve(strict=False)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Caches" / "ms-playwright").resolve(strict=False)
    cache = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
    return ((Path(cache).expanduser() if cache else Path.home() / ".cache") / "ms-playwright").resolve(strict=False)


def playwright_expected_executable() -> Path | None:
    revision = playwright_chromium_revision()
    root = playwright_registry_root()
    if not revision or root is None:
        return None
    directory = root / f"chromium-{revision}"
    if IS_WINDOWS:
        return directory / "chrome-win64" / "chrome.exe"
    machine = os.uname().machine.casefold() if hasattr(os, "uname") else ""
    if sys.platform == "darwin":
        bundle = "chrome-mac-arm64" if "arm" in machine or "aarch64" in machine else "chrome-mac-x64"
        return directory / bundle / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    bundle = "chrome-linux" if "arm" in machine or "aarch64" in machine else "chrome-linux64"
    return directory / bundle / "chrome"


def playwright_cache_roots() -> list[Path]:
    roots: list[Path] = []
    authoritative = playwright_registry_root()
    if authoritative is not None:
        roots.append(authoritative)
    if IS_WINDOWS:
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        profile = str(os.environ.get("USERPROFILE") or "").strip()
        if local:
            roots.append(Path(local) / "ms-playwright")
        if profile:
            roots.append(Path(profile) / "AppData" / "Local" / "ms-playwright")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def playwright_browser_present(executable: str | Path) -> bool:
    try:
        path = Path(executable)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def playwright_cache_candidates(include_expected: bool = True) -> list[Path]:
    candidates: list[Path] = []
    if include_expected:
        expected = playwright_expected_executable()
        if expected and expected not in candidates:
            candidates.append(expected)
    names = ("chrome.exe", "chromium.exe") if IS_WINDOWS else ("chrome", "chromium")
    for root in playwright_cache_roots():
        if not root.is_dir():
            continue
        for directory in sorted(root.glob("chromium-*"), reverse=True):
            if "headless" in directory.name.lower():
                continue
            for name in names:
                for executable in directory.rglob(name):
                    if executable not in candidates:
                        candidates.append(executable)
    return candidates


def find_playwright_browser() -> str | None:
    expected = playwright_expected_executable()
    revision = playwright_chromium_revision()
    root = playwright_registry_root()
    if expected is not None and playwright_browser_present(expected):
        return str(expected.resolve())
    if revision and root is not None:
        directory = root / f"chromium-{revision}"
        names = ("chrome.exe", "chromium.exe") if IS_WINDOWS else (("Google Chrome for Testing", "Chromium") if sys.platform == "darwin" else ("chrome", "chromium"))
        if directory.is_dir():
            for name in names:
                for candidate in directory.rglob(name):
                    if playwright_browser_present(candidate):
                        return str(candidate.resolve())
        return None
    for candidate in playwright_cache_candidates(include_expected=False):
        if playwright_browser_present(candidate):
            return str(candidate.resolve())
    manifest = _read_manifest()
    manifest_source = str(manifest.get("source") or "").casefold()
    manifest_executable = str(manifest.get("executable") or "")
    if "playwright" in manifest_source and playwright_browser_present(manifest_executable):
        return str(Path(manifest_executable).resolve())
    return None


def _managed_candidates() -> list[Path]:
    if not MANAGED_BROWSER_ROOT.is_dir():
        return []
    names = ("chrome.exe", "chromium.exe", "msedge.exe") if IS_WINDOWS else ("chrome", "chromium", "msedge")
    values: list[Path] = []
    for name in names:
        for executable in MANAGED_BROWSER_ROOT.rglob(name):
            if executable not in values:
                values.append(executable)
    return values


def find_browser(profile: str = "global") -> str | None:
    explicit = os.environ.get("VIDEOHAUL_BROWSER_EXECUTABLE")
    if explicit:
        if profile == "playwright" and playwright_browser_present(explicit):
            return str(Path(explicit).resolve())
        if profile != "playwright" and valid_browser(explicit):
            return str(Path(explicit))
    if profile == "playwright":
        playwright_browser = find_playwright_browser()
        if playwright_browser:
            return playwright_browser
    adopted = _manifest_browser()
    if adopted:
        return adopted
    for candidate in _managed_candidates():
        if valid_browser(candidate):
            return str(candidate)
    for candidate in _path_candidates():
        if valid_browser(candidate):
            return candidate
    if IS_WINDOWS:
        for candidate in _windows_install_candidates():
            if valid_browser(candidate):
                return str(candidate)
    return None



def complete_playwright_chromium_install(force: bool = False) -> None:
    command = [sys.executable, "-m", "playwright", "install", *((["--force"]) if force else []), "chromium"]
    flags = 0x08000000 if IS_WINDOWS else 0
    result = subprocess.run(command, text=True, encoding="utf-8", errors="backslashreplace", timeout=1800, creationflags=flags)
    if result.returncode != 0:
        raise RuntimeError(f"Playwright Chromium installation failed with exit code {result.returncode}")


def ensure_browser(profile: str = "playwright", force: bool = False) -> str:
    normalized = "playwright" if str(profile or "playwright").casefold() == "playwright" else "global"
    with _BROWSER_LOCK:
        if normalized == "playwright":
            existing = find_playwright_browser()
            if existing and not force:
                return adopt_browser(existing, "playwright-chromium", validate_process=False)
            if playwright_package_root() is None:
                raise RuntimeError("Playwright is not installed")
            complete_playwright_chromium_install(force=force)
            installed = find_playwright_browser()
            if not installed:
                raise RuntimeError("Playwright Chromium was installed but could not be located")
            return adopt_browser(installed, "playwright-chromium", validate_process=False)
        existing = find_browser("global")
        if existing:
            return existing
        if IS_WINDOWS:
            return install_managed_browser()
        raise RuntimeError("A compatible Chromium browser is required")


def adopt_browser(executable: str | Path, source: str = "existing", validate_process: bool = True) -> str:
    path = Path(executable).resolve()
    valid = valid_browser(path) if validate_process else playwright_browser_present(path)
    if not valid:
        raise RuntimeError(f"Browser failed validation: {path}")
    _write_manifest(path, source)
    os.environ["VIDEOHAUL_BROWSER_EXECUTABLE"] = str(path)
    return str(path)


def _bundle_root(executable: Path) -> Path:
    names = {"chrome-win64", "chrome-win32", "chrome-linux64", "chrome-mac-arm64", "chrome-mac-x64"}
    for parent in [executable.parent, *executable.parents]:
        if parent.name.lower() in names:
            return parent
    return executable.parent


def import_playwright_browser(executable: str | Path) -> str:
    source = Path(executable).resolve()
    if not valid_browser(source):
        raise RuntimeError(f"Playwright browser failed validation: {source}")
    ensure_directories()
    source_root = _bundle_root(source)
    destination = BROWSER_DIR / source_root.name
    expected = destination / source.relative_to(source_root)
    if valid_browser(expected):
        return adopt_browser(expected, "videohaul-imported-playwright")
    pending = BROWSER_DIR / (source_root.name + ".pending")
    if pending.exists():
        shutil.rmtree(pending, ignore_errors=True)
    try:
        shutil.copytree(source_root, pending)
        pending_executable = pending / source.relative_to(source_root)
        if not valid_browser(pending_executable):
            raise RuntimeError("Imported Playwright Chromium failed validation")
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        os.replace(pending, destination)
    finally:
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)
    return adopt_browser(expected, "videohaul-imported-playwright")


def _download(url: str, destination: Path) -> Path:
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
    return destination


def _chrome_download() -> tuple[str, str]:
    request = urllib.request.Request(CFT_METADATA_URL, headers={"User-Agent": "VideoHaul/0.1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    stable = payload.get("channels", {}).get("Stable", {})
    version = str(stable.get("version") or "")
    platform_key = "win64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() not in {"x86"} else "win32"
    downloads = stable.get("downloads", {}).get("chrome", [])
    url = next((str(item.get("url")) for item in downloads if item.get("platform") == platform_key and item.get("url")), "")
    if not version or not url:
        raise RuntimeError("Chrome for Testing metadata did not provide a compatible Windows download")
    return version, url


def install_managed_browser() -> str:
    if not IS_WINDOWS:
        raise RuntimeError("A compatible Chromium, Chrome, or Edge browser is required")
    ensure_directories()
    version, url = _chrome_download()
    destination = BROWSER_DIR / f"chrome-{version}"
    expected = next(destination.rglob("chrome.exe"), None) if destination.exists() else None
    if expected and valid_browser(expected):
        return adopt_browser(expected, "videohaul-managed")
    archive = CACHE_DIR / f"chrome-for-testing-{version}.zip"
    if not zipfile.is_zipfile(archive):
        archive.unlink(missing_ok=True)
        _download(url, archive)
    if not zipfile.is_zipfile(archive):
        archive.unlink(missing_ok=True)
        raise RuntimeError("Chrome for Testing archive failed validation")
    pending = BROWSER_DIR / f"chrome-{version}.pending"
    if pending.exists():
        shutil.rmtree(pending, ignore_errors=True)
    try:
        pending.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            extract_zip_safely(bundle, pending)
        pending_executable = next(pending.rglob("chrome.exe"), None)
        if not pending_executable or not valid_browser(pending_executable):
            raise RuntimeError("Managed Chrome for Testing failed validation")
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        os.replace(pending, destination)
    finally:
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)
    executable = next(destination.rglob("chrome.exe"), None)
    if not executable or not valid_browser(executable):
        raise RuntimeError("Managed Chrome for Testing installation failed validation")
    return adopt_browser(executable, "videohaul-managed")
