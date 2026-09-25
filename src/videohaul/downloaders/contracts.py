from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TransferResult:
    returncode: int
    detail: str = ""
    output_path: str = ""
    component: str = "download"
    retryable: bool = False
    paused: bool = False
    stopped: bool = False
    restart: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.paused and not self.stopped


def normalize_result(value) -> TransferResult:
    if isinstance(value, TransferResult):
        return value
    if isinstance(value, tuple):
        return TransferResult(
            returncode=int(value[0]) if value else 1,
            detail=str(value[1]) if len(value) > 1 else "",
            output_path=str(value[2]) if len(value) > 2 else "",
        )
    return TransferResult(returncode=int(value or 0))
