"""Pinterest (videos only) and Instagram (reels, videos, photos, carousels, stories).

* Pinterest  -> yt-dlp (video pins only; image pins are rejected on purpose).
* Instagram  -> reels/videos via yt-dlp; photos, carousels (mixed photo+video) and
                stories via gallery-dl (yt-dlp cannot read Instagram photos).
                Each method is the fallback of the other.

Instagram often asks for a login (stories, many posts, server IPs). Provide a cookies
file (see README) to make it reliable.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import aiohttp
from yt_dlp import YoutubeDL

from soundcloud import EXECUTOR, INFO_MAX_AGE, ScError, first, iso_date, run_ffmpeg, to_int

log = logging.getLogger("social")

COOKIES: Optional[str] = None
BRANDS = {"instagram": "اینستاگرام", "pinterest": "پینترست"}
VIDEO_EXT = {"mp4", "mov", "webm", "mkv", "m4v"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "heic"}

IG_AUTH_MSG = ("فایل مورد نظر دانلود نشد"
               "بخاطر محدودیت های اینستاگرام و یا مشکل کوچیک در زیر ساخت ممکن است ربات قادر به دانلود این فایل نباشد.")
PIN_NO_VIDEO = "این پین ویدیو ندارد. فقط پین‌های ویدیویی پشتیبانی می‌شوند."


class AuthRequired(ScError):
    """Instagram wants a logged-in session."""


def configure(cookies_file: Optional[str]) -> None:
    global COOKIES
    COOKIES = cookies_file or None


# --------------------------------------------------------------------------- #
# URL detection
# --------------------------------------------------------------------------- #
IG_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:[\w.]+/)?(?:p|reels?|tv)/[^\s<>\"']+"
    r"|https?://(?:www\.)?instagram\.com/(?:stories|share)/[^\s<>\"']+",
    re.I,
)
PIN_RE = re.compile(
    r"https?://(?:[\w-]+\.)?pinterest\.[a-z.]{2,6}/pin/[^\s<>\"']+|https?://pin\.it/[^\s<>\"']+",
    re.I,
)


def detect(text: Optional[str]) -> Optional[tuple[str, str]]:
    for platform, rx in (("instagram", IG_RE), ("pinterest", PIN_RE)):
        m = rx.search(text or "")
        if m:
            return platform, m.group(0).rstrip(".,;:!?)]}»")
    return None


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


async def resolve(url: str) -> str:
    """Follow short/share links (pin.it, instagram.com/share/...)."""
    if "pin.it/" not in url and "/share/" not in url:
        return url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=_UA) as s:
            async with s.get(url, allow_redirects=True) as r:
                final = str(r.url)
        return url if "accounts/login" in final else final
    except Exception as exc:
        log.debug("resolve failed for %s: %s", url, exc)
        return url


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class MediaItem:
    kind: str                       # "video" | "photo"
    url: str = ""
    ext: str = ""
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class MediaInfo:
    platform: str
    url: str
    mode: str                       # "ytdlp" | "gdl"
    title: str = ""
    description: str = ""
    author: str = ""
    author_name: str = ""
    date: Optional[str] = None
    likes: Optional[int] = None
    views: Optional[int] = None
    comments: Optional[int] = None
    story: bool = False
    items: list = field(default_factory=list)
    ytdlp_info: Optional[dict] = None
    fetched_at: float = 0.0

    @property
    def photos(self) -> int:
        return sum(1 for i in self.items if i.kind == "photo")

    @property
    def videos(self) -> int:
        return sum(1 for i in self.items if i.kind == "video")

    @property
    def brand(self) -> str:
        return BRANDS.get(self.platform, self.platform)


@dataclass
class LocalMedia:
    path: Path
    kind: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    thumb: Optional[Path] = None


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
def _classify(text: str, platform: str) -> ScError:
    s = text.lower()
    if platform == "instagram" and any(k in s for k in (
            "authrequired", "login", "log in", "cookies", "401", "403", "checkpoint",
            "challenge", "rate-limit", "rate limit", "empty media response", "not available")):
        return AuthRequired(IG_AUTH_MSG)
    if platform == "pinterest" and any(k in s for k in (
            "no video", "unsupported url", "no formats", "video formats")):
        return ScError(PIN_NO_VIDEO)
    if "private" in s:
        return ScError("این محتوا خصوصی است.")
    if "404" in s or "not found" in s or "notfound" in s:
        return ScError("محتوا پیدا نشد یا حذف شده است.")
    if "429" in s or "too many" in s:
        return ScError("تعداد درخواست‌ها زیاد است. کمی بعد دوباره امتحان کنید.")
    return ScError("دریافت از %s ممکن نشد. دوباره امتحان کنید." % BRANDS.get(platform, platform))


# --------------------------------------------------------------------------- #
# Fetch (metadata + media list)
# --------------------------------------------------------------------------- #
def _ytdlp_opts(platform: str, **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 3}
    if platform == "instagram" and COOKIES:
        opts["cookiefile"] = COOKIES
    opts.update(extra)
    return opts


def _ytdlp_fetch_sync(platform: str, url: str) -> MediaInfo:
    opts = _ytdlp_opts(platform, skip_download=True, noplaylist=(platform == "pinterest"))
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise _classify(str(exc), platform) from exc
    if not info:
        raise ScError("اطلاعاتی از این لینک دریافت نشد.")

    entries = [e for e in (info.get("entries") or []) if e] or [info]
    if platform == "pinterest":
        fmts = info.get("formats") or []
        has_video = bool(info.get("url")) or any(f.get("vcodec") not in (None, "none") for f in fmts)
        if not has_video:
            raise ScError(PIN_NO_VIDEO)

    return MediaInfo(
        platform=platform, url=info.get("webpage_url") or url, mode="ytdlp",
        title=(info.get("title") or "") if platform == "pinterest" else "",
        description=(info.get("description") or "").strip(),
        author=info.get("uploader_id") or info.get("channel") or "",
        author_name=info.get("uploader") or "",
        date=iso_date(first(info.get("upload_date"), info.get("timestamp") and
                            time.strftime("%Y%m%d", time.gmtime(info["timestamp"])))),
        likes=to_int(info.get("like_count")), views=to_int(info.get("view_count")),
        comments=to_int(info.get("comment_count")),
        items=[MediaItem("video", url) for _ in entries],
        ytdlp_info=info, fetched_at=time.monotonic(),
    )


def _gdl_fetch_sync(url: str) -> MediaInfo:
    cmd = [sys.executable, "-m", "gallery_dl", "-j"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise ScError("اینستاگرام پاسخ نداد. دوباره امتحان کنید.")
    except FileNotFoundError:
        raise ScError("ماژول gallery-dl نصب نیست.")

    data = []
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else []
    except json.JSONDecodeError:
        log.warning("gallery-dl output is not json: %s", p.stdout[:200])

    items, meta, seen = [], {}, set()
    for msg in data:
        if not (isinstance(msg, list) and len(msg) >= 3 and msg[0] == 3):
            continue
        murl, kw = msg[1], (msg[2] if isinstance(msg[2], dict) else {})
        if not isinstance(murl, str) or not murl.startswith("http"):
            continue
        key = urlparse(murl).path
        if key in seen:
            continue
        seen.add(key)
        ext = str(kw.get("extension") or Path(urlparse(murl).path).suffix.lstrip(".")).lower()
        kind = "video" if ext in VIDEO_EXT else "photo"
        items.append(MediaItem(kind, murl, ext, to_int(kw.get("width")), to_int(kw.get("height"))))
        meta = meta or kw
    if not items:
        raise _classify(p.stderr or "not found", "instagram")

    return MediaInfo(
        platform="instagram", url=first(meta.get("post_url"), url), mode="gdl",
        description=(meta.get("description") or "").strip(),
        author=str(meta.get("username") or ""), author_name=str(meta.get("fullname") or ""),
        date=iso_date(meta.get("date")), likes=to_int(meta.get("likes")),
        story="/stories/" in url, items=items, fetched_at=time.monotonic(),
    )


async def fetch(platform: str, url: str) -> MediaInfo:
    url = await resolve(url)
    loop = asyncio.get_running_loop()

    if platform == "pinterest":
        try:
            return await loop.run_in_executor(EXECUTOR, _ytdlp_fetch_sync, "pinterest", url)
        except ScError:
            raise
        except Exception as exc:
            log.warning("pinterest fetch failed: %s", exc)
            raise ScError("دریافت از پینترست ممکن نشد. دوباره امتحان کنید.") from exc

    # Instagram: reels -> yt-dlp first; posts/carousels/stories -> gallery-dl first
    is_reel = bool(re.search(r"/(?:reels?|tv)/", url))
    methods = [("ytdlp", lambda: _ytdlp_fetch_sync("instagram", url)), ("gdl", lambda: _gdl_fetch_sync(url))]
    if not is_reel:
        methods.reverse()
    errors: list[ScError] = []
    for name, fn in methods:
        try:
            return await loop.run_in_executor(EXECUTOR, fn)
        except ScError as e:
            log.info("instagram %s failed: %s", name, e.user_message)
            errors.append(e)
        except Exception as exc:
            log.warning("instagram %s crashed: %s", name, exc)
            errors.append(ScError("دریافت از اینستاگرام ممکن نشد."))
    raise next((e for e in errors if isinstance(e, AuthRequired)), errors[0])


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
ProgressCb = Callable[[int, int, float, Optional[float], float], None]


def _ytdlp_download_sync(info: MediaInfo, url: str, workdir: Path, cb: ProgressCb) -> list[Path]:
    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                frac = done / total
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.0
            cb(done, total, d.get("speed") or 0.0, d.get("eta"), max(0.0, min(frac, 0.99)))
        elif d.get("status") == "finished":
            size = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            cb(size, size, 0.0, 0, 1.0)

    opts = _ytdlp_opts(
        info.platform,
        format="bv*+ba/b", merge_output_format="mp4",
        outtmpl=str(workdir / "m_%(autonumber)02d.%(ext)s"),
        concurrent_fragment_downloads=8, fragment_retries=5, overwrites=True,
        noprogress=True, progress_hooks=[hook], noplaylist=(info.platform == "pinterest"),
    )

    def run(reuse: bool) -> None:
        with YoutubeDL(opts) as ydl:
            if reuse and info.ytdlp_info:
                ydl.process_ie_result(copy.deepcopy(info.ytdlp_info), download=True)
            else:
                ydl.extract_info(url, download=True)

    reuse = (time.monotonic() - info.fetched_at) < INFO_MAX_AGE
    try:
        run(reuse)
    except Exception as exc:
        if not reuse:
            raise _classify(str(exc), info.platform) from exc
        log.info("reuse of extracted info failed (%s); extracting again", exc)
        for p in workdir.glob("m_*"):
            p.unlink(missing_ok=True)
        try:
            run(False)
        except Exception as exc2:
            raise _classify(str(exc2), info.platform) from exc2

    files = sorted(p for p in workdir.glob("m_*") if p.suffix not in {".part", ".ytdl", ".temp"})
    if not files:
        raise ScError("فایلی دریافت نشد.")
    return files


_CTYPE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
              "video/mp4": "mp4", "video/quicktime": "mov"}


async def _http_download(info: MediaInfo, workdir: Path, cb: ProgressCb) -> list[Path]:
    out: list[Path] = []
    n = len(info.items)
    got_total, t0 = 0, time.monotonic()
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=_UA) as s:
        for i, it in enumerate(info.items):
            async with s.get(it.url) as r:
                if r.status != 200:
                    raise ScError("دریافت فایل از اینستاگرام ممکن نشد (لینک منقضی شده است).")
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                ext = _CTYPE_EXT.get(ctype) or it.ext or ("mp4" if it.kind == "video" else "jpg")
                path = workdir / f"m_{i + 1:02d}.{ext}"
                total = r.content_length or 0
                got = 0
                with path.open("wb") as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        got += len(chunk)
                        got_total += len(chunk)
                        part = (got / total) if total else 0.0
                        elapsed = max(time.monotonic() - t0, 0.001)
                        cb(got_total, 0, got_total / elapsed, None, min((i + part) / n, 0.99))
            out.append(path)
    cb(got_total, got_total, 0.0, 0, 1.0)
    return out


async def download(info: MediaInfo, workdir: Path, cb: ProgressCb) -> list[LocalMedia]:
    loop = asyncio.get_running_loop()
    if info.mode == "ytdlp":
        paths = await loop.run_in_executor(EXECUTOR, _ytdlp_download_sync, info, info.url, workdir, cb)
    else:
        paths = await _http_download(info, workdir, cb)
    return [
        LocalMedia(p, "photo" if p.suffix.lower().lstrip(".") in IMAGE_EXT else "video")
        for p in paths
    ]


# --------------------------------------------------------------------------- #
# Prepare for Telegram (ffprobe/ffmpeg)
# --------------------------------------------------------------------------- #
async def _probe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError:
        return {}


def _streams(probe: dict):
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), {})
    w, h = to_int(v.get("width")), to_int(v.get("height"))
    rot = (v.get("tags") or {}).get("rotate")
    for sd in v.get("side_data_list") or []:
        rot = sd.get("rotation", rot)
    try:
        if rot is not None and abs(int(float(rot))) % 180 == 90:
            w, h = h, w
    except (TypeError, ValueError):
        pass
    dur = first(v.get("duration"), (probe.get("format") or {}).get("duration"))
    try:
        dur = int(float(dur)) if dur else None
    except ValueError:
        dur = None
    return v, a, w, h, dur


async def prepare(files: list[LocalMedia], workdir: Path) -> list[LocalMedia]:
    for i, m in enumerate(files, 1):
        probe = await _probe(m.path)
        v, a, w, h, dur = _streams(probe)

        if m.kind == "video":
            if not v:
                raise ScError("فایل ویدیویی معتبر نیست.")
            ok = (v.get("codec_name") == "h264" and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
                  and a.get("codec_name") in (None, "aac", "mp3"))
            out = workdir / f"v{i}.mp4"
            base = ["-i", str(m.path), "-map", "0:v:0", "-map", "0:a:0?"]
            if ok:
                await run_ffmpeg(*base, "-c", "copy", "-movflags", "+faststart", str(out))
            else:
                await run_ffmpeg(
                    *base, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
                )
            thumb = workdir / f"t{i}.jpg"
            for seek in ("0.3", "0"):
                try:
                    await run_ffmpeg("-ss", seek, "-i", str(out), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                                     "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                                     "-q:v", "5", str(thumb))
                    break
                except RuntimeError:
                    thumb = None
            m.path, m.width, m.height, m.duration, m.thumb = out, w, h, dur, thumb
        else:
            ext = m.path.suffix.lower().lstrip(".")
            too_big = bool(w and h and (w + h > 9500))
            if ext not in ("jpg", "jpeg", "png") or too_big:
                out = workdir / f"p{i}.jpg"
                await run_ffmpeg(
                    "-i", str(m.path), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                    "-vf", "scale='min(4000,iw)':'min(4000,ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "2", str(out),
                )
                m.path = out
                v, a, w, h, dur = _streams(await _probe(out))
            m.width, m.height = w, h
    return files
