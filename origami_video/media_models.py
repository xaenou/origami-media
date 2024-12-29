import os
from dataclasses import dataclass
from io import BytesIO
from typing import Optional


@dataclass
class VideoMetadata:
    url: str
    id: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    ext: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class ThumbnailMetadata:
    url: Optional[str] = None
    ext: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

    def __post_init__(self):
        if self.url and not self.ext:
            self.ext = os.path.splitext(self.url)[-1].lstrip(".") or None


@dataclass
class VideoData:
    stream: BytesIO
    info: VideoMetadata
    size: int


@dataclass
class ThumbnailData:
    stream: BytesIO
    info: ThumbnailMetadata
    size: int
