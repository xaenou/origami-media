from dataclasses import dataclass
from io import BytesIO
from typing import Optional


@dataclass
class YTDLPMetadata:
    url: str
    id: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None


@dataclass
class StreamMetadata:
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None


@dataclass
class MediaMetadata:
    url: str
    id: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None
    has_video: bool = False
    has_audio: bool = False
    is_image: bool = False


@dataclass
class Media:
    filename: str
    stream: BytesIO
    metadata: MediaMetadata

    def __repr__(self):
        return f"Media(filename={self.filename!r}, " f"metadata={self.metadata!r})"
