from __future__ import annotations

from dataclasses import asdict, fields

from .models import AudioStream, DownloadJob, JobSettings, JobStatus, MediaAnalysis, ProgressState, Stage, SubtitleTrack, VideoStream


def job_to_dict(job: DownloadJob, include_private: bool = False) -> dict:
    value = asdict(job)
    if include_private and job.status == JobStatus.DONE:
        from .media_candidates import redact_url, url_carries_secrets

        value["transfer_headers"] = {}
        if url_carries_secrets(str(value.get("source_url") or "")):
            value["source_url"] = redact_url(str(value.get("source_url") or ""))
        if url_carries_secrets(str(value.get("thumbnail_source_url") or "")):
            value["thumbnail_source_url"] = redact_url(str(value.get("thumbnail_source_url") or ""))
        analysis = value.get("analysis")
        if isinstance(analysis, dict):
            for key in ("source_url", "canonical_url", "thumbnail_url"):
                raw = str(analysis.get(key) or "")
                if url_carries_secrets(raw):
                    analysis[key] = redact_url(raw)
            metadata = dict(analysis.get("metadata") or {})
            analysis["metadata"] = {key: item for key, item in metadata.items() if not str(key).startswith("_")}
            raw_media_id = str(analysis.get("platform_media_id") or "")
            if url_carries_secrets(raw_media_id):
                analysis["platform_media_id"] = redact_url(raw_media_id)
            for collection in ("video_streams", "audio_streams"):
                for stream in analysis.get(collection) or []:
                    if isinstance(stream, dict):
                        stream["direct_stream_url"] = ""
    if not include_private:
        value.pop("transfer_headers", None)
        analysis = value.get("analysis")
        if isinstance(analysis, dict) and isinstance(analysis.get("metadata"), dict):
            analysis["metadata"] = {key: item for key, item in analysis["metadata"].items() if not str(key).startswith("_")}
        if isinstance(analysis, dict):
            for collection in ("video_streams", "audio_streams"):
                for stream in analysis.get(collection) or []:
                    if isinstance(stream, dict):
                        stream["direct_stream_available"] = bool(stream.get("direct_stream_url"))
                        stream["direct_stream_url"] = ""
    return value


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _fields_for(cls, value) -> dict:
    allowed = {item.name for item in fields(cls)}
    return {key: item for key, item in _mapping(value).items() if key in allowed}


def _enum_value(cls, value, fallback):
    try:
        return cls(value)
    except Exception:
        return fallback


def _video(data: dict) -> VideoStream | None:
    value = _fields_for(VideoStream, data)
    value["stream_id"] = str(value.get("stream_id") or "").strip()
    value["resolution_family"] = str(value.get("resolution_family") or "unknown")
    if not value["stream_id"]:
        return None
    try:
        return VideoStream(**value)
    except Exception:
        return None


def _audio(data: dict) -> AudioStream | None:
    value = _fields_for(AudioStream, data)
    value["stream_id"] = str(value.get("stream_id") or "").strip()
    if not value["stream_id"]:
        return None
    try:
        return AudioStream(**value)
    except Exception:
        return None


def _subtitle(data: dict) -> SubtitleTrack | None:
    value = _fields_for(SubtitleTrack, data)
    value["track_id"] = str(value.get("track_id") or "").strip()
    value["language_code"] = str(value.get("language_code") or "und")
    value["language_name"] = str(value.get("language_name") or value["language_code"] or "Unknown")
    if not value["track_id"]:
        return None
    try:
        return SubtitleTrack(**value)
    except Exception:
        return None


def _analysis(data: dict | None) -> MediaAnalysis | None:
    if not isinstance(data, dict) or not data:
        return None
    value = _fields_for(MediaAnalysis, data)
    value["video_streams"] = [stream for item in data.get("video_streams") or [] if isinstance(item, dict) for stream in [_video(item)] if stream is not None]
    value["audio_streams"] = [stream for item in data.get("audio_streams") or [] if isinstance(item, dict) for stream in [_audio(item)] if stream is not None]
    value["subtitles"] = [track for item in data.get("subtitles") or [] if isinstance(item, dict) for track in [_subtitle(item)] if track is not None]
    return MediaAnalysis(**value)


def job_from_dict(data: dict) -> DownloadJob:
    source = _mapping(data)
    value = _fields_for(DownloadJob, source)
    value["analysis"] = _analysis(source.get("analysis"))
    settings = JobSettings(**_fields_for(JobSettings, source.get("settings")))
    value["settings"] = settings.normalized()
    progress_source = _mapping(source.get("progress"))
    progress = _fields_for(ProgressState, progress_source)
    progress["stage"] = _enum_value(Stage, progress_source.get("stage") or Stage.IDLE.value, Stage.IDLE)
    try:
        value["progress"] = ProgressState(**progress).normalized()
    except Exception:
        value["progress"] = ProgressState()
    value["status"] = _enum_value(JobStatus, source.get("status") or JobStatus.UNANALYZED.value, JobStatus.UNANALYZED)
    return DownloadJob(**value)

