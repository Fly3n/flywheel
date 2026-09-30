#!/usr/bin/env python3
"""
WeChat Official Account Article Fetcher Script
Fetches WeChat MP articles (mp.weixin.qq.com/s/...) cleanly by avoiding anti-crawler protections.
"""

import os
import sys
import re
import html
import json
import hashlib
import argparse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

ARTICLE_FIELDS = ("title", "author", "publish_time", "url", "content")
DEFAULT_CACHE_DIR = Path.home() / ".wx-fetch" / "cache" / "articles"


def article_cache_path(url, cache_dir=None):
    root = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return root / f"{key}.json"


def load_cached_article(url, cache_dir=None):
    path = article_cache_path(url, cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None, path
    if data.get("url") != url or not data.get("content"):
        return None, path
    return data, path


def cache_article(data, cache_dir=None):
    path = article_cache_path(data["url"], cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    temp_path = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as temp:
            json.dump(payload, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
            temp_path = Path(temp.name)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return path


def format_article(data, as_json=False):
    article = {field: data.get(field, "") for field in ARTICLE_FIELDS}
    if as_json:
        return json.dumps(article, ensure_ascii=False, indent=2)
    time_line = f"Publish Time: {article['publish_time']}\n" if article.get("publish_time") else ""
    return (
        f"Title: {article['title']}\n"
        f"Author: {article['author']}\n"
        f"{time_line}"
        f"URL: {article['url']}\n\n{article['content']}"
    )


def emit_article(data, output_file=None, as_json=False):
    formatted_output = format_article(data, as_json)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(formatted_output)
        print(f"Article saved successfully to: {output_file}")
    else:
        print(formatted_output)


def fetch_wechat_article(
    url, output_file=None, as_json=False, cache_dir=None, refresh=False
):
    if not refresh:
        cached, cache_path = load_cached_article(url, cache_dir)
        if cached:
            print(f"Article cache hit: {cache_path}", file=sys.stderr)
            emit_article(cached, output_file, as_json)
            return True

    # 1. Unset proxy env vars to avoid SSL handshake EOF errors with local proxies
    for key in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']:
        os.environ.pop(key, None)

    # 2. Use WeChat iOS App User-Agent to bypass standard anti-spider verification
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.38(0x1800262c) NetType/WIFI Language/zh_CN"
    }

    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)

    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

            if "环境异常" in content:
                error_msg = "Error: Encountered WeChat anti-spider verification page."
                print(error_msg, file=sys.stderr)
                return False

            # Extract Title
            title_match = re.search(r'id="activity-name"[^>]*>\s*(.*?)\s*</h[12]>', content, re.DOTALL)
            if not title_match:
                title_match = re.search(r'var msg_title = ["\'](.*?)["\']', content)
                if not title_match:
                    title_match = re.search(r'<meta property="og:title" content="(.*?)"', content)

            title = html.unescape(title_match.group(1)).strip() if title_match else "Unknown Title"

            # Extract Author / Nickname
            author_match = re.search(r'id="js_name"[^>]*>\s*(.*?)\s*</a>', content, re.DOTALL)
            if not author_match:
                author_match = re.search(r'var nickname = ["\'](.*?)["\']', content)
            if not author_match:
                author_match = re.search(r'<meta property="og:article:author" content="(.*?)"', content)

            author = html.unescape(author_match.group(1)).strip() if author_match else "Unknown Author"

            # Extract Publish Time timestamp if present
            ct_match = re.search(r'var ct = ["\']?(\d+)["\']?;', content)
            publish_time = ""
            if ct_match:
                ts_str = ct_match.group(1)
                try:
                    dt = datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
                    publish_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    publish_time = ts_str

            # Extract Main Body Text
            content_match = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*<script', content, re.DOTALL)
            if not content_match:
                content_match = re.search(r'id="js_content"[^>]*>(.*?)</div>', content, re.DOTALL)

            text = ""
            if content_match:
                raw_html = content_match.group(1)
                # Remove scripts and styles
                raw_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', raw_html, flags=re.DOTALL)
                # Replace line breaks and paragraph tags
                raw_html = re.sub(r'<(br|p|div|section)[^>]*>', '\n', raw_html)
                # Strip remaining HTML tags
                text = re.sub(r'<[^>]+>', '', raw_html)
                text = html.unescape(text)
                # Clean up empty lines
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                text = '\n'.join(lines)

            if not text:
                print("Error: WeChat article body is empty.", file=sys.stderr)
                return False

            result_data = {
                "title": title,
                "author": author,
                "publish_time": publish_time,
                "url": url,
                "content": text
            }

            try:
                cache_path = cache_article(result_data, cache_dir)
                print(f"Article cached: {cache_path}", file=sys.stderr)
            except OSError as error:
                print(f"Warning: Failed to cache article: {error}", file=sys.stderr)

            emit_article(result_data, output_file, as_json)

            return True
    except Exception as e:
        print(f"Error fetching article: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Fetch WeChat Official Account Article Content")
    parser.add_argument("url", help="WeChat Article URL (e.g. https://mp.weixin.qq.com/s/...)")
    parser.add_argument("-o", "--output", help="Save output to file path")
    parser.add_argument("--json", action="store_true", help="Output result as JSON format")
    parser.add_argument(
        "--cache-dir", help="Override the default ~/.wx-fetch/cache/articles directory"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore cached content and fetch again"
    )

    args = parser.parse_args()
    success = fetch_wechat_article(
        args.url,
        output_file=args.output,
        as_json=args.json,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
    )
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
