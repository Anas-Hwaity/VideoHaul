from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import re
import shutil
import threading


from .unicode_text import normalize_unicode, truncate_filename_component


OUTPUT_PUBLISH_LOCK = threading.RLock()


WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}


def safe_name(value: str, fallback: str = "media", max_utf8_bytes: int = 180, max_utf16_units: int = 180) -> str:
    cleaned = normalize_unicode(value)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    cleaned = truncate_filename_component(cleaned, max_utf8_bytes=max_utf8_bytes, max_utf16_units=max_utf16_units).rstrip(" .")
    return cleaned if cleaned not in {"", ".", ".."} else fallback


def safe_filename_component(value: str, fallback: str = "media") -> str:
    text = normalize_unicode(value)
    suffix = Path(text).suffix
    if not suffix or len(suffix) > 20:
        return safe_name(text, fallback)
    raw_extension = suffix[1:]
    extension = safe_name(raw_extension, "bin", max_utf8_bytes=18, max_utf16_units=18)
    rendered_suffix = "." + extension
    stem_budget_utf8 = max(32, 180 - len(rendered_suffix.encode("utf-8")))
    stem_budget_utf16 = max(32, 180 - len(rendered_suffix.encode("utf-16-le")) // 2)
    stem = safe_name(text[:-len(suffix)], fallback, stem_budget_utf8, stem_budget_utf16)
    return stem + rendered_suffix


MANIFEST_CONTAINERS = {"m3u8", "m3u", "mpd", "ism", "f4m", "manifest", "hls", "dash", "playlist"}

MANIFEST_DELIVERED_CONTAINER = "mp4"

MANIFEST_DELIVERED_AUDIO_CONTAINER = "m4a"


def delivered_container(value: str, audio_only: bool = False) -> str:
    text = str(value or "").strip().lower().lstrip(".")
    if not text:
        return ""
    if text in MANIFEST_CONTAINERS:
        return MANIFEST_DELIVERED_AUDIO_CONTAINER if audio_only else MANIFEST_DELIVERED_CONTAINER
    return text


def output_extension(job) -> str:
    configured = str(job.settings.container or "").strip().lower()
    if configured not in {"", "auto", "source"}:
        return configured.lstrip(".")
    if str(job.selected_video or "") == "audio_only":
        audio_output = str(job.settings.audio_output_format or "source").strip().lower()
        if audio_output not in {"", "source"}:
            return audio_output
    analysis = job.analysis
    if analysis:
        audio_only = str(job.selected_video or "") == "audio_only"
        selected = next((item for item in analysis.video_streams if item.stream_id == job.selected_video), None)
        if selected and selected.container:
            resolved = delivered_container(selected.container, audio_only)
            if resolved:
                return safe_name(resolved, "media").lower()
        selected_audio = next((item for item in analysis.audio_streams if item.stream_id in job.settings.selected_audio), None)
        if selected_audio and selected_audio.container:
            resolved = delivered_container(selected_audio.container, True)
            if resolved:
                return safe_name(resolved, "media").lower()
    return "media"


def render_output_template(job) -> str:
    analysis = job.analysis
    playlist = dict(analysis.playlist or {}) if analysis else {}
    selected = None
    if analysis:
        selected = next((item for item in analysis.video_streams if item.stream_id == job.selected_video), None)
        if selected is None and str(job.selected_video or "") == "bestvideo*+bestaudio/best" and analysis.video_streams:
            selected = max(analysis.video_streams, key=lambda item: ((item.height or 0), (item.fps or 0), (item.bitrate or 0), item.stream_id))
    values = {
        "title": safe_name(analysis.title if analysis else "media"),
        "id": safe_name(analysis.platform_media_id if analysis else job.job_id, job.job_id),
        "creator": safe_name(analysis.creator if analysis else "", "unknown"),
        "channel": safe_name(analysis.channel if analysis else "", "unknown"),
        "platform": safe_name(analysis.platform if analysis else "", "source"),
        "upload_date": safe_name(analysis.upload_date if analysis else "", "unknown-date"),
        "date": safe_name(analysis.upload_date if analysis else "", "unknown-date"),
        "resolution": safe_name(selected.resolution_family if selected else "audio" if str(job.selected_video or "") == "audio_only" else "unknown"),
        "quality": safe_name(selected.resolution_family if selected else str(job.selected_video or "best"), "best"),
        "playlist_title": safe_name(str(playlist.get("title") or ""), "playlist"),
        "playlist_index": int(playlist.get("index") or playlist.get("batch_index") or 0),
        "batch_index": int(playlist.get("batch_index") or playlist.get("index") or 0),
        "ext": output_extension(job),
    }
    template = str(job.settings.filename_template or "%(title)s.%(ext)s")
    rendered = template
    for key, value in values.items():
        if key == "ext" and value == "media":
            continue
        rendered = rendered.replace(f"%({key})s", str(value))
        if isinstance(value, int):
            rendered = rendered.replace(f"%({key})03d", f"{value:03d}")
            rendered = rendered.replace(f"%({key})02d", f"{value:02d}")
            rendered = rendered.replace(f"%({key})d", str(value))
    if "%(" in rendered:
        raise ValueError("Unknown or unsupported filename template field")
    posix_path = PurePosixPath(rendered)
    windows_path = PureWindowsPath(rendered)
    if not rendered or rendered in {".", ".."} or "/" in rendered or "\\" in rendered or posix_path.is_absolute() or windows_path.is_absolute() or bool(windows_path.drive) or bool(windows_path.root):
        raise ValueError("Filename template must resolve to one filename inside the download destination")
    filename = safe_filename_component(rendered)
    if not filename or filename in {".", ".."}:
        raise ValueError("Filename template resolved to an empty filename")
    return filename


def resolve_collision(destination: Path, rendered: str, policy: str) -> Path:
    target = destination / rendered
    normalized = str(policy or "rename").strip().lower()
    if not target.exists():
        return target
    if normalized == "overwrite":
        return target
    if normalized == "ask":
        raise FileExistsError(str(target))
    if normalized != "rename":
        raise ValueError(f"Unknown collision policy: {policy}")
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 10001):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Unable to allocate a collision-free output filename")



