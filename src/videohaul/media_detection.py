from __future__ import annotations

import queue
import re
import threading
import time
from itertools import count
from typing import Any
from urllib.parse import urljoin

from .browser import ensure_browser
from .media_candidates import (
    CandidateCollection,
    build_candidate,
    candidate_request_headers,
    classify_media,
    is_media_reference,
    kind_for_sniffed,
    looks_like_segment,
    looks_like_observation_noise,
    normalized_mime,
    rank_candidates,
    redact_url,
    should_sniff,
    sniff_media_body,
)
from .models import DetectedMediaCandidate, DetectorSessionState
from .resolvers.common import ResolverDependencyError

DEFAULT_AUTOMATIC_BUDGET_SECONDS = 25.0
DEFAULT_INTERACTIVE_BUDGET_SECONDS = 900.0
DEFAULT_MAX_FRAME_DEPTH = 5
DEFAULT_MAX_FRAMES = 40
DEFAULT_MAX_CANDIDATES = 120
DEFAULT_MAX_NETWORK_EVENTS = 4000
DEFAULT_PROBE_WORKERS = 3
DEFAULT_MAX_INSTRUMENTED_BODY = 512 * 1024
DOM_POLL_SECONDS = 0.75

NOISE_RESOURCE_TYPES = {"image", "stylesheet", "font", "script", "websocket", "manifest", "texttrack", "eventsource", "ping", "preflight", "cspviolationreport"}
MEDIA_RESOURCE_TYPES = {"media", "xhr", "fetch", "document", "other"}

URL_IN_BODY = re.compile(r"https?://[^\s\"'<>\\\\)\]}]+", re.IGNORECASE)
RELATIVE_MEDIA_IN_BODY = re.compile(r"[\"'](/[^\s\"'<>]*?\.(?:m3u8|mpd|mp4|m4v|webm|m4a|mp3|mov|mkv)(?:\?[^\s\"'<>]*)?)[\"']", re.IGNORECASE)

BINDING_NAME = "__videohaulObserve"

DOM_PROBE = """
() => {
  const out = [];
  const push = (url, kind, extra) => {
    if (!url) return;
    out.push(Object.assign({url: String(url), kind: kind}, extra || {}));
  };
  const elements = Array.from(document.querySelectorAll('video, audio'));
  for (const element of elements) {
    const isVideo = element.tagName.toLowerCase() === 'video';
    const extra = {
      poster: isVideo ? (element.getAttribute('poster') || '') : '',
      duration: Number.isFinite(element.duration) ? element.duration : null,
      width: isVideo ? (element.videoWidth || null) : null,
      height: isVideo ? (element.videoHeight || null) : null,
      playing: !element.paused,
      encrypted: !!(element.mediaKeys)
    };
    push(element.currentSrc, isVideo ? 'video_currentsrc' : 'audio_currentsrc', extra);
    push(element.getAttribute('src'), isVideo ? 'video_src' : 'audio_src', extra);
    for (const source of Array.from(element.querySelectorAll('source'))) {
      push(source.getAttribute('src'), 'source_element', Object.assign({}, extra, {type: source.getAttribute('type') || ''}));
    }
  }
  for (const meta of Array.from(document.querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"], meta[itemprop="contentURL"]'))) {
    push(meta.getAttribute('content'), 'page_metadata', {});
  }
  for (const link of Array.from(document.querySelectorAll('link[as="video"], link[as="audio"]'))) {
    push(link.getAttribute('href'), 'page_metadata', {});
  }
  return {
    title: document.title || '',
    poster: (document.querySelector('meta[property="og:image"]') || {}).content || '',
    duration: (() => {
      const meta = document.querySelector('meta[property="video:duration"], meta[itemprop="duration"]');
      if (!meta) return null;
      const raw = Number(meta.getAttribute('content'));
      return Number.isFinite(raw) && raw > 0 ? raw : null;
    })(),
    items: out
  };
}
"""

