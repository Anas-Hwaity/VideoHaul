from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import re
import threading
import time
from typing import Any

from .audio import auto_audio_stream, sort_audio_streams
from .formats import combined_filesize
from .inputs import extract_urls
from .models import JobSettings, JobStatus, MediaAnalysis, ProgressState, Stage, SubtitleTrack, VideoStream
from .quality import AUDIO_ONLY_SELECTOR, BEST_SELECTOR, best_stream, recommended_stream, select_device_preset
from .resolvers.common import AuthenticationRequired, MediaUnavailable, ResolverDependencyError
from .subtitles import sort_subtitles


RANGE_TOKEN = re.compile(r"^(\d+)(?:-(\d+)?)?$")
BATCH_ACTIONS = {"skip", "best", "recommended", "skip_subtitles", "auto_audio"}


@dataclass(slots=True)
class BatchItem:
    item_id: str
    index: int
    source_url: str
    source_group: str = ""
    playlist_index: int | None = None
    playlist_count: int | None = None
    playlist_title: str = ""
    title: str = ""
    selected: bool = True
    analysis: MediaAnalysis | None = None
    status: str = "pending"
    selected_video: str | None = None
    selected_audio: list[str] = field(default_factory=list)
    selected_subtitles: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    action: str = ""
    size_bytes: int | None = None
    size_is_estimate: bool = False
    duration_seconds: float | None = None
    filename_preview: str = ""


@dataclass(slots=True)
class BatchDraft:
    source_text: str = ""
    range_spec: str = ""
    requested_quality: str = "best"
    requested_audio_languages: list[str] = field(default_factory=list)
    requested_subtitle_languages: list[str] = field(default_factory=list)
    shared_settings: dict[str, Any] = field(default_factory=dict)
    filename_template: str = "%(playlist_index)03d - %(title)s.%(ext)s"
    items: list[BatchItem] = field(default_factory=list)
    created_job_ids: list[str] = field(default_factory=list)


