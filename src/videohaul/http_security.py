from __future__ import annotations

import http.server
import ipaddress
from pathlib import Path
import re
import secrets
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urljoin, urlparse
import xml.etree.ElementTree as ET

CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
SENSITIVE_FORWARD_HEADERS = frozenset({*CREDENTIAL_HEADERS, "origin", "referer"})
NON_PUBLIC_SPECIAL_NETWORKS = (ipaddress.ip_network("192.88.99.0/24"),)


def origin_of(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, int(port)


def same_origin(left: str, right: str) -> bool:
    first = origin_of(left)
    return first is not None and first == origin_of(right)


def has_sensitive_headers(headers: dict[str, str] | None) -> bool:
    return any(str(name).casefold() in CREDENTIAL_HEADERS for name in dict(headers or {}))


def scope_headers(headers: dict[str, str] | None, target_url: str, credential_origin_url: str | None) -> dict[str, str]:
    values = {str(name): str(value) for name, value in dict(headers or {}).items() if str(name).strip() and str(value).strip()}
    if credential_origin_url and same_origin(target_url, credential_origin_url):
        return values
    return {name: value for name, value in values.items() if name.casefold() not in SENSITIVE_FORWARD_HEADERS}


def is_public_unicast_address(address: str) -> bool:
    try:
        value = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if not value.is_global or value.is_multicast or value.is_unspecified or value.is_loopback or value.is_link_local or value.is_private or value.is_reserved:
        return False
    return not any(value in network for network in NON_PUBLIC_SPECIAL_NETWORKS if value.version == network.version)


def resolve_public_endpoints(hostname: str, port: int) -> list[tuple]:
    host = str(hostname or "").casefold().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Automatic backend requests cannot access localhost")
    try:
        endpoints = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Remote host could not be resolved") from exc
    if not endpoints:
        raise ValueError("Remote host did not resolve to an address")
    if any(not is_public_unicast_address(item[4][0]) for item in endpoints):
        raise ValueError("Automatic backend requests require a public unicast address")
    return endpoints



def require_public_http_url(url: str) -> str:
    target = str(url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Remote URL must use HTTP or HTTPS")
    resolve_public_endpoints(parsed.hostname, parsed.port or (443 if parsed.scheme.casefold() == "https" else 80))
    return target


class ScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, credential_origin_url: str | None = None, require_public: bool = False):
        super().__init__()
        self.credential_origin_url = str(credential_origin_url or "")
        self.require_public = bool(require_public)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.require_public:
            require_public_http_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        credential_origin = self.credential_origin_url or str(getattr(req, "full_url", "") or "")
        if not same_origin(newurl, credential_origin):
            for name in list(redirected.headers):
                if str(name).casefold() in SENSITIVE_FORWARD_HEADERS:
                    redirected.remove_header(name)
            for name in list(redirected.unredirected_hdrs):
                if str(name).casefold() in SENSITIVE_FORWARD_HEADERS:
                    redirected.unredirected_hdrs.pop(name, None)
        return redirected


def require_public_response_peer(response) -> None:
    current = response
    seen = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        peer = getattr(current, "getpeername", None)
        if callable(peer):
            try:
                address = peer()[0]
            except Exception as exc:
                raise ValueError("Remote peer address could not be verified") from exc
            if not is_public_unicast_address(str(address)):
                raise ValueError("Automatic backend requests require a public unicast peer")
            return
        next_value = None
        for name in ("fp", "raw", "_sock", "sock", "socket"):
            candidate = getattr(current, name, None)
            if candidate is not None and id(candidate) not in seen:
                next_value = candidate
                break
        current = next_value
    raise ValueError("Remote peer address could not be verified")


class ScopedOpener:
    def __init__(self, opener, require_public: bool):
        self._opener = opener
        self._require_public = bool(require_public)

    def open(self, *args, **kwargs):
        response = self._opener.open(*args, **kwargs)
        if self._require_public:
            try:
                require_public_response_peer(response)
            except Exception:
                response.close()
                raise
        return response


def build_scoped_opener(start_url: str, credential_origin_url: str | None = None, require_public: bool = False):
    target = require_public_http_url(start_url) if require_public else str(start_url or "").strip()
    origin_url = str(credential_origin_url or target)
    opener = urllib.request.build_opener(ScopedRedirectHandler(origin_url, require_public))
    return ScopedOpener(opener, require_public)


class _GuardedRelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _relay(self):
        server = self.server
        parsed_local = urlparse(self.path)
        if not parsed_local.path.startswith(f"/fetch/{server.relay_token}/"):
            self.send_error(404)
            return
        values = parse_qs(parsed_local.query, keep_blank_values=True)
        target = str((values.get("u") or [""])[0]).strip()
        if not target:
            self.send_error(400)
            return
        try:
            headers = scope_headers(server.remote_headers, target, server.credential_origin)
            for name in ("Range", "If-Range"):
                value = str(self.headers.get(name) or "").strip()
                if value:
                    headers[name] = value
            request = urllib.request.Request(target, headers=headers, method="HEAD" if self.command == "HEAD" else "GET")
            opener = build_scoped_opener(target, server.credential_origin, require_public=True)
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            self.send_error(int(exc.code or 502))
            return
        except Exception:
            self.send_error(502, "Blocked or unavailable upstream")
            return
        with response:
            final_url = str(response.geturl() or target)
            content_type = str(response.headers.get("Content-Type") or "")
            kind = server.manifest_kind(final_url, content_type)
            if self.command != "HEAD" and kind:
                limit = 8 * 1024 * 1024
                body = response.read(limit + 1)
                if len(body) > limit:
                    self.send_error(502, "Manifest is too large")
                    return
                try:
                    body = server.rewrite_manifest(kind, body, final_url)
                except Exception:
                    self.send_error(502, "Manifest could not be secured")
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type or ("application/vnd.apple.mpegurl" if kind == "hls" else "application/dash+xml"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return
            status = int(getattr(response, "status", 200) or 200)
            self.send_response(status)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                value = response.headers.get(name)
                if value is not None:
                    self.send_header(name, str(value))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.close_connection = True

    def do_GET(self):
        self._relay()

    def do_HEAD(self):
        self._relay()


class GuardedMediaRelay:
    def __init__(self, headers: dict[str, str] | None = None, credential_origin: str = ""):
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GuardedRelayHandler)
        self._server.daemon_threads = True
        self._server.relay_token = secrets.token_urlsafe(24)
        self._server.remote_headers = dict(headers or {})
        self._server.credential_origin = str(credential_origin or "")
        self._server.manifest_kind = self._manifest_kind
        self._server.rewrite_manifest = self._rewrite_manifest
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="videohaul-guarded-relay")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _encode_target(self, target: str) -> str:
        placeholders = []
        def preserve(match):
            marker = f"VHPLACEHOLDER{len(placeholders)}TOKEN"
            placeholders.append((marker, match.group(0)))
            return marker
        prepared = re.sub(r"\$[^$]+\$", preserve, str(target or ""))
        encoded = quote(prepared, safe="")
        for marker, placeholder in placeholders:
            encoded = encoded.replace(marker, placeholder)
        return encoded

    def url(self, target: str) -> str:
        suffix = str(Path(urlparse(str(target or "")).path).suffix or "").casefold()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ""
        return f"{self.base_url}/fetch/{self._server.relay_token}/resource{suffix}?u={self._encode_target(target)}"

    def _manifest_kind(self, url: str, content_type: str) -> str:
        path = urlparse(str(url or "")).path.casefold()
        mime = str(content_type or "").split(";", 1)[0].strip().casefold()
        if path.endswith((".m3u8", ".m3u")) or mime in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}:
            return "hls"
        if path.endswith(".mpd") or mime in {"application/dash+xml", "video/vnd.mpeg.dash.mpd"}:
            return "dash"
        return ""

    def _rewrite_hls(self, body: bytes, base_url: str) -> bytes:
        text = body.decode("utf-8-sig", "replace")
        output = []
        uri_pattern = re.compile(r'URI=(?:"([^"]+)"|([^,\s]+))')
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                output.append(self.url(urljoin(base_url, line)))
                continue
            def replace_uri(match):
                value = match.group(1) or match.group(2) or ""
                return f'URI="{self.url(urljoin(base_url, value))}"'
            output.append(uri_pattern.sub(replace_uri, raw))
        return ("\n".join(output) + "\n").encode("utf-8")

    def _rewrite_dash(self, body: bytes, base_url: str) -> bytes:
        root = ET.fromstring(body)
        def local(value):
            return str(value).rsplit("}", 1)[-1]
        def visit(node, inherited_base):
            base_children = [child for child in list(node) if local(child.tag) == "BaseURL" and str(child.text or "").strip()]
            effective = urljoin(inherited_base, str(base_children[0].text).strip()) if base_children else inherited_base
            for child in base_children:
                child.text = self.url(urljoin(inherited_base, str(child.text).strip()))
            tag = local(node.tag)
            names = set()
            if tag == "SegmentTemplate":
                names.update({"media", "initialization"})
            if tag == "SegmentURL":
                names.update({"media", "index"})
            if tag in {"Initialization", "RepresentationIndex"}:
                names.add("sourceURL")
            for key, value in list(node.attrib.items()):
                attr = local(key)
                if attr in names or attr == "href":
                    node.attrib[key] = self.url(urljoin(effective, str(value)))
            if tag in {"Location", "PatchLocation"} and str(node.text or "").strip():
                node.text = self.url(urljoin(effective, str(node.text).strip()))
            for child in list(node):
                if child not in base_children:
                    visit(child, effective)
        visit(root, base_url)
        if str(root.tag).startswith("{"):
            ET.register_namespace("", str(root.tag).split("}", 1)[0][1:])
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _rewrite_manifest(self, kind: str, body: bytes, base_url: str) -> bytes:
        if kind == "hls":
            return self._rewrite_hls(body, base_url)
        if kind == "dash":
            return self._rewrite_dash(body, base_url)
        raise ValueError("Unsupported manifest type")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