def private_staging_directory(destination: Path, job_id: str) -> Path:
    root = Path(destination)
    staging = root / f".videohaul-partial-{safe_name(str(job_id or 'job'), 'job')[:32]}"
    if staging.is_symlink():
        raise RuntimeError("Private staging path cannot be a symbolic link")
    staging.mkdir(parents=True, exist_ok=True)
    if staging.is_symlink() or staging.resolve().parent != root.resolve():
        raise RuntimeError("Private staging directory escaped the download destination")
    return staging


def path_is_within(path: str | Path, directory: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(directory).resolve())
        return True
    except Exception:
        return False


def private_staging_path(destination: Path, job_id: str, candidate: str | Path) -> bool:
    root = Path(destination) / f".videohaul-partial-{safe_name(str(job_id or 'job'), 'job')[:32]}"
    return path_is_within(candidate, root)


def cleanup_private_staging(directory: str | Path, destination: str | Path, job_id: str) -> bool:
    target = Path(directory)
    expected = Path(destination) / f".videohaul-partial-{safe_name(str(job_id or 'job'), 'job')[:32]}"
    try:
        if target.resolve() != expected.resolve() or not target.name.startswith(".videohaul-partial-"):
            return False
    except Exception:
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def reserve_publish_target(destination: Path, rendered: str, policy: str) -> tuple[Path, bool]:
    target = Path(destination) / rendered
    normalized = str(policy or "rename").strip().lower()
    if normalized == "overwrite":
        return target, False
    candidates = [target]
    if normalized == "rename":
        candidates.extend(target.with_name(f"{target.stem} ({index}){target.suffix}") for index in range(2, 10001))
    elif normalized != "ask":
        raise ValueError(f"Unknown collision policy: {policy}")
    for candidate in candidates:
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if normalized == "ask":
                raise FileExistsError(str(candidate))
            continue
        else:
            os.close(descriptor)
            return candidate, True
    raise RuntimeError("Unable to allocate a collision-free output filename")