INSTRUMENTATION = """
(() => {
  const limit = __VIDEOHAUL_BODY_LIMIT__;
  const readable = value => {
    const text = String(value || '').toLowerCase();
    return !text || text.indexOf('json') >= 0 || text.indexOf('text') >= 0 || text.indexOf('xml') >= 0
      || text.indexOf('mpegurl') >= 0 || text.indexOf('dash') >= 0 || text.indexOf('javascript') >= 0;
  };
  const send = payload => {
    try {
      const report = window.__VIDEOHAUL_BINDING__;
      if (report) report(payload);
    } catch (error) {}
  };
  try {
    const openMethod = XMLHttpRequest.prototype.open;
    const sendMethod = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__videohaulMethod = method;
      this.__videohaulUrl = url;
      return openMethod.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      this.addEventListener('load', () => {
        try {
          const type = this.getResponseHeader('content-type') || '';
          let body = '';
          if (readable(type) && (this.responseType === '' || this.responseType === 'text')) {
            body = String(this.responseText || '').slice(0, limit);
          }
          send({
            channel: 'xhr',
            method: this.__videohaulMethod || 'GET',
            url: String(this.__videohaulUrl || ''),
            finalUrl: String(this.responseURL || ''),
            status: this.status || 0,
            contentType: type,
            contentLength: this.getResponseHeader('content-length') || '',
            contentDisposition: this.getResponseHeader('content-disposition') || '',
            pageUrl: String(location.href || ''),
            body: body
          });
        } catch (error) {}
      });
      return sendMethod.apply(this, arguments);
    };
  } catch (error) {}
  try {
    const originalFetch = window.fetch;
    if (originalFetch) {
      window.fetch = function (...args) {
        const result = originalFetch.apply(this, args);
        try {
          result.then(response => {
            try {
              const type = response.headers.get('content-type') || '';
              const base = {
                channel: 'fetch',
                method: (args[1] && args[1].method) || 'GET',
                url: String((args[0] && args[0].url) || args[0] || ''),
                finalUrl: String(response.url || ''),
                status: response.status || 0,
                contentType: type,
                contentLength: response.headers.get('content-length') || '',
                contentDisposition: response.headers.get('content-disposition') || '',
                pageUrl: String(location.href || '')
              };
              if (readable(type)) {
                response.clone().text().then(text => {
                  base.body = String(text || '').slice(0, limit);
                  send(base);
                }).catch(() => send(Object.assign(base, {body: ''})));
              } else {
                send(Object.assign(base, {body: ''}));
              }
            } catch (error) {}
          }).catch(() => {});
        } catch (error) {}
        return result;
      };
    }
  } catch (error) {}
})();
"""


class DetectorUnavailable(ResolverDependencyError):
    pass


def playwright_available() -> bool:
    try:
        import playwright.sync_api
    except Exception:
        return False
    return True


_LISTENER_METHODS = {"request": "_on_request", "response": "_on_response", "requestfailed": "_on_request_failed", "page": "_on_popup"}


