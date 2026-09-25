# Third-party notices

VideoHaul orchestrates external engines rather than bundling them. Nothing in
this list is redistributed inside the VideoHaul source tree. On Windows, yt-dlp,
FFmpeg and Deno can be fetched from their official distribution channels, stored
in a persistent per-user location, validated, and reused. Playwright Chromium can
be installed through Playwright when a compatible browser runtime is needed. On
macOS and Linux, VideoHaul can reuse compatible tools already installed on the
system.

## Managed runtime engines

| Engine | Role in VideoHaul | License | Project |
| --- | --- | --- | --- |
| yt-dlp | Extractor and downloader for a wide range of sites | Unlicense | https://github.com/yt-dlp/yt-dlp |
| FFmpeg | Muxing, remuxing, post-processing, cover-art embedding | LGPL-2.1-or-later / GPL-2.0-or-later depending on build | https://ffmpeg.org |
| FFprobe | Output validation and media-candidate verification | Same terms as the FFmpeg build in use | https://ffmpeg.org |
| Streamlink | Live and stream-oriented source handling | BSD-2-Clause | https://streamlink.github.io |
| Deno (optional) | JavaScript runtime that some extractor paths need | MIT | https://deno.com |
| Chromium-based browser (Playwright Chromium or a compatible installed Chrome, Edge or Chromium) | Browser-backed media discovery and the interactive Media Detector | The license and third-party notices of the browser build in use | https://www.chromium.org |

FFmpeg licensing depends on how the specific binary was compiled. A build that
enables GPL-licensed components is distributed under the GPL. Because VideoHaul
does not ship an FFmpeg binary, the license that applies is the one attached to
the build present on the user's machine.

## Optional, never installed by VideoHaul

| Component | Role | License | Project |
| --- | --- | --- | --- |
| VLC media player | Optional preview of a verified media candidate | GPL-2.0-or-later | https://www.videolan.org/vlc/ |

VideoHaul detects an existing VLC installation and reuses it. It never downloads
or installs VLC, and preview is unavailable rather than automatic when VLC is
absent.

## Python packages

VideoHaul depends on the following packages at runtime, installed through the
normal Python packaging tooling:

| Package | License |
| --- | --- |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| pywebview | BSD-3-Clause |
| Streamlink | BSD-2-Clause |
| Playwright for Python | Apache-2.0 |


Each package remains governed by its own license. Consult the package metadata
in your environment for the authoritative text.
