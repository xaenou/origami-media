from dataclasses import dataclass


@dataclass
class FfmpegMetadata:
    width: int
    height: int
    duration: float
