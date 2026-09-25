from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _fatal(message: str) -> None:
    text = str(message or "VideoHaul could not start.")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "VideoHaul", 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def _ensure_runtime() -> None:
    from videohaul.bootstrap import ensure_python_runtime
    ensure_python_runtime()


def main() -> None:
    if not ((3, 10) <= sys.version_info[:2] < (3, 15)):
        _fatal("VideoHaul requires Python 3.10 through 3.14.")
        raise SystemExit(1)
    try:
        _ensure_runtime()
        from videohaul.app import main as run
        run()
    except subprocess.CalledProcessError as exc:
        from videohaul.bootstrap import manual_install_hint

        _fatal(f"VideoHaul could not install its Python packages (pip exit code {exc.returncode}). Install them manually: {manual_install_hint()}")
        raise SystemExit(1)
    except Exception as exc:
        _fatal(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