class BatchService:
    def __init__(self, state, resolvers=None):
        self.state = state
        self.resolvers = resolvers
        self._lock = threading.RLock()
        self.draft = BatchDraft()
        self._generation = 0
        self._activity = self._idle_activity()
        self._cancel_event = threading.Event()

    @staticmethod
    def _idle_activity() -> dict:
        return {
            "state": "idle",
            "stage": "idle",
            "message": "No batch analysis is running",
            "started_at": None,
            "finished_at": None,
            "sources_total": 0,
            "sources_completed": 0,
            "items_total": 0,
            "items_completed": 0,
            "current_source": "",
            "current_item": "",
            "error": "",
        }

    def _set_activity(self, generation: int, **values) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._activity.update(values)
            return True

    def _activity_snapshot(self) -> dict:
        with self._lock:
            value = dict(self._activity)
        started = value.get("started_at")
        finished = value.get("finished_at")
        if started:
            value["elapsed_seconds"] = max(0.0, float((finished or time.time()) - started))
        else:
            value["elapsed_seconds"] = 0.0
        completed = int(value.get("items_completed") or 0)
        total = int(value.get("items_total") or 0)
        elapsed = float(value.get("elapsed_seconds") or 0.0)
        if value.get("state") == "analyzing" and completed > 0 and total > completed:
            value["eta_seconds"] = max(0.0, elapsed / completed * (total - completed))
        else:
            value["eta_seconds"] = 0.0 if value.get("state") == "completed" else None
        return value

    def reset(self) -> dict:
        with self._lock:
            self._cancel_event.set()
            self._cancel_event = threading.Event()
            self._generation += 1
            self.draft = BatchDraft()
            self._activity = self._idle_activity()
        self.state.record_diagnostic("batch_reset", "Batch draft reset")
        return self.snapshot()

    @staticmethod
    def _set_analysis_failure(item: BatchItem, error: Exception) -> None:
        if isinstance(error, AuthenticationRequired):
            status, message = "auth_required", "Authentication required"
        elif isinstance(error, MediaUnavailable):
            status, message = "unavailable", str(error).strip() or "Media unavailable"
        elif isinstance(error, ResolverDependencyError):
            status, message = "dependency_repair", str(error).strip() or "A media dependency needs repair"
        else:
            status, message = "unresolved", str(error).strip() or "Could not resolve media"
        item.status = status
        item.issues = [{"kind": "authentication_required" if status == "auth_required" else status, "message": message}]

    def analyze(
        self,
        text: str,
        range_spec: str = "",
        requested_quality: str = "best",
        requested_audio_languages: list[str] | None = None,
        requested_subtitle_languages: list[str] | None = None,
        shared_settings: dict | None = None,
        filename_template: str | None = None,
    ) -> dict:
        if self.resolvers is None:
            raise RuntimeError("Batch analysis resolver is unavailable")
        urls = extract_urls(text)
        if not urls:
            raise ValueError("Add at least one valid http:// or https:// media or playlist URL")
        with self._lock:
            if self._activity.get("state") == "analyzing":
                raise RuntimeError("Batch analysis is already running. Wait for it to finish or press Reset before starting another analysis.")
            self._cancel_event.set()
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event
            self._generation += 1
            generation = self._generation
            started = time.time()
            self._activity = {
                **self._idle_activity(),
                "state": "analyzing",
                "stage": "discovering_sources",
                "message": "Starting batch analysis",
                "started_at": started,
                "sources_total": len(urls),
            }
        radio_sources = sum(1 for url in urls if _youtube_radio_mix(url))
        self.state.record_diagnostic("batch_analysis_started", "Batch analysis started", {"sources": len(urls), "requested_quality": requested_quality, "range": range_spec, "dynamic_radio_sources": radio_sources})
        draft = BatchDraft(
            source_text=str(text or ""),
            range_spec=str(range_spec or "").strip(),
            requested_quality=str(requested_quality or "best").strip(),
            requested_audio_languages=_unique_strings(requested_audio_languages or []),
            requested_subtitle_languages=_unique_strings(requested_subtitle_languages or []),
            shared_settings=dict(shared_settings or {}),
            filename_template=str(filename_template or "%(playlist_index)03d - %(title)s.%(ext)s"),
        )
        try:
            def discovery_progress(source_index: int, source_total: int, url: str, completed: bool = False, discovered_items: int = 0) -> None:
                radio = _youtube_radio_mix(url)
                message = f"Discovering source {source_index} of {source_total}"
                if radio:
                    message += " - YouTube radio mix detected; analyzing up to the first 10 generated items"
                self._set_activity(
                    generation,
                    stage="discovering_sources",
                    message=message,
                    sources_total=source_total,
                    sources_completed=source_index if completed else max(0, source_index - 1),
                    current_source=url,
                    items_total=max(int(self._activity_snapshot().get("items_total") or 0), int(discovered_items or 0)),
                )

            discovered = self._discover(urls, discovery_progress, str(draft.shared_settings.get("browser_cookies") or "none"), cancel_event)
            if generation != self._generation:
                return self.snapshot()
            selected_indexes = parse_range(draft.range_spec, len(discovered))
            selected_total = len(selected_indexes)
            self._set_activity(
                generation,
                stage="analyzing_items",
                message=f"Discovered {len(discovered)} item(s); analyzing {selected_total} selected item(s)",
                items_total=selected_total,
                items_completed=0,
                current_source="",
            )
            completed_selected = 0
            for position, item in enumerate(discovered, start=1):
                if generation != self._generation:
                    return self.snapshot()
                item.index = position
                item.item_id = f"item-{position}"
                item.selected = position in selected_indexes
                if not item.selected:
                    item.status = "skipped"
                    item.filename_preview = self._preview(item, draft)
                    continue
                label = item.title or item.source_url or f"Item {position}"
                self._set_activity(
                    generation,
                    stage="analyzing_items",
                    message=f"Analyzing selected item {completed_selected + 1} of {selected_total}",
                    current_item=label,
                    current_source=item.source_url,
                    items_total=selected_total,
                    items_completed=completed_selected,
                )
                if item.analysis is None and item.source_url:
                    try:
                        item.analysis = self.resolvers.analyze(item.source_url, str(draft.shared_settings.get("browser_cookies") or "none"), cancel_event=cancel_event)
                    except Exception as error:
                        self._set_analysis_failure(item, error)
                if item.analysis is not None:
                    self._prepare_item(item, draft)
                item.filename_preview = self._preview(item, draft)
                completed_selected += 1
                self._set_activity(
                    generation,
                    items_completed=completed_selected,
                    current_item=label,
                    message=f"Analyzed {completed_selected} of {selected_total} selected item(s)",
                )
            draft.items = discovered
            with self._lock:
                if generation != self._generation:
                    return self.snapshot()
                self.draft = draft
            snapshot = self.snapshot()
            completed_message = f"Batch analysis complete: {snapshot['totals']['ready']} ready, {snapshot['totals']['exceptions']} need attention"
            if radio_sources:
                completed_message += f". {radio_sources} YouTube radio/mix source(s) were limited to the first 10 generated items"
            self._set_activity(
                generation,
                state="completed",
                stage="completed",
                message=completed_message,
                finished_at=time.time(),
                sources_completed=len(urls),
                items_completed=selected_total,
                current_source="",
                current_item="",
            )
            snapshot = self.snapshot()
            self.state.record_diagnostic("batch_analysis_completed", "Batch analysis completed", {"items": snapshot["totals"]["items"], "selected": snapshot["totals"]["selected"], "ready": snapshot["totals"]["ready"], "exceptions": snapshot["totals"]["exceptions"], "elapsed_seconds": snapshot["activity"]["elapsed_seconds"]})
            return snapshot
        except Exception as exc:
            self._set_activity(
                generation,
                state="failed",
                stage="failed",
                message=str(exc) or "Batch analysis failed",
                error=str(exc) or "Batch analysis failed",
                finished_at=time.time(),
            )
            self.state.record_diagnostic("batch_analysis_failed", str(exc) or "Batch analysis failed", {"sources": len(urls)}, "error")
            raise

    def update_selection(self, indexes: list[int] | None = None, range_spec: str | None = None, mode: str = "set") -> dict:
        with self._lock:
            total = len(self.draft.items)
            if range_spec is not None:
                chosen = parse_range(range_spec, total)
                self.draft.range_spec = str(range_spec or "").strip()
            else:
                chosen = {int(value) for value in indexes or [] if 1 <= int(value) <= total}
            if mode == "all":
                chosen = set(range(1, total + 1))
            elif mode == "none":
                chosen = set()
            elif mode == "invert":
                chosen = {item.index for item in self.draft.items if not item.selected}
            elif mode not in {"set", "all", "none", "invert"}:
                raise ValueError("Unsupported selection mode")
            generation = self._generation
            cancel_event = self._cancel_event
            cookies = str(self.draft.shared_settings.get("browser_cookies") or "none")
            needs_analysis = []
            for item in self.draft.items:
                was_selected = item.selected
                item.selected = item.index in chosen
                if not item.selected:
                    item.status = "skipped"
                elif not was_selected or item.status == "skipped":
                    if item.analysis is None and item.source_url and self.resolvers is not None:
                        item.status = "pending"
                        item.issues = []
                        needs_analysis.append((item.item_id, item.source_url))
                    elif item.analysis is not None:
                        self._prepare_item(item, self.draft)
                item.filename_preview = self._preview(item, self.draft)
        for item_id, source_url in needs_analysis:
            analysis = None
            failure = None
            try:
                analysis = self.resolvers.analyze(source_url, cookies, cancel_event=cancel_event)
            except Exception as error:
                failure = error
            with self._lock:
                if generation != self._generation:
                    return self.snapshot()
                try:
                    item = self._item(item_id)
                except KeyError:
                    continue
                if not item.selected:
                    continue
                if failure is not None:
                    self._set_analysis_failure(item, failure)
                else:
                    item.analysis = analysis
                    if item.analysis is not None:
                        self._prepare_item(item, self.draft)
                item.filename_preview = self._preview(item, self.draft)
        snapshot = self.snapshot()
        self.state.record_diagnostic("batch_selection_updated", "Batch selection updated", {"mode": mode, "selected": snapshot["totals"]["selected"], "range": self.draft.range_spec})
        return snapshot

    def reorder(self, item_id: str, index: int) -> dict:
        with self._lock:
            item = self._item(item_id)
            self.draft.items.remove(item)
            target = max(0, min(int(index), len(self.draft.items)))
            self.draft.items.insert(target, item)
            for position, current in enumerate(self.draft.items, start=1):
                current.index = position
                current.filename_preview = self._preview(current, self.draft)
            snapshot = self.snapshot()
            self.state.record_diagnostic("batch_reordered", "Batch item reordered", {"item_id": item.item_id, "index": target})
            return snapshot

    def configure(self, values: dict) -> dict:
        with self._lock:
            payload = dict(values or {})
            if "requested_quality" in payload:
                self.draft.requested_quality = str(payload["requested_quality"] or "best").strip()
            if "requested_audio_languages" in payload:
                self.draft.requested_audio_languages = _unique_strings(payload["requested_audio_languages"] or [])
            if "requested_subtitle_languages" in payload:
                self.draft.requested_subtitle_languages = _unique_strings(payload["requested_subtitle_languages"] or [])
            if "shared_settings" in payload:
                self.draft.shared_settings = dict(payload["shared_settings"] or {})
            if "filename_template" in payload:
                self.draft.filename_template = str(payload["filename_template"] or "%(playlist_index)03d - %(title)s.%(ext)s")
            for item in self.draft.items:
                if item.selected and item.analysis is not None:
                    self._prepare_item(item, self.draft)
                item.filename_preview = self._preview(item, self.draft)
            snapshot = self.snapshot()
            self.state.record_diagnostic("batch_shared_settings_updated", "Batch shared settings updated", {"fields": sorted(payload), "exceptions": snapshot["totals"]["exceptions"]})
            return snapshot

    def resolve_item(self, item_id: str, action: str, selected_video: str | None = None, selected_audio: list[str] | None = None, selected_subtitles: list[str] | None = None) -> dict:
        with self._lock:
            item = self._item(item_id)
            normalized = str(action or "").strip().casefold()
            if normalized not in BATCH_ACTIONS and normalized != "choose":
                raise ValueError("Unsupported exception action")
            if normalized == "skip":
                item.action = "skip"
                item.selected = False
                item.status = "skipped"
                item.issues = []
            elif item.analysis is None:
                raise ValueError("This item cannot be resolved without media analysis")
            else:
                item.action = normalized
                if normalized == "best":
                    if not item.analysis.video_streams:
                        raise ValueError("No video stream is available")
                    item.selected_video = BEST_SELECTOR
                elif normalized == "recommended":
                    selected = recommended_stream(item.analysis.video_streams)
                    if not selected:
                        raise ValueError("No recommended stream is available")
                    item.selected_video = selected.stream_id
                elif normalized == "skip_subtitles":
                    item.selected_subtitles = []
                elif normalized == "auto_audio":
                    automatic = auto_audio_stream(item.analysis.audio_streams)
                    item.selected_audio = [automatic.stream_id] if automatic else []
                elif normalized == "choose":
                    if selected_video is not None:
                        if selected_video == AUDIO_ONLY_SELECTOR:
                            if not item.analysis.audio_streams:
                                raise ValueError("Audio-only is unavailable")
                            item.selected_video = selected_video
                        elif not any(stream.stream_id == selected_video for stream in item.analysis.video_streams):
                            raise ValueError("Selected stream is unavailable")
                        else:
                            item.selected_video = selected_video
                    if selected_audio is not None:
                        available_audio = {track.stream_id for track in item.analysis.audio_streams}
                        missing_audio = [track for track in selected_audio if track not in available_audio]
                        if missing_audio:
                            raise ValueError("Selected audio track is unavailable")
                        item.selected_audio = list(dict.fromkeys(selected_audio))
                    if selected_subtitles is not None:
                        available = {track.track_id for track in item.analysis.subtitles}
                        missing = [track for track in selected_subtitles if track not in available]
                        if missing:
                            raise ValueError("Selected subtitle track is unavailable")
                        item.selected_subtitles = list(dict.fromkeys(selected_subtitles))
                self._recheck_after_action(item)
            item.filename_preview = self._preview(item, self.draft)
            snapshot = self.snapshot()
            self.state.record_diagnostic("batch_exception_resolved", "Batch item exception action applied", {"item_id": item.item_id, "action": normalized, "status": item.status})
            return snapshot

    def commit(self) -> dict:
        with self._lock:
            blockers = [item for item in self.draft.items if item.selected and item.status != "ready"]
            if blockers:
                raise ValueError(f"Resolve {len(blockers)} batch item exception(s) before adding jobs")
            created = []
            for item in self.draft.items:
                if not item.selected or item.status != "ready" or item.analysis is None:
                    continue
                settings = self._settings_for_item(item)
                job = self.state.add_job(item.source_url, settings)
                analysis = deepcopy(item.analysis)
                analysis.playlist = dict(analysis.playlist or {})
                analysis.playlist.update({
                    "index": item.playlist_index or item.index,
                    "count": item.playlist_count or len(self.draft.items),
                    "title": item.playlist_title or item.source_group,
                    "batch_index": item.index,
                })
                job.analysis = analysis
                job.selected_video = item.selected_video
                job.status = JobStatus.READY
                job.progress = ProgressState(stage=Stage.IDLE, message="Ready")
                self.state.update_job(
                    job.job_id,
                    analysis=job.analysis,
                    selected_video=job.selected_video,
                    status=job.status,
                    progress=job.progress,
                    settings=job.settings,
                )
                created.append(job.job_id)
            self.draft.created_job_ids = created
            self.state.record_diagnostic("batch_committed", "Batch jobs added to queue", {"created": len(created), "job_ids": created})
            return {"created_job_ids": created, "created": len(created), "batch": self.snapshot()}

    def snapshot(self) -> dict:
        with self._lock:
            items = [self._item_dict(item) for item in self.draft.items]
            selected = [item for item in self.draft.items if item.selected]
            ready = [item for item in selected if item.status == "ready"]
            exceptions = [item for item in selected if item.status not in {"ready", "skipped"}]
            known_durations = [item.duration_seconds for item in selected if item.duration_seconds is not None]
            known_sizes = [item.size_bytes for item in selected if item.size_bytes is not None]
            total_duration = sum(known_durations) if len(known_durations) == len(selected) and selected else None
            total_size = sum(known_sizes) if len(known_sizes) == len(selected) and selected else None
            estimated = any(item.size_is_estimate for item in selected if item.size_bytes is not None)
            return {
                "source_text": self.draft.source_text,
                "range_spec": self.draft.range_spec,
                "requested_quality": self.draft.requested_quality,
                "requested_audio_languages": list(self.draft.requested_audio_languages),
                "requested_subtitle_languages": list(self.draft.requested_subtitle_languages),
                "shared_settings": dict(self.draft.shared_settings),
                "filename_template": self.draft.filename_template,
                "items": items,
                "created_job_ids": list(self.draft.created_job_ids),
                "activity": self._activity_snapshot(),
                "totals": {
                    "items": len(self.draft.items),
                    "selected": len(selected),
                    "ready": len(ready),
                    "exceptions": len(exceptions),
                    "duration_seconds": total_duration,
                    "duration_known_count": len(known_durations),
                    "size_bytes": total_size,
                    "size_known_count": len(known_sizes),
                    "size_is_estimate": estimated,
                },
            }

    def _discover(self, urls: list[str], progress=None, browser_cookies: str = "none", cancel_event=None) -> list[BatchItem]:
        values: list[BatchItem] = []
        total = len(urls)
        for source_number, url in enumerate(urls, start=1):
            group = f"Source {source_number}"
            if progress is not None:
                progress(source_number, total, url, False, len(values))
            try:
                analysis = self.resolvers.analyze(url, browser_cookies, cancel_event=cancel_event)
            except Exception as error:
                item = BatchItem("", 0, url, group, title=url)
                self._set_analysis_failure(item, error)
                values.append(item)
                if progress is not None:
                    progress(source_number, total, url, True, len(values))
                continue
            playlist = analysis.playlist or {}
            entries = playlist.get("entries") if isinstance(playlist, dict) else None
            if entries:
                count = len(entries)
                playlist_title = str(playlist.get("title") or analysis.title or "Playlist")
                for entry_position, entry in enumerate(entries, start=1):
                    entry = dict(entry or {})
                    entry_url = str(entry.get("url") or "").strip()
                    values.append(BatchItem(
                        "",
                        0,
                        entry_url,
                        playlist_title,
                        playlist_index=int(entry.get("index") or entry_position),
                        playlist_count=count,
                        playlist_title=playlist_title,
                        title=str(entry.get("title") or "Untitled media"),
                        duration_seconds=_float_or_none(entry.get("duration_seconds")),
                    ))
            else:
                values.append(BatchItem("", 0, url, group, title=analysis.title, analysis=analysis, duration_seconds=analysis.duration_seconds))
            if progress is not None:
                progress(source_number, total, url, True, len(values))
        return values

    def _prepare_item(self, item: BatchItem, draft: BatchDraft) -> None:
        analysis = item.analysis
        if analysis is None:
            return
        analysis.audio_streams = sort_audio_streams(analysis.audio_streams)
        analysis.subtitles = sort_subtitles(analysis.subtitles)
        item.title = analysis.title or item.title
        item.duration_seconds = analysis.duration_seconds
        item.issues = []
        item.action = ""
        selected, quality_issue = self._quality_selection(analysis, draft.requested_quality)
        item.selected_video = selected
        if quality_issue:
            item.issues.append(quality_issue)
        audio_ids, audio_issue = self._audio_selection(analysis.audio_streams, draft.requested_audio_languages)
        item.selected_audio = audio_ids
        if audio_issue:
            item.issues.append(audio_issue)
        subtitle_ids, subtitle_issue = self._subtitle_selection(analysis.subtitles, draft.requested_subtitle_languages)
        item.selected_subtitles = subtitle_ids
        if subtitle_issue:
            item.issues.append(subtitle_issue)
        item.status = "exception" if item.issues else "ready"
        self._update_size(item)

    def _quality_selection(self, analysis: MediaAnalysis, requested: str) -> tuple[str | None, dict | None]:
        value = str(requested or "best").strip().casefold()
        streams = list(analysis.video_streams)
        if value in {"best", "best_available", ""}:
            if streams:
                return BEST_SELECTOR, None
            if analysis.audio_streams:
                return AUDIO_ONLY_SELECTOR, None
            return None, {"kind": "quality_unavailable", "message": "No downloadable media stream is available", "requested": requested}
        if value == "recommended":
            selected = recommended_stream(streams)
            return (selected.stream_id, None) if selected else (None, {"kind": "quality_unavailable", "message": "Recommended video is unavailable", "requested": requested})
        if value == "audio_only":
            return (AUDIO_ONLY_SELECTOR, None) if analysis.audio_streams else (None, {"kind": "quality_unavailable", "message": "Audio-only is unavailable", "requested": requested})
        if value.startswith("preset:"):
            selected = select_device_preset(streams, value.split(":", 1)[1])
            return (selected.stream_id, None) if selected else (None, {"kind": "quality_unavailable", "message": f"Preset {requested} is unavailable", "requested": requested})
        family = value.upper().replace("P", "p") if value[:-1].isdigit() and value.endswith("p") else value
        candidates = [stream for stream in streams if str(stream.resolution_family).casefold() == family.casefold()]
        if candidates:
            selected = best_stream(candidates)
            return selected.stream_id if selected else None, None
        explicit = next((stream for stream in streams if stream.stream_id == requested), None)
        if explicit:
            return explicit.stream_id, None
        available = sorted({stream.resolution_family for stream in streams if stream.resolution_family})
        return None, {"kind": "quality_unavailable", "message": f"Requested quality {requested} is unavailable", "requested": requested, "available": available}


    def _audio_selection(self, tracks, requested: list[str]) -> tuple[list[str], dict | None]:
        ordered = sort_audio_streams(tracks)
        if not requested:
            automatic = auto_audio_stream(ordered)
            return ([automatic.stream_id] if automatic else []), None
        selected = []
        missing = []
        for language in requested:
            key = str(language or "").strip().casefold()
            track = next((item for item in ordered if key in {str(item.language or "").casefold(), str(item.label or "").casefold()}), None)
            if track:
                if track.stream_id not in selected:
                    selected.append(track.stream_id)
            else:
                missing.append(language)
        if missing:
            return selected, {"kind": "audio_unavailable", "message": f"Requested audio unavailable: {', '.join(missing)}", "missing": missing}
        return selected, None

    def _subtitle_selection(self, tracks: list[SubtitleTrack], requested: list[str]) -> tuple[list[str], dict | None]:
        if not requested:
            return [], None
        ordered = sort_subtitles(tracks)
        selected = []
        missing = []
        for language in requested:
            key = str(language or "").strip().casefold()
            track = next((item for item in ordered if key in {str(item.language_code).casefold(), str(item.language_name).casefold(), str(item.label).casefold()}), None)
            if track:
                if track.track_id not in selected:
                    selected.append(track.track_id)
            else:
                missing.append(language)
        if missing:
            return selected, {"kind": "subtitle_unavailable", "message": f"Requested subtitles unavailable: {', '.join(missing)}", "missing": missing}
        return selected, None

    def _recheck_after_action(self, item: BatchItem) -> None:
        remaining = []
        for issue in item.issues:
            kind = issue.get("kind")
            if kind == "quality_unavailable" and item.selected_video:
                continue
            if kind == "audio_unavailable" and (item.action == "auto_audio" or bool(item.selected_audio)):
                continue
            if kind == "subtitle_unavailable" and (item.action == "skip_subtitles" or bool(item.selected_subtitles)):
                continue
            remaining.append(issue)
        item.issues = remaining
        item.status = "exception" if remaining else "ready"
        self._update_size(item)

    def _update_size(self, item: BatchItem) -> None:
        item.size_bytes = None
        item.size_is_estimate = False
        if item.analysis is None:
            return
        selected_audio = [track for track in item.analysis.audio_streams if track.stream_id in set(item.selected_audio)]
        if item.selected_video == AUDIO_ONLY_SELECTOR:
            tracks = selected_audio or ([auto_audio_stream(item.analysis.audio_streams)] if auto_audio_stream(item.analysis.audio_streams) else [])
            if tracks and all(track.filesize_bytes is not None for track in tracks):
                item.size_bytes = sum(int(track.filesize_bytes or 0) for track in tracks)
                item.size_is_estimate = any(bool(track.filesize_is_estimate) for track in tracks)
            return
        if item.selected_video == BEST_SELECTOR:
            selected = best_stream(item.analysis.video_streams)
        else:
            selected = next((stream for stream in item.analysis.video_streams if stream.stream_id == item.selected_video), None)
        if selected is None or selected.filesize_bytes is None:
            return
        size = int(selected.filesize_bytes)
        estimated = bool(selected.filesize_is_estimate)
        if not selected.has_audio:
            tracks = selected_audio
            if not tracks and selected.audio_reference:
                reference = next((track for track in item.analysis.audio_streams if track.stream_id == selected.audio_reference), None)
                tracks = [reference] if reference else []
            if not tracks:
                automatic = auto_audio_stream(item.analysis.audio_streams)
                tracks = [automatic] if automatic else []
            if tracks:
                if any(track.filesize_bytes is None for track in tracks):
                    item.size_bytes = size
                    item.size_is_estimate = True
                    return
                size += sum(int(track.filesize_bytes or 0) for track in tracks)
                estimated = estimated or any(bool(track.filesize_is_estimate) for track in tracks)
        item.size_bytes = size
        item.size_is_estimate = estimated

    def _settings_for_item(self, item: BatchItem) -> JobSettings:
        base = self.state.default_job_settings()
        values = {name: getattr(base, name) for name in JobSettings.__dataclass_fields__}
        unknown = set(self.draft.shared_settings) - set(values)
        if unknown:
            raise ValueError(f"Unknown shared settings: {', '.join(sorted(unknown))}")
        values.update(self.draft.shared_settings)
        values["filename_template"] = self.draft.filename_template
        values["audio_selection_mode"] = "manual" if item.selected_audio else "auto"
        values["selected_audio"] = list(item.selected_audio)
        values["subtitles_enabled"] = bool(item.selected_subtitles)
        values["selected_subtitles"] = list(item.selected_subtitles)
        return JobSettings(**values).normalized()

    def _preview(self, item: BatchItem, draft: BatchDraft) -> str:
        analysis = item.analysis
        values = {
            "playlist_index": item.playlist_index or item.index,
            "batch_index": item.index,
            "title": _safe_preview(analysis.title if analysis else item.title or "media"),
            "creator": _safe_preview(analysis.creator if analysis else "unknown"),
            "date": _safe_preview(analysis.upload_date if analysis else "unknown-date"),
            "resolution": _safe_preview(self._resolution(item)),
            "quality": _safe_preview(self._quality_preview(item, draft)),
            "ext": _safe_preview(self._extension(item)),
        }
        rendered = str(draft.filename_template or "%(playlist_index)03d - %(title)s.%(ext)s")
        for key, value in values.items():
            rendered = rendered.replace(f"%({key})s", str(value))
            rendered = rendered.replace(f"%({key})03d", f"{int(value):03d}" if isinstance(value, int) or str(value).isdigit() else str(value))
        return rendered

    def _quality_preview(self, item: BatchItem, draft: BatchDraft) -> str:
        if item.selected_video == AUDIO_ONLY_SELECTOR:
            return "audio-only"
        if item.analysis is not None and item.selected_video not in {None, BEST_SELECTOR}:
            selected = next((stream for stream in item.analysis.video_streams if stream.stream_id == item.selected_video), None)
            if selected is not None:
                return selected.resolution_family or selected.stream_id
        if item.selected_video == BEST_SELECTOR:
            return "best"
        return str(draft.requested_quality or "best")

    def _resolution(self, item: BatchItem) -> str:
        if item.analysis is None or item.selected_video == AUDIO_ONLY_SELECTOR:
            return "audio"
        selected = best_stream(item.analysis.video_streams) if item.selected_video == BEST_SELECTOR else next((stream for stream in item.analysis.video_streams if stream.stream_id == item.selected_video), None)
        return selected.resolution_family if selected else "unknown"

    def _extension(self, item: BatchItem) -> str:
        if item.analysis is None:
            return "media"
        if item.selected_video == AUDIO_ONLY_SELECTOR:
            output = str(self.draft.shared_settings.get("audio_output_format") or "source")
            if output not in {"", "source"}:
                return output
            audio = auto_audio_stream(item.analysis.audio_streams)
            return str(audio.container or "media") if audio else "media"
        selected = best_stream(item.analysis.video_streams) if item.selected_video == BEST_SELECTOR else next((stream for stream in item.analysis.video_streams if stream.stream_id == item.selected_video), None)
        configured = str(self.draft.shared_settings.get("container") or "auto")
        if configured not in {"", "auto", "source"}:
            return configured
        return str(selected.container or "media") if selected else "media"

    def _item(self, item_id: str) -> BatchItem:
        for item in self.draft.items:
            if item.item_id == item_id:
                return item
        raise KeyError(item_id)

    def _item_dict(self, item: BatchItem) -> dict:
        data = asdict(item)
        if item.analysis is not None:
            analysis = data["analysis"]
            analysis["video_streams"] = [asdict(stream) for stream in item.analysis.video_streams]
            analysis["audio_streams"] = [asdict(stream) for stream in item.analysis.audio_streams]
            analysis["subtitles"] = [asdict(track) for track in item.analysis.subtitles]
        return data


def parse_range(spec: str, total: int) -> set[int]:
    total = max(0, int(total))
    value = str(spec or "").strip()
    if not value:
        return set(range(1, total + 1))
    selected: set[int] = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        match = RANGE_TOKEN.fullmatch(token)
        if not match:
            raise ValueError(f"Invalid range token: {token}")
        start = int(match.group(1))
        end_text = match.group(2)
        if "-" in token and end_text is None:
            end = total
        elif end_text is not None:
            end = int(end_text)
        else:
            end = start
        if start < 1 or end < start:
            raise ValueError(f"Invalid range token: {token}")
        for index in range(start, min(end, total) + 1):
            selected.add(index)
    return selected



def _youtube_radio_mix(url: str) -> bool:
    value = str(url or "").casefold()
    return ("youtube.com/" in value or "youtu.be/" in value) and "list=rd" in value

def _unique_strings(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _safe_preview(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip(" .") or "media"


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except Exception:
        return None
