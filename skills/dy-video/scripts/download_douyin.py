#!/usr/bin/env python3
"""
Douyin (TikTok China) No-Watermark Video & Audio Downloader
Supports HD 1080P, 720P, and 540P quality selection, as well as Audio-Only extraction.
Uses high-speed official API with Headless Chrome/Edge as resilient fallback.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# Force UTF-8 stream encoding across all operating systems and console codepages
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

APP_USER_AGENT = (
    "com.ss.android.ugc.aweme/110101 (Linux; U; Android 5.1.1; "
    "zh_CN; MI 9; Build/PI; Cronet/TTNetVersion:b4d74d15 "
    "2020-04-23 QuicVersion:0144d358 2020-03-24)"
)

# Common browser executable candidates on Windows / Linux / macOS
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_browser_path() -> str | None:
    """Find available Chrome or Edge binary from PATH or common system locations."""
    for bin_name in ["chrome", "google-chrome", "chromium", "msedge", "edge"]:
        found = shutil.which(bin_name)
        if found and os.path.isfile(found):
            return found

    for candidate in BROWSER_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def extract_url(text: str) -> str:
    """Extract Douyin URL from text snippet (handles sharing templates and raw links)."""
    match = re.search(r"https?://v\.douyin\.com/[a-zA-Z0-9_\-]+/?", text)
    if match:
        return match.group(0)
    match_long = re.search(r"https?://(?:www\.)?douyin\.com/video/(\d+)", text)
    if match_long:
        return match_long.group(0)
    match_ies = re.search(r"https?://(?:www\.)?iesdouyin\.com/share/video/(\d+)/?", text)
    if match_ies:
        return f"https://www.douyin.com/video/{match_ies.group(1)}"
    if text.strip().startswith("http://") or text.strip().startswith("https://"):
        return text.strip()
    raise ValueError(f"未在输入内容中找到有效的抖音视频链接: {text}")


def resolve_redirect(url: str) -> str:
    """Pre-resolve 302/301 redirects to get canonical douyin.com/video/<id> URL."""
    ctx = ssl._create_unverified_context()
    headers = {"User-Agent": MOBILE_USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            final_url = resp.geturl()
            m = re.search(r'/(?:video|share/video)/(\d+)', final_url)
            if m:
                return f"https://www.douyin.com/video/{m.group(1)}"
            return final_url
    except Exception:
        return url


def extract_aweme_id(url: str) -> str:
    """Extract numeric aweme_id from URL."""
    m = re.search(r'/(?:video|share/video)/(\d+)', url)
    return m.group(1) if m else ""


def fetch_metadata_via_api(aweme_id: str) -> dict | None:
    """Fast-path: Fetch video metadata directly via official Aweme Feed APIs without browser."""
    if not aweme_id:
        return None

    api_hosts = ["aweme.snssdk.com", "api.amemv.com", "aweme-hl.snssdk.com"]
    ctx = ssl._create_unverified_context()

    for host in api_hosts:
        api = f"https://{host}/aweme/v1/feed/?aweme_id={aweme_id}"
        req = urllib.request.Request(api, headers={"User-Agent": APP_USER_AGENT})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=6) as r:
                body = r.read().decode("utf-8", errors="ignore")
                data = json.loads(body)
                aweme_list = data.get("aweme_list", [])
                for item in aweme_list:
                    if str(item.get("aweme_id")) == str(aweme_id) or len(aweme_list) == 1:
                        title = item.get("desc", "").strip() or f"douyin_{aweme_id}"
                        safe_title = re.sub(r'[\\/*?:"<>|#\n\r\t]', "_", title).strip("_ ")
                        if len(safe_title) > 60:
                            safe_title = safe_title[:60]

                        video = item.get("video", {})
                        video_id = video.get("vid") or ""

                        # Base play addresses
                        play_addr_list = video.get("play_addr", {}).get("url_list", [])
                        default_stream_url = play_addr_list[0] if play_addr_list else ""

                        # Bitrates mapping for quality selection
                        bitrates = video.get("bit_rate", []) or []
                        stream_map = {}
                        for br in bitrates:
                            gear = str(br.get("gear_name", "")).lower()
                            urls = br.get("play_addr", {}).get("url_list", [])
                            if urls:
                                stream_map[gear] = urls[0]

                        author = item.get("author", {}).get("nickname", "")
                        music = item.get("music", {})
                        music_urls = music.get("play_url", {}).get("url_list", [])
                        music_url = music_urls[0] if music_urls else ""

                        return {
                            "title": title,
                            "safe_title": safe_title or "douyin_video",
                            "author": author,
                            "aweme_id": str(item.get("aweme_id", aweme_id)),
                            "video_id": video_id,
                            "default_stream_url": default_stream_url,
                            "stream_map": stream_map,
                            "music_url": music_url,
                        }
        except Exception:
            continue
    return None


def extract_video_id_from_stream_header(stream_url: str) -> str:
    """Fetch first 16KB of stream to extract native video_id from MP4 metadata."""
    ctx = ssl._create_unverified_context()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Range": "bytes=0-16384",
    }
    req = urllib.request.Request(stream_url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=6) as resp:
            header_bytes = resp.read()
            m = re.search(rb'vid:(v[a-zA-Z0-9_]+)', header_bytes)
            if m:
                return m.group(1).decode("ascii")
    except Exception:
        pass
    return ""


def dump_douyin_data(url: str, browser_path: str, timeout_sec: int = 15) -> tuple[str, list[str]]:
    """Render Douyin webpage DOM and capture network media requests via headless browser fallback."""
    tmp_user_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        netlog_path = tf.name

    cmd = [
        browser_path,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={tmp_user_dir}",
        f"--log-net-log={netlog_path}",
        f"--user-agent={DEFAULT_USER_AGENT}",
        "--dump-dom",
        url,
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout_sec,
        )
        dom = res.stdout

        vod_urls: list[str] = []
        if os.path.isfile(netlog_path):
            with open(netlog_path, "r", encoding="utf-8", errors="ignore") as f:
                netlog_content = f.read()
            raw_vods = re.findall(r'https://[a-zA-Z0-9_\-\.]*douyinvod\.com/[^\s"\'<>\\]+', netlog_content)
            vod_urls = list({re.sub(r'[:;,]+$', '', u) for u in raw_vods})

        return dom, vod_urls
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"浏览器加载网页超时 ({timeout_sec}s)")
    except Exception as e:
        raise RuntimeError(f"调用无头浏览器失败: {e}")
    finally:
        try:
            if os.path.isfile(netlog_path):
                os.unlink(netlog_path)
            shutil.rmtree(tmp_user_dir, ignore_errors=True)
        except Exception:
            pass


def parse_video_metadata(dom_html: str, origin_url: str, vod_urls: list[str]) -> dict:
    """Extract video title, author, aweme_id, and video_id from DOM and captured media streams."""
    aweme_id_match = re.search(r'/video/(\d+)', origin_url) or re.search(r'data-e2e-aweme-id="(\d+)"', dom_html) or re.search(r'__vid=(\d+)', dom_html)
    aweme_id = aweme_id_match.group(1) if aweme_id_match else ""

    title = ""
    title_match = re.search(r'<title>(.*?)</title>', dom_html)
    if title_match:
        raw_title = title_match.group(1).strip()
        title = re.sub(r"-\s*抖音$", "", raw_title).strip()
    if not title:
        title = f"douyin_{aweme_id}" if aweme_id else "douyin_video"

    safe_title = re.sub(r'[\\/*?:"<>|#\n\r\t]', "_", title).strip("_ ")
    if len(safe_title) > 60:
        safe_title = safe_title[:60]

    video_id = ""
    vid_match = re.search(r'video_id=(v[a-zA-Z0-9_]+)', dom_html)
    if vid_match:
        video_id = vid_match.group(1)
    else:
        sources = re.findall(r'<source[^>]*src="([^"]+)"', dom_html)
        for s in sources:
            m = re.search(r'video_id=(v[a-zA-Z0-9_]+)', s)
            if m:
                video_id = m.group(1)
                break

    if not video_id and vod_urls:
        for u in vod_urls:
            if "media-video" in u or "video/tos" in u:
                vid = extract_video_id_from_stream_header(u)
                if vid:
                    video_id = vid
                    break

    direct_sources = re.findall(r'<source[^>]*src="([^"]+)"', dom_html)
    default_stream_url = ""
    for s in direct_sources:
        s_clean = html.unescape(s)
        if "douyinvod.com" in s_clean:
            default_stream_url = s_clean
            break

    return {
        "title": title,
        "safe_title": safe_title or "douyin_video",
        "author": "",
        "aweme_id": aweme_id,
        "video_id": video_id,
        "default_stream_url": default_stream_url,
        "stream_map": {},
        "music_url": "",
    }


def resolve_stream_url(
    video_id: str,
    quality: str = "1080p",
    stream_map: dict | None = None,
    fallback_url: str = "",
) -> tuple[str, int]:
    """
    Resolve no-watermark direct stream URL and file size for given quality ratio.
    Ratios: '1080p', '720p', '540p'
    """
    ctx = ssl._create_unverified_context()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://www.douyin.com/",
    }

    # 1. If video_id exists, request official SNSSDK origin stream
    if video_id:
        ratio_param = f"&ratio={quality}" if quality in ["1080p", "720p", "540p"] else ""
        play_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={video_id}{ratio_param}&line=0"
        try:
            req = urllib.request.Request(play_url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                final_url = resp.geturl()
                content_length = int(resp.headers.get("content-length", 0))
                if content_length > 0 and resp.status == 200:
                    return final_url, content_length
        except Exception:
            pass

    # 2. Match from stream_map if available
    if stream_map:
        gear_candidates = {
            "1080p": ["1080p", "higher", "normal"],
            "720p": ["720p", "higher", "normal", "lowest"],
            "540p": ["540p", "normal", "lowest"],
        }
        for candidate in gear_candidates.get(quality, ["normal"]):
            for gear_key, stream_url in stream_map.items():
                if candidate in gear_key and stream_url:
                    try:
                        req = urllib.request.Request(stream_url, headers=headers)
                        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                            cl = int(resp.headers.get("content-length", 0))
                            return stream_url, cl
                    except Exception:
                        pass

    # 3. Use default fallback
    if fallback_url:
        try:
            req = urllib.request.Request(fallback_url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                content_length = int(resp.headers.get("content-length", 0))
                return fallback_url, content_length
        except Exception:
            return fallback_url, 0

    raise RuntimeError("未能解析出可用的视频播放流，可能是该作品已被删除或设为私密。")


def download_file(url: str, output_path: Path, expected_size: int = 0) -> None:
    """Download video stream with progress reporting."""
    ctx = ssl._create_unverified_context()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://www.douyin.com/",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp, open(output_path, "wb") as f:
        total = expected_size or int(resp.headers.get("content-length", 0))
        downloaded = 0
        chunk_size = 128 * 1024

        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                percent = (downloaded / total) * 100
                mb_downloaded = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024)
                sys.stdout.write(f"\r下载进度: {percent:5.1f}% ({mb_downloaded:5.2f}MB / {mb_total:5.2f}MB)")
                sys.stdout.flush()

    sys.stdout.write("\n")


def ensure_ffmpeg() -> str:
    """Find system ffmpeg or automatically download portable standalone binary to ~/.dy-video/bin/."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    cache_dir = Path.home() / ".dy-video" / "bin"
    cache_dir.mkdir(parents=True, exist_ok=True)
    bin_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    local_bin = cache_dir / bin_name

    if local_bin.is_file():
        if sys.platform != "win32" and not os.access(local_bin, os.X_OK):
            local_bin.chmod(local_bin.stat().st_mode | 0o755)
        return str(local_bin)

    # Automatically download portable standalone ffmpeg
    print("[提示] 本地未检测到 FFmpeg（用于抽取/转码音频），正在自动下载免安装便携版...", file=sys.stderr)

    arch = platform.machine().lower()
    is_arm = "arm" in arch or "aarch" in arch
    if sys.platform == "win32":
        target_asset = "ffmpeg-win64-v4.2.2.exe"
    elif sys.platform == "darwin":
        target_asset = "ffmpeg-macos-aarch64-v7.1" if is_arm else "ffmpeg-macos-x86_64-v7.1"
    else:
        target_asset = "ffmpeg-linux-aarch64-v7.0.2" if is_arm else "ffmpeg-linux-x86_64-v7.0.2"

    urls = [
        f"https://raw.githubusercontent.com/imageio/imageio-binaries/master/ffmpeg/{target_asset}",
        f"https://ghproxy.net/https://raw.githubusercontent.com/imageio/imageio-binaries/master/ffmpeg/{target_asset}",
    ]

    ctx = ssl._create_unverified_context()
    headers = {"User-Agent": DEFAULT_USER_AGENT}

    downloaded = False
    temp_file = local_bin.with_suffix(".tmp")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp, open(temp_file, "wb") as f:
                total_size = int(resp.headers.get("content-length", 0))
                downloaded_size = 0
                chunk_size = 256 * 1024
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0:
                        pct = (downloaded_size / total_size) * 100
                        mb_done = downloaded_size / (1024 * 1024)
                        mb_tot = total_size / (1024 * 1024)
                        sys.stderr.write(f"\rFFmpeg 下载进度: {pct:5.1f}% ({mb_done:5.2f}MB / {mb_tot:5.2f}MB)")
                        sys.stderr.flush()
                sys.stderr.write("\n")
            if temp_file.is_file() and temp_file.stat().st_size > 1024 * 1024:
                temp_file.replace(local_bin)
                if sys.platform != "win32":
                    local_bin.chmod(local_bin.stat().st_mode | 0o755)
                downloaded = True
                print(f"[提示] FFmpeg 已自动就绪: {local_bin}", file=sys.stderr)
                break
        except Exception:
            if temp_file.is_file():
                temp_file.unlink(missing_ok=True)
            continue

    if downloaded and local_bin.is_file():
        return str(local_bin)

    raise RuntimeError(
        "未能自动下载 FFmpeg。请检查网络连接，或手动安装 ffmpeg 并加入系统 PATH。"
    )


