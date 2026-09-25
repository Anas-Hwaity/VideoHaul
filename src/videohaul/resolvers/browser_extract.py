from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlparse

from ..browser import find_browser
from .common import AuthenticationRequired, MediaUnresolved, ResolverDependencyError, ResolverUnsupported, SourceRefusedRequest, authentication_from_text
from .generic_html import GenericHtmlResolver


class BrowserExtractResolver:
    name = "browser_extract"

    def __init__(self, executable: str | None = None):
        self.executable = executable
        self.html_resolver = GenericHtmlResolver()

    def supports(self, url: str) -> bool:
        return urlparse(url).scheme.lower() in {"http", "https"}

    def analyze(self, url: str, browser_cookies: str = "none"):
        executable = self.executable or find_browser()
        if not executable:
            raise ResolverDependencyError("No compatible headless browser is available")
        args = [executable, "--headless=new", "--disable-gpu", "--disable-extensions", "--disable-background-networking", "--dump-dom", url]
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="backslashreplace", timeout=45, creationflags=flags)
        except Exception as exc:
            raise MediaUnresolved("Browser extraction could not resolve media") from exc
        text = (result.stderr or "").lower()
        if result.returncode != 0:
            if re.search(r"\b401\b", text) or authentication_from_text(text):
                raise AuthenticationRequired("Authentication required")
            if re.search(r"\b403\b", text):
                raise SourceRefusedRequest("The source refused the browser extraction request")
            raise MediaUnresolved("Browser extraction could not resolve media")
        html = result.stdout or ""
        if not html.strip():
            raise ResolverUnsupported("Browser produced no page DOM")
        analysis = self.html_resolver.analyze(url, browser_cookies, html=html)
        analysis.metadata["resolver"] = self.name
        analysis.metadata["browser_executable"] = str(executable)
        return analysis