def publish_staged_file(produced: Path, destination: Path, policy: str, final_name: str | None = None, staging_root: Path | None = None) -> Path:
    source = Path(produced)
    if staging_root is not None and not path_is_within(source, staging_root):
        raise RuntimeError("Refusing to publish output from outside the private staging directory")
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if source.stat().st_size <= 0:
        raise RuntimeError(f"Refusing to publish an empty output file: {source}")
    target_name = str(final_name or source.name)
    target_directory = Path(destination)
    target_directory.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PUBLISH_LOCK:
        final, reserved = reserve_publish_target(target_directory, target_name, policy)
        try:
            os.replace(source, final)
        except Exception:
            if reserved:
                final.unlink(missing_ok=True)
            raise
    return final


def publish_staged_group(entries: list[tuple[Path, str, str]], destination: Path, staging_root: Path) -> list[Path]:
    root = Path(staging_root)
    target_directory = Path(destination)
    target_directory.mkdir(parents=True, exist_ok=True)
    planned: list[dict] = []
    moved: list[dict] = []
    with OUTPUT_PUBLISH_LOCK:
        try:
            for index, (raw_source, raw_name, raw_policy) in enumerate(entries):
                source = Path(raw_source)
                if not path_is_within(source, root):
                    raise RuntimeError("Refusing to publish output from outside the private staging directory")
                if not source.is_file() or source.stat().st_size <= 0:
                    raise RuntimeError(f"Refusing to publish an unusable staged file: {source}")
                policy = str(raw_policy or "rename").strip().lower()
                final, reserved = reserve_publish_target(target_directory, str(raw_name), policy)
                backup = None
                if policy == "overwrite" and final.exists():
                    backup = root / f".videohaul-backup-{index}-{safe_filename_component(final.name, 'backup')}"
                    counter = 1
                    while backup.exists():
                        backup = root / f".videohaul-backup-{index}-{counter}-{safe_filename_component(final.name, 'backup')}"
                        counter += 1
                    os.replace(final, backup)
                planned.append({"source": source, "final": final, "reserved": reserved, "backup": backup})
            for plan in planned:
                os.replace(plan["source"], plan["final"])
                moved.append(plan)
        except Exception:
            for plan in reversed(moved):
                final = plan["final"]
                source = plan["source"]
                try:
                    if final.exists() and not source.exists():
                        os.replace(final, source)
                except Exception:
                    pass
            for plan in reversed(planned):
                backup = plan["backup"]
                final = plan["final"]
                if backup is not None:
                    try:
                        if backup.exists():
                            os.replace(backup, final)
                    except Exception:
                        pass
                elif plan["reserved"]:
                    try:
                        if final.exists() and plan not in moved:
                            final.unlink(missing_ok=True)
                    except Exception:
                        pass
            raise
        for plan in planned:
            backup = plan["backup"]
            if backup is not None:
                backup.unlink(missing_ok=True)
    return [Path(plan["final"]) for plan in planned]

def remove_partial(path: str) -> bool:
    target = Path(path)
    if not path or not target.is_file():
        return False
    parent = target.parent
    if not parent.name.startswith(".videohaul-partial-") or parent.is_symlink():
        return False
    target.unlink()
    try:
        leftovers = [item for item in parent.iterdir() if item.suffix.casefold() not in {".ytdl", ".part"}]
        if not leftovers:
            for item in parent.iterdir():
                item.unlink(missing_ok=True)
            parent.rmdir()
    except OSError:
        pass
    return True
