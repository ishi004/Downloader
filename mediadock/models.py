import math
import re
import string
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]


def validate_url(value: str) -> str:
    value = value.strip()
    p = urlsplit(value)
    if len(value) > 4096 or p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise ValueError('인증 정보가 없는 HTTP 또는 HTTPS 링크를 입력하세요.')
    if any(ord(c) < 32 for c in value):
        raise ValueError('링크에 제어 문자를 사용할 수 없습니다.')
    _ = p.port
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Options(StrictModel):
    kind: Literal['video', 'audio'] = 'video'
    container: Literal['auto', 'mp4', 'mkv'] = 'auto'
    height: int | None = Field(default=None, ge=1, le=16384)
    fps: float | None = Field(default=None, gt=0, le=1000)
    audio_format: Literal['mp3', 'm4a', 'original'] = 'mp3'
    mp3_mode: Literal['vbr', 'cbr'] = 'vbr'
    mp3_quality: int = Field(default=0, ge=0, le=9)
    bitrate: Literal[64, 96, 128, 160, 192, 256, 320] = 192
    subtitle_mode: Literal['none', 'srt', 'embed', 'burn'] = 'none'
    subtitle_lang: str | None = None
    auto_subtitles: bool = False
    thumbnail: bool = False
    start: float = Field(default=0, ge=0, le=604800)
    end: float | None = Field(default=None, gt=0, le=604800)

    @model_validator(mode='after')
    def coherence(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError('종료 시간은 시작 시간보다 뒤여야 합니다.')
        if self.subtitle_mode != 'none' and (not self.subtitle_lang or not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', self.subtitle_lang)):
            raise ValueError('제공되는 자막 언어를 선택하세요.')
        if self.kind == 'audio' and self.subtitle_mode in ('embed', 'burn'):
            raise ValueError('음원은 별도 SRT 자막만 지원합니다.')
        return self

    def normalized(self):
        d = self.model_dump()
        if self.kind == 'video':
            for k in ('audio_format', 'mp3_mode', 'mp3_quality', 'bitrate'):
                d.pop(k)
        else:
            for k in ('container', 'height', 'fps'):
                d.pop(k)
            if self.audio_format != 'mp3':
                for k in ('mp3_mode', 'mp3_quality', 'bitrate'):
                    d.pop(k)
            else:
                d.pop('bitrate' if self.mp3_mode == 'vbr' else 'mp3_quality')
        if self.subtitle_mode == 'none':
            d.pop('subtitle_lang')
            d.pop('auto_subtitles')
        return d


class Settings(StrictModel):
    app_name: str = Field(default='MediaDock', min_length=1, max_length=40)
    port: int = Field(default=8765, ge=1024, le=65535)
    save_dir: str = str(ROOT / 'downloads')
    subfolder: str = ''
    by_channel: bool = False
    filename: str = '{title} [{id}]'
    concurrency: int = Field(default=2, ge=1, le=4)
    speed_kib: int = Field(default=0, ge=0, le=1048576)
    remember: bool = True
    defaults: Options = Field(default_factory=Options)

    @field_validator('save_dir')
    @classmethod
    def root_path(cls, v):
        if not v.strip() or not Path(v).is_absolute() or v.startswith(('\\\\', '//')):
            raise ValueError('이 컴퓨터의 절대 저장 경로를 입력하세요. 네트워크 경로는 지원하지 않습니다.')
        return str(Path(v).resolve())

    @field_validator('subfolder')
    @classmethod
    def safe_subfolder(cls, v):
        v = v.strip().replace('\\', '/')
        if not v:
            return ''
        if len(v) > 120 or any(p in ('', '.', '..') or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith(('.', ' ')) for p in v.split('/')):
            raise ValueError('하위 폴더는 상대 경로만 가능합니다. 예: 여행/2026')
        return v

    @field_validator('filename')
    @classmethod
    def safe_template(cls, v):
        if not v.strip() or len(v) > 140 or re.search(r'[<>:"/\\|?*\x00-\x1f]', v):
            raise ValueError('파일명에 경로 또는 특수 문자를 사용할 수 없습니다.')
        for _, field, spec, conv in string.Formatter().parse(v):
            if field is not None and (field not in ('title', 'id', 'channel', 'date') or spec or conv):
                raise ValueError('파일명 변수는 {title}, {id}, {channel}, {date}만 지원합니다.')
        return v


class InspectRequest(StrictModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    playlist: bool = True
    @field_validator('urls')
    @classmethod
    def urls_valid(cls, urls):
        return [validate_url(u) for u in urls]


class QueueItem(StrictModel):
    media_id: str
    options: Options


class QueueRequest(StrictModel):
    items: list[QueueItem] = Field(min_length=1, max_length=200)


def safe_name(value: str, limit=100):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:limit]
    if not value or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    return value
