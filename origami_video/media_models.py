from dataclasses import dataclass, field
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
class MediaMetadata(YTDLPMetadata, StreamMetadata):
    url: str = field(init=True)
    id: str = field(init=True)
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None


@dataclass
class Media:
    filename: str
    stream: BytesIO
    metadata: MediaMetadata