class DetectorSession:
    def __init__(
        self,
        page_url: str,
        job_id: str = "",
        headless: bool = True,
        mode: str = "automatic",
        budget_seconds: float | None = None,
        executable: str | None = None,
        observer=None,
        max_frame_depth: int = DEFAULT_MAX_FRAME_DEPTH,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_network_events: int = DEFAULT_MAX_NETWORK_EVENTS,
        probe=None,
        interaction=None,
        probe_workers: int = DEFAULT_PROBE_WORKERS,
        instrument: bool = True,
        connectivity_checker=None,
    ):
        self.state = DetectorSessionState(
            job_id=str(job_id or ""),
            page_url=str(page_url or ""),
            mode=str(mode or "automatic"),
            headless=bool(headless),
            started_at=time.time(),
        )
        self.budget_seconds = float(budget_seconds if budget_seconds is not None else (DEFAULT_INTERACTIVE_BUDGET_SECONDS if mode == "interactive" else DEFAULT_AUTOMATIC_BUDGET_SECONDS))
        self.executable = executable
        self.observer = observer
        self.max_frame_depth = max(0, int(max_frame_depth))
        self.max_frames = max(1, int(max_frames))
        self.max_network_events = max(1, int(max_network_events))
        self.probe = probe
        self.interaction = interaction
        self.probe_workers = max(1, int(probe_workers))
        self.instrument = bool(instrument)
        self.connectivity_checker = connectivity_checker
        self._connectivity_state = True
        self._collection = CandidateCollection(limit=max_candidates)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._probe_sequence = count()
        self._probe_threads: list[threading.Thread] = []
        self._queued_candidates: set[str] = set()
        self._page_title = ""
        self._page_duration: float | None = None
        self._page_poster = ""
        self._user_agent = ""
        self._seen_frames: set[str] = set()
        self._encrypted_seen = False
        self._pages: list = []
        self._event_limit_reported = False

    @property
    def session_id(self) -> str:
        return self.state.session_id

    def _emit(self, kind: str, message: str = "", payload: dict | None = None, level: str = "info") -> None:
        if self.observer is None:
            return
        data = {"session_id": self.session_id, "page_url": redact_url(self.state.page_url), "mode": self.state.mode}
        data.update(payload or {})
        try:
            self.observer(kind, message, data, level)
        except Exception:
            return

    def _set_stage(self, stage: str, message: str = "") -> None:
        with self._lock:
            self.state.stage = str(stage)
            self.state.message = str(message or stage.replace("_", " ").capitalize())
        self._emit("media_detector_stage", self.state.message, {"stage": stage})

    def start(self) -> "DetectorSession":
        if not playwright_available():
            raise DetectorUnavailable("Managed Chromium automation runtime is unavailable")
        try:
            self.executable = self.executable or ensure_browser("playwright")
        except Exception as exc:
            raise DetectorUnavailable(str(exc) or "Managed Chromium automation runtime is unavailable") from exc
        self._start_probe_workers()
        self._thread = threading.Thread(target=self._run, name=f"videohaul-detector-{self.session_id[:8]}", daemon=True)
        self._thread.start()
        return self

    def request_stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.state.active:
                self.state.stage = "closing"
                self.state.message = "Closing the browser session"

    def stop(self, timeout: float = 20.0) -> None:
        self.request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._stop_probe_workers(timeout=min(10.0, timeout))

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout=timeout)

    def snapshot(self) -> dict:
        with self._lock:
            ordered = rank_candidates(self._collection.values(), self._page_duration, self._collection)
            for item in ordered:
                item.segment_activity = self._collection.segment_activity(item)
            self.state.candidates = ordered
            return self.state.public_dict()

    def candidates(self) -> list[DetectedMediaCandidate]:
        with self._lock:
            ordered = rank_candidates(self._collection.values(), self._page_duration, self._collection)
            for item in ordered:
                item.segment_activity = self._collection.segment_activity(item)
            return ordered

    def candidate(self, candidate_id: str) -> DetectedMediaCandidate:
        with self._lock:
            for item in self._collection.values():
                if item.candidate_id == str(candidate_id):
                    return item
        raise KeyError(candidate_id)

    def _start_probe_workers(self) -> None:
        if self.probe is None:
            return
        for index in range(self.probe_workers):
            worker = threading.Thread(target=self._probe_worker, name=f"videohaul-probe-{self.session_id[:6]}-{index}", daemon=True)
            worker.start()
            self._probe_threads.append(worker)

    def _stop_probe_workers(self, timeout: float = 10.0) -> None:
        workers = list(self._probe_threads)
        for _ in workers:
            self._probe_queue.put((10_000, next(self._probe_sequence), None))
        deadline = time.monotonic() + float(timeout)
        for worker in workers:
            remaining = max(0.05, deadline - time.monotonic())
            worker.join(timeout=remaining)
        alive = [worker for worker in workers if worker.is_alive()]
        self._probe_threads = alive
        if alive:
            self._emit("media_detector_probe_workers_alive", "Some Media Detector probe workers are still stopping", {"threads": [worker.name for worker in alive]}, "warning")

    def clear_sensitive_context(self) -> None:
        with self._lock:
            for candidate in self._collection.values():
                candidate.required_headers = {}
                candidate.required_cookie_scope = ""
            for candidate in self.state.candidates:
                candidate.required_headers = {}
                candidate.required_cookie_scope = ""

    def _probe_worker(self) -> None:
        while True:
            try:
                item = self._probe_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set() and self._finished.is_set():
                    return
                continue
            _, _, candidate = item
            if candidate is None:
                self._probe_queue.task_done()
                return
            try:
                while not self._stop.is_set() and not self._online():
                    self._set_detector_connectivity(False)
                    self._stop.wait(0.5)
                if self._stop.is_set():
                    continue
                self._set_detector_connectivity(True)
                self._emit("media_candidate_probing", "Probing candidate", {"candidate_id": candidate.candidate_id, "url": redact_url(candidate.final_url or candidate.discovered_url)})
                self.probe(candidate)
                if candidate.is_manifest and candidate.verification_state in {"verified", "http_verified"}:
                    self._expand_manifest(candidate)
                self._emit(
                    "media_candidate_ready",
                    "Candidate verified",
                    {
                        "candidate_id": candidate.candidate_id,
                        "verification_state": candidate.verification_state,
                        "media_kind": candidate.media_kind,
                        "duration_seconds": candidate.duration_seconds,
                        "variant_count": candidate.variant_count,
                    },
                )
            except Exception as exc:
                candidate.verification_state = "probe_failed"
                candidate.verification_detail = str(exc)
                self._emit("media_candidate_probe_failed", str(exc), {"candidate_id": candidate.candidate_id}, "warning")
            finally:
                self._probe_queue.task_done()

    def _expand_manifest(self, candidate: DetectedMediaCandidate) -> None:
        self._emit("media_manifest_parsing", "Parsing manifest", {"candidate_id": candidate.candidate_id, "media_kind": candidate.media_kind})
        target = candidate.final_url or candidate.discovered_url
        try:
            if candidate.media_kind == "hls":
                from .resolvers.hls import HlsResolver

                analysis = HlsResolver().analyze(target, request_headers=candidate_request_headers(candidate))
            elif candidate.media_kind == "dash":
                from .resolvers.dash import DashResolver

                analysis = DashResolver().analyze(target, request_headers=candidate_request_headers(candidate))
            else:
                return
        except Exception as exc:
            candidate.verification_detail = "; ".join(filter(None, [candidate.verification_detail, f"Manifest parse unavailable: {exc}"]))
            return
        variants = []
        for stream in analysis.video_streams:
            variants.append(
                {
                    "stream_id": stream.stream_id,
                    "width": stream.width,
                    "height": stream.height,
                    "fps": stream.fps,
                    "bitrate": stream.bitrate,
                    "codec": stream.codec,
                    "container": stream.container,
                    "hdr_mode": stream.hdr_mode,
                    "duration_seconds": stream.duration_seconds,
                    "filesize_bytes": stream.filesize_bytes,
                    "filesize_is_estimate": stream.filesize_is_estimate,
                    "has_audio": stream.has_audio,
                }
            )
        candidate.variants = variants
        candidate.variant_count = len(variants)
        if analysis.audio_streams:
            first = analysis.audio_streams[0]
            candidate.split_audio_url = str(first.direct_stream_url or "")
            candidate.language = str(first.language or "")
            candidate.audio_codec = candidate.audio_codec or str(first.codec or "")
        if candidate.duration_seconds is None and analysis.duration_seconds is not None:
            candidate.duration_seconds = analysis.duration_seconds
        if analysis.is_live:
            candidate.is_live = True
        best = max((item for item in variants if item.get("height")), key=lambda item: int(item.get("height") or 0), default=None)
        if best:
            candidate.width = candidate.width or best.get("width")
            candidate.height = candidate.height or best.get("height")
            candidate.video_codec = candidate.video_codec or str(best.get("codec") or "")
            candidate.bitrate = candidate.bitrate or best.get("bitrate")
        self._emit("media_manifest_parsed", f"{len(variants)} variant(s) parsed", {"candidate_id": candidate.candidate_id, "variants": len(variants)})

    def _record(self, candidate: DetectedMediaCandidate) -> None:
        now = time.time()
        candidate.first_seen_at = candidate.first_seen_at or now
        candidate.last_seen_at = now
        with self._lock:
            stored = self._collection.add(candidate)
        if stored is None:
            return
        stored.last_seen_at = now
        if stored is not candidate:
            return
        self._emit(
            "media_candidate_discovered",
            "Media candidate discovered",
            {
                "candidate_id": stored.candidate_id,
                "url": redact_url(stored.final_url or stored.discovered_url),
                "media_kind": stored.media_kind,
                "discovery_method": stored.discovery_method,
                "discovery_channel": stored.discovery_channel,
                "frame_depth": stored.frame_depth,
            },
        )
        if self.probe is not None and stored.candidate_id not in self._queued_candidates:
            self._queued_candidates.add(stored.candidate_id)
            self._probe_queue.put((self._probe_priority(stored), next(self._probe_sequence), stored))

    def _probe_priority(self, candidate: DetectedMediaCandidate) -> int:
        target = candidate.final_url or candidate.discovered_url
        priority = 50
        if candidate.is_manifest:
            priority -= 20
        if candidate.media_kind == "video":
            priority -= 15
        if "playing" in str(candidate.discovery_method or "").casefold():
            priority -= 20
        if candidate.height and candidate.height >= 480:
            priority -= 5
        from .media_candidates import looks_like_advertising

        if looks_like_advertising(target):
            priority += 100
        return priority

    def _online(self) -> bool:
        if self.connectivity_checker is None:
            return True
        try:
            return bool(self.connectivity_checker())
        except Exception:
            return True

    def _set_detector_connectivity(self, online: bool) -> None:
        value = bool(online)
        if value == self._connectivity_state:
            return
        self._connectivity_state = value
        if value:
            self._set_stage("observing_network", "Connectivity restored; resuming media observation")
            self._emit("media_detector_connectivity_restored", "Detector connectivity restored; observation resumed")
        else:
            self._set_stage("waiting_for_connection", "Connectivity lost; media observation is paused")
            self._emit("media_detector_connectivity_lost", "Detector connectivity lost; observation and verification are paused", {}, "warning")

    def _frame_depth(self, frame) -> int:
        depth = 0
        current = frame
        while True:
            try:
                parent = current.parent_frame
            except Exception:
                break
            if parent is None:
                break
            depth += 1
            current = parent
            if depth > self.max_frame_depth + 2:
                break
        return depth

    def _count_event(self) -> bool:
        report_limit = False
        with self._lock:
            if self.state.network_events_seen >= self.max_network_events:
                if not self._event_limit_reported:
                    self._event_limit_reported = True
                    report_limit = True
            else:
                self.state.network_events_seen += 1
                return True
        if report_limit:
            self._emit("media_detector_event_limit_reached", "Media observation reached its bounded event limit", {"limit": self.max_network_events}, "warning")
        return False

    def _on_request(self, request) -> None:
        try:
            url = str(request.url or "")
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
        except Exception:
            return
        if url.startswith(("data:", "blob:", "about:")):
            return
        if resource_type in NOISE_RESOURCE_TYPES:
            return
        if not is_media_reference(url, ""):
            return
        if not self._count_event():
            return
        if looks_like_segment(url, ""):
            with self._lock:
                self._collection.add(build_candidate(self.state.page_url, url, "network+segment"))
            return
        candidate = self._build_from_request(request, url, resource_type, "", None)
        if candidate is not None:
            candidate.discovery_channel = "network_request"
            self._record(candidate)

    def _on_request_failed(self, request) -> None:
        try:
            url = str(request.url or "")
            failure = str(getattr(request, "failure", "") or "")
        except Exception:
            return
        if not is_media_reference(url, ""):
            return
        self._emit("media_request_failed", failure or "Media request failed", {"url": redact_url(url)}, "warning")

    def _build_from_request(self, request, url: str, resource_type: str, mime: str, status: int | None) -> DetectedMediaCandidate | None:
        method = "GET"
        referer = ""
        required: dict[str, str] = {}
        frame_url = ""
        depth = 0
        initiator = ""
        try:
            if request is not None:
                method = str(request.method or "GET")
                headers = request.all_headers()
                referer = str(headers.get("referer") or "")
                for name in ("referer", "user-agent", "origin", "cookie", "authorization"):
                    value = headers.get(name)
                    if value:
                        required[name] = str(value)
                frame = getattr(request, "frame", None)
                if frame is not None:
                    frame_url = str(getattr(frame, "url", "") or "")
                    depth = self._frame_depth(frame)
                initiator = str(getattr(request, "redirected_from", None) and request.redirected_from.url or "")
        except Exception:
            required = {}
        candidate = build_candidate(
            self.state.page_url,
            url,
            "network+response" if status is not None else "network+request",
            mime_type=mime,
            final_url=url,
            source_frame_url=frame_url,
            frame_depth=depth,
            request_method=method,
            status_code=status,
            page_title=self._page_title,
            poster_or_thumbnail=self._page_poster,
            referer=referer,
            user_agent=self._user_agent,
            required_headers=required,
            required_cookie_scope=_cookie_scope(url) if _has_cookie(request) else "",
        )
        candidate.resource_type = resource_type
        candidate.initiator_url = initiator
        return candidate

    def _on_response(self, response) -> None:
        try:
            url = str(response.url or "")
            status = int(response.status)
            headers = response.headers or {}
        except Exception:
            return
        if url.startswith(("data:", "blob:", "about:")):
            return
        request = getattr(response, "request", None)
        resource_type = ""
        try:
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
        except Exception:
            resource_type = ""
        if resource_type in NOISE_RESOURCE_TYPES:
            return
        mime = normalized_mime(headers.get("content-type") or "")
        media = is_media_reference(url, mime)
        sniffed = ""
        if not media:
            if looks_like_observation_noise(url):
                return
            if not should_sniff(url, mime, resource_type):
                return
            sniffed = self._sniff_response(response)
            if kind_for_sniffed(sniffed) == "unknown":
                return
        if not self._count_event():
            return
        if media and looks_like_segment(url, mime):
            with self._lock:
                self._collection.add(build_candidate(self.state.page_url, url, "network+segment", mime_type=mime))
            return
        size = None
        try:
            raw_length = headers.get("content-length")
            size = int(raw_length) if raw_length else None
        except Exception:
            size = None
        candidate = self._build_from_request(request, url, resource_type, mime, status)
        if candidate is None:
            return
        candidate.discovery_channel = "network_response"
        candidate.filesize_bytes = size
        disposition = str(headers.get("content-disposition") or "")
        if disposition:
            candidate.content_disposition = disposition
        if sniffed:
            candidate.sniffed_type = sniffed
            candidate.media_kind = kind_for_sniffed(sniffed)
            candidate.is_manifest = True
            candidate.discovery_method = "network+response+sniff"
            self._emit(
                "media_content_sniffed",
                f"Server reported {mime or 'no content type'} but the body is {sniffed}",
                {"url": redact_url(url), "sniffed_type": sniffed},
            )
        if status >= 400:
            candidate.verification_state = "probe_failed"
            candidate.verification_detail = f"Response status {status}"
        self._record(candidate)

    def _sniff_response(self, response) -> str:
        try:
            payload = response.body()
        except Exception:
            return ""
        if not payload:
            return ""
        return sniff_media_body(payload[:8192])

    def _on_download(self, download) -> None:
        try:
            url = str(download.url or "")
            suggested = str(getattr(download, "suggested_filename", "") or "")
        except Exception:
            return
        if not url or url.startswith(("data:", "blob:", "about:")):
            return
        candidate = build_candidate(
            self.state.page_url,
            url,
            "browser+download",
            final_url=url,
            page_title=self._page_title,
            user_agent=self._user_agent,
        )
        candidate.discovery_channel = "download_event"
        candidate.suggested_filename = suggested
        if suggested and not candidate.title:
            candidate.title = suggested
        self._emit("media_download_event", "Browser download event observed", {"url": redact_url(url), "filename": suggested})
        self._record(candidate)
        try:
            download.cancel()
        except Exception:
            return

    def _on_popup(self, page) -> None:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        self._emit("media_detector_popup", "Popup page observed", {"url": redact_url(url)})
        self._attach_page(page)

    def _on_instrumented(self, source, payload) -> None:
        if not isinstance(payload, dict):
            return
        channel = str(payload.get("channel") or "xhr")
        url = str(payload.get("finalUrl") or payload.get("url") or "")
        page_url = str(payload.get("pageUrl") or self.state.page_url)
        mime = normalized_mime(str(payload.get("contentType") or ""))
        body = str(payload.get("body") or "")
        status = payload.get("status")
        try:
            status = int(status) if status else None
        except Exception:
            status = None
        absolute = _absolute(page_url, url)
        body_has_media = bool(body and (URL_IN_BODY.search(body) or RELATIVE_MEDIA_IN_BODY.search(body)))
        if looks_like_observation_noise(absolute) and not is_media_reference(absolute, mime):
            return
        if not (is_media_reference(absolute, mime) or body_has_media or (body and sniff_media_body(body))):
            return
        if not self._count_event():
            return
        if absolute and not absolute.startswith(("data:", "blob:", "about:")):
            sniffed = sniff_media_body(body) if body else ""
            if is_media_reference(absolute, mime) or sniffed:
                if not looks_like_segment(absolute, mime):
                    candidate = build_candidate(
                        self.state.page_url,
                        absolute,
                        f"{channel}+response",
                        mime_type=mime,
                        final_url=absolute,
                        status_code=status,
                        page_title=self._page_title,
                        poster_or_thumbnail=self._page_poster,
                        user_agent=self._user_agent,
                    )
                    candidate.discovery_channel = channel
                    candidate.resource_type = channel
                    disposition = str(payload.get("contentDisposition") or "")
                    if disposition:
                        candidate.content_disposition = disposition
                    if sniffed:
                        candidate.sniffed_type = sniffed
                        resolved = kind_for_sniffed(sniffed)
                        if resolved != "unknown":
                            candidate.media_kind = resolved
                            candidate.is_manifest = True
                    self._record(candidate)
        if body:
            self._harvest_body(body, page_url, channel)

    def _harvest_body(self, body: str, page_url: str, channel: str) -> None:
        found = 0
        for match in URL_IN_BODY.findall(body):
            if found >= 25:
                break
            target = str(match).rstrip(".,);\"'")
            if not is_media_reference(target, "") or looks_like_segment(target, ""):
                continue
            candidate = build_candidate(
                self.state.page_url,
                target,
                f"{channel}+body",
                final_url=target,
                page_title=self._page_title,
                poster_or_thumbnail=self._page_poster,
                user_agent=self._user_agent,
                referer=page_url,
                required_headers={"referer": page_url} if page_url else None,
            )
            candidate.discovery_channel = f"{channel}_body"
            candidate.resource_type = channel
            self._record(candidate)
            found += 1
        for match in RELATIVE_MEDIA_IN_BODY.findall(body):
            if found >= 25:
                break
            target = _absolute(page_url, str(match))
            if not target or looks_like_segment(target, ""):
                continue
            candidate = build_candidate(
                self.state.page_url,
                target,
                f"{channel}+body",
                final_url=target,
                page_title=self._page_title,
                poster_or_thumbnail=self._page_poster,
                user_agent=self._user_agent,
                referer=page_url,
                required_headers={"referer": page_url} if page_url else None,
            )
            candidate.discovery_channel = f"{channel}_body"
            candidate.resource_type = channel
            self._record(candidate)
            found += 1

    def _attach_page(self, page) -> None:
        if page in self._pages:
            return
        self._pages.append(page)
        try:
            page.on("download", self._on_download)
        except Exception:
            pass
        try:
            page.on("popup", self._on_popup)
        except Exception:
            pass

    def _scan_frames(self, page) -> None:
        try:
            frames = list(page.frames)
        except Exception:
            return
        examined = 0
        for frame in frames:
            if examined >= self.max_frames or self._stop.is_set():
                break
            depth = self._frame_depth(frame)
            if depth > self.max_frame_depth:
                continue
            examined += 1
            frame_url = ""
            try:
                frame_url = str(frame.url or "")
            except Exception:
                frame_url = ""
            if frame_url and frame_url not in self._seen_frames:
                self._seen_frames.add(frame_url)
                self._emit("media_detector_frame", "Frame inspected", {"frame_url": redact_url(frame_url), "depth": depth})
            try:
                payload = frame.evaluate(DOM_PROBE)
            except Exception:
                continue
            self._ingest_dom(payload, frame_url, depth)
        with self._lock:
            self.state.frames_seen = max(self.state.frames_seen, examined)

    def _ingest_dom(self, payload: Any, frame_url: str, depth: int) -> None:
        if not isinstance(payload, dict):
            return
        title = str(payload.get("title") or "")
        if title and not self._page_title:
            self._page_title = title
        poster = str(payload.get("poster") or "")
        if poster and not self._page_poster:
            self._page_poster = _absolute(frame_url or self.state.page_url, poster)
        page_duration = payload.get("duration")
        if isinstance(page_duration, (int, float)) and page_duration and self._page_duration is None:
            self._page_duration = float(page_duration)
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("url") or "")
            if not raw or raw.startswith(("data:", "about:")):
                continue
            if raw.startswith("blob:"):
                if item.get("encrypted"):
                    self._encrypted_seen = True
                continue
            if item.get("encrypted"):
                self._encrypted_seen = True
                continue
            absolute = _absolute(frame_url or self.state.page_url, raw)
            mime = str(item.get("type") or "")
            if not is_media_reference(absolute, mime):
                continue
            candidate = build_candidate(
                self.state.page_url,
                absolute,
                f"dom+{item.get('kind') or 'element'}",
                mime_type=mime,
                final_url=absolute,
                source_frame_url=frame_url,
                frame_depth=depth,
                page_title=self._page_title,
                poster_or_thumbnail=_absolute(frame_url or self.state.page_url, str(item.get("poster") or "")) or self._page_poster,
                user_agent=self._user_agent,
            )
            candidate.discovery_channel = "dom"
            duration = item.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                candidate.duration_seconds = float(duration)
            width = item.get("width")
            height = item.get("height")
            if isinstance(width, int) and width > 0:
                candidate.width = width
            if isinstance(height, int) and height > 0:
                candidate.height = height
            if item.get("playing"):
                candidate.discovery_method = f"{candidate.discovery_method}+playing"
            self._record(candidate)

    def _teardown(self, context, browser) -> None:
        started = time.monotonic()
        for name in ("request", "response", "requestfailed", "page"):
            try:
                context.remove_listener(name, getattr(self, _LISTENER_METHODS[name]))
            except Exception:
                continue
        try:
            context.close()
        except Exception as exc:
            self._emit("media_detector_context_close_incomplete", str(exc), {}, "warning")
        try:
            browser.close()
        except Exception as exc:
            detail = str(exc)
            level = "info" if "WinError 32" in detail or "being used by another process" in detail else "warning"
            self._emit(
                "media_detector_profile_cleanup_incomplete",
                "Chromium closed but its temporary profile could not be removed yet",
                {"error": detail},
                level,
            )
        self._emit("media_detector_browser_closed", "Chromium session closed", {"seconds": round(time.monotonic() - started, 3)})

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        executable = self.executable or ensure_browser("playwright")
        try:
            with sync_playwright() as driver:
                self._set_stage("loading_browser", "Opening Chromium")
                launch_options: dict[str, Any] = {"headless": self.state.headless}
                if executable:
                    launch_options["executable_path"] = executable
                browser = driver.chromium.launch(**launch_options)
                try:
                    context = browser.new_context(accept_downloads=True)
                    if self.instrument:
                        try:
                            context.expose_binding(BINDING_NAME, lambda source, payload: self._on_instrumented(source, payload))
                            script = INSTRUMENTATION.replace("__VIDEOHAUL_BINDING__", BINDING_NAME).replace("__VIDEOHAUL_BODY_LIMIT__", str(DEFAULT_MAX_INSTRUMENTED_BODY))
                            context.add_init_script(script)
                        except Exception as exc:
                            self._emit("media_detector_instrumentation_failed", str(exc), {}, "warning")
                    context.on("request", self._on_request)
                    context.on("response", self._on_response)
                    context.on("requestfailed", self._on_request_failed)
                    context.on("page", self._on_popup)
                    page = context.new_page()
                    self._attach_page(page)
                    try:
                        self._user_agent = str(page.evaluate("() => navigator.userAgent") or "")
                    except Exception:
                        self._user_agent = ""
                    self._set_stage("waiting_for_page", "Loading page")
                    try:
                        navigation = page.goto(self.state.page_url, wait_until="domcontentloaded", timeout=45000)
                        if navigation is not None:
                            with self._lock:
                                self.state.main_status = int(navigation.status)
                    except Exception as exc:
                        with self._lock:
                            self.state.error = str(exc)
                        self._emit("media_detector_navigation_failed", str(exc), {}, "warning")
                    self._set_stage("observing_network", "Observing network" if self.state.mode == "automatic" else "Waiting for interaction")
                    if self.interaction is not None:
                        try:
                            self.interaction(page)
                        except Exception as exc:
                            self._emit("media_detector_interaction_failed", str(exc), {}, "warning")
                    deadline = time.monotonic() + self.budget_seconds
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        online = self._online()
                        self._set_detector_connectivity(online)
                        if not online:
                            self._stop.wait(DOM_POLL_SECONDS)
                            continue
                        self._scan_frames(page)
                        if self.state.mode == "automatic" and self._collection.values() and time.monotonic() > deadline - self.budget_seconds * 0.4:
                            break
                        try:
                            page.wait_for_timeout(int(DOM_POLL_SECONDS * 1000))
                        except Exception:
                            time.sleep(DOM_POLL_SECONDS)
                    self._scan_frames(page)
                finally:
                    self._teardown(context, browser)
        except Exception as exc:
            with self._lock:
                self.state.error = str(exc)
            self._emit("media_detector_failed", str(exc), {}, "error")
        finally:
            self._drain_probe_queue()
            with self._lock:
                self.state.active = False
                self.state.finished_at = time.time()
                self.state.stage = "finished"
                found = len(self._collection.values())
                self.state.message = f"{found} media candidate(s) detected" if found else "No media candidates detected"
                if self._encrypted_seen:
                    self.state.message = f"{self.state.message}; protected playback was observed and left untouched"
            self._emit(
                "media_detector_finished",
                self.state.message,
                {
                    "candidates": len(self._collection.values()),
                    "frames": self.state.frames_seen,
                    "network_events": self.state.network_events_seen,
                },
            )
            self._finished.set()

    def _drain_probe_queue(self, timeout: float = 120.0) -> None:
        if self.probe is None:
            return
        if not self._online():
            self._emit("media_candidate_verification_deferred", "Candidate verification stopped at the session boundary because connectivity is unavailable", {"queued": self._probe_queue.unfinished_tasks}, "warning")
            return
        self._set_stage("verifying_candidates", "Verifying candidates")
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if self._probe_queue.unfinished_tasks == 0:
                return
            time.sleep(0.1)