def extract_audio_file(
    stream_url: str,
    output_path: Path,
    audio_format: str = "mp3",
    music_url: str = "",
) -> None:
    """Extract audio track directly from media stream or music stream."""
    # If music_url exists and format is mp3, download directly without transcoding
    if music_url and audio_format == "mp3":
        try:
            download_file(music_url, output_path)
            return
        except Exception:
            pass

    ffmpeg_bin = ensure_ffmpeg()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers_arg = f"User-Agent: {DEFAULT_USER_AGENT}\r\nReferer: https://www.douyin.com/\r\n"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-headers",
        headers_arg,
        "-i",
        stream_url,
        "-vn",
    ]
    if audio_format == "m4a":
        cmd += ["-c:a", "copy", str(output_path)]
    else:
        cmd += ["-c:a", "libmp3lame", "-q:a", "0", str(output_path)]

    res = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg 提取音频失败: {res.stderr}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="抖音无水印高清视频与音频下载工具 (支持 1080P/720P/540P 画质选择及音频提取)"
    )
    parser.add_argument("url_or_text", help="抖音视频分享文本、短链接或网页长链接")
    parser.add_argument(
        "-q",
        "--quality",
        choices=["1080p", "720p", "540p"],
        default="1080p",
        help="目标清晰度 (默认: 1080p 超高清；若视频源无 1080p 将自动匹配最高画质)",
    )
    parser.add_argument(
        "-a",
        "--audio",
        "--audio-only",
        action="store_true",
        help="仅提取并下载音频（支持导出为 .mp3 或 .m4a 原生无损）",
    )
    parser.add_argument(
        "--audio-format",
        choices=["mp3", "m4a"],
        default="mp3",
        help="音频导出格式（默认: mp3，可选: m4a 原生无损）",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="保存的目标路径（文件路径或目录路径，默认保存到系统的 Downloads 目录）",
    )
    parser.add_argument(
        "--info-only",
        action="store_true",
        help="仅获取并打印视频元数据与无水印直链，不执行下载",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果信息",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 1. Extract valid Douyin URL & resolve redirects
    try:
        raw_url = extract_url(args.url_or_text)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    douyin_url = resolve_redirect(raw_url)
    aweme_id = extract_aweme_id(douyin_url)

    if not args.json:
        print(f"[1/3] 解析视频链接: {douyin_url}")

    # 2. Fast-path: official Feed API
    meta = None
    if aweme_id:
        meta = fetch_metadata_via_api(aweme_id)

    # 3. Fallback: Headless browser
    if not meta or (not meta.get("default_stream_url") and not meta.get("video_id")):
        browser_path = find_browser_path()
        if not browser_path:
            print("错误: 本地未找到 Chrome 或 Edge 浏览器可执行文件，无法渲染提取视频数据。", file=sys.stderr)
            sys.exit(1)
        try:
            dom_html, vod_urls = dump_douyin_data(douyin_url, browser_path)
            meta = parse_video_metadata(dom_html, douyin_url, vod_urls)
        except Exception as e:
            print(f"解析失败: {e}", file=sys.stderr)
            sys.exit(1)

    if not meta or (not meta.get("video_id") and not meta.get("default_stream_url")):
        print("错误: 无法从页面中提取有效视频源，可能该视频已被删除、设为私密或需要登录才能查看。", file=sys.stderr)
        sys.exit(1)

    author_str = f" @{meta['author']}" if meta.get("author") else ""
    if not args.json:
        print(f"[2/3] 获取作品信息: 《{meta['title']}》{author_str} (ID: {meta['aweme_id'] or '未知'})")

    # 4. Resolve Direct Stream URL for desired quality
    try:
        direct_url, size_bytes = resolve_stream_url(
            meta["video_id"],
            quality=args.quality,
            stream_map=meta.get("stream_map"),
            fallback_url=meta.get("default_stream_url", ""),
        )
    except Exception as e:
        print(f"获取视频流失败: {e}", file=sys.stderr)
        sys.exit(1)

    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0

    result = {
        "title": meta["title"],
        "author": meta.get("author", ""),
        "aweme_id": meta["aweme_id"],
        "video_id": meta["video_id"],
        "quality": args.quality,
        "size_mb": round(size_mb, 2),
        "stream_url": direct_url,
    }

    # Info-only mode
    if args.info_only:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("\n=== 视频解析结果 ===")
            print(f"作品标题: {meta['title']}")
            if meta.get("author"):
                print(f"作者昵称: {meta['author']}")
            print(f"作品 ID:   {meta['aweme_id']}")
            print(f"目标画质: {args.quality}")
            print(f"预估大小: {size_mb:.2f} MB")
            print(f"无水印直链: {direct_url}")
        return

    # 5. Determine Destination Path
    if args.audio:
        ext = f".{args.audio_format}"
        default_filename = f"{meta['safe_title']}_音频{ext}"
    else:
        ext = ".mp4"
        default_filename = f"{meta['safe_title']}_{args.quality}_无水印{ext}"

    if args.output:
        out = Path(args.output)
        if out.is_dir() or args.output.endswith(("/", "\\")):
            dest_path = out / default_filename
        else:
            dest_path = out
    else:
        downloads_dir = Path.home() / "Downloads"
        dest_path = downloads_dir / default_filename

    # 6. Download or Extract
    if args.audio:
        if not args.json:
            print(f"[3/3] 开始提取并导出 {args.audio_format.upper()} 音频...")
            print(f"      保存路径: {dest_path}")
        try:
            extract_audio_file(
                direct_url,
                dest_path,
                audio_format=args.audio_format,
                music_url=meta.get("music_url", ""),
            )
            actual_size_mb = dest_path.stat().st_size / (1024 * 1024)
            result["saved_path"] = str(dest_path.resolve())
            result["audio_format"] = args.audio_format
            result["audio_size_mb"] = round(actual_size_mb, 2)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"[成功] 音频已下载完成！文件大小: {actual_size_mb:.2f} MB，路径: {dest_path}")
        except Exception as e:
            print(f"\n音频提取失败: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.json:
            print(f"[3/3] 开始下载 {args.quality} 无水印视频 (预估 {size_mb:.2f} MB)...")
            print(f"      保存路径: {dest_path}")
        try:
            download_file(direct_url, dest_path, expected_size=size_bytes)
            result["saved_path"] = str(dest_path.resolve())
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"[成功] 视频已下载完成！文件路径: {dest_path}")
        except Exception as e:
            print(f"\n下载失败: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