class MediaDetectorService:
    def __init__(self, observer=None, probe=None, session_factory=None, connectivity_checker=None):
        self.observer = observer
        self.probe = probe
        self.session_factory = session_factory or DetectorSession
        self.connectivity_checker = connectivity_checker
        self._sessions: dict[str, DetectorSession] = {}
        self._lock = threading.RLock()

    def open(self, page_url: str, job_id: str = "", mode: str = "interactive", headless: bool | None = None, budget_seconds: float | None = None) -> DetectorSession:
        normalized_mode = "automatic" if str(mode) == "automatic" else "interactive"
        resolved_headless = bool(headless) if headless is not None else normalized_mode == "automatic"
        normalized_url = str(page_url or "").strip()
        normalized_job = str(job_id or "").strip()
        with self._lock:
            for existing in self._sessions.values():
                snapshot = existing.snapshot()
                if not bool(snapshot.get("active")):
                    continue
                existing_job = str(snapshot.get("job_id") or "").strip()
                same_owner = existing_job == normalized_job if normalized_job else not existing_job
                same_page = str(snapshot.get("page_url") or "").strip() == normalized_url
                same_mode = str(snapshot.get("mode") or "interactive") == normalized_mode
                same_headless = bool(snapshot.get("headless")) == resolved_headless
                if same_owner and same_page and same_mode and same_headless:
                    return existing
            session = self.session_factory(
                page_url=normalized_url,
                job_id=normalized_job,
                headless=resolved_headless,
                mode=normalized_mode,
                budget_seconds=budget_seconds,
                observer=self.observer,
                probe=self.probe,
                connectivity_checker=self.connectivity_checker,
            )
            self._sessions[session.session_id] = session
        try:
            session.start()
        except Exception:
            with self._lock:
                if self._sessions.get(session.session_id) is session:
                    self._sessions.pop(session.session_id, None)
            try:
                session.stop(timeout=2.0)
            except Exception:
                pass
            raise
        return session

    def get(self, session_id: str) -> DetectorSession:
        with self._lock:
            session = self._sessions.get(str(session_id))
        if session is None:
            raise KeyError(session_id)
        return session

    def close(self, session_id: str, wait_seconds: float = 1.5) -> dict:
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
        if session is None:
            raise KeyError(session_id)
        session.request_stop()
        session.wait(timeout=max(0.0, float(wait_seconds)))
        snapshot = session.snapshot()
        session.clear_sensitive_context()
        finisher = threading.Thread(target=session.stop, kwargs={"timeout": 30.0}, name=f"videohaul-detector-close-{session_id[:8]}", daemon=True)
        finisher.start()
        return snapshot

    def discard(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
        if session is not None:
            session.clear_sensitive_context()
            session.stop(timeout=5.0)

    def sessions(self) -> list[dict]:
        with self._lock:
            values = list(self._sessions.values())
        return [session.snapshot() for session in values]

    def shutdown(self) -> None:
        with self._lock:
            values = list(self._sessions.values())
            self._sessions.clear()
        for session in values:
            session.clear_sensitive_context()
            session.stop(timeout=5.0)


def detect_media(page_url: str, budget_seconds: float | None = None, executable: str | None = None, observer=None, probe=None) -> tuple[list[DetectedMediaCandidate], int | None]:
    session = DetectorSession(
        page_url=page_url,
        headless=True,
        mode="automatic",
        budget_seconds=budget_seconds,
        executable=executable,
        observer=observer,
        probe=probe,
    )
    session.start()
    session.wait(timeout=(budget_seconds or DEFAULT_AUTOMATIC_BUDGET_SECONDS) + 60.0)
    session.stop(timeout=10.0)
    return session.candidates(), session.state.main_status


def _absolute(base: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "blob:", "data:")):
        return text
    try:
        return urljoin(str(base or ""), text)
    except Exception:
        return text


def _has_cookie(request) -> bool:
    try:
        headers = request.all_headers() if request is not None else {}
        return bool(headers.get("cookie"))
    except Exception:
        return False


def _cookie_scope(url: str) -> str:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(url or ""))
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""
