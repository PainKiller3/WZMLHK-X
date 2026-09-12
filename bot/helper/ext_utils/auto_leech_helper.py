import re
from urllib.parse import unquote

MEDIA_EXTENSIONS = (
    # Video
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".3gp",
    ".ts",
    ".vob",
    ".mpg",
    ".mpeg",
    ".m2ts",
    # Audio
    ".mp3",
    ".flac",
    ".m4a",
    ".wav",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".alac",
)

URL_REGEX = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def is_direct_media_url(url: str) -> bool:
    """Checks if a URL points strictly to a video or audio file by extension (including S3/R2 presigned filenames)."""
    if not url or url.startswith("magnet:"):
        return False

    url_lower = url.lower()

    # 1. Check clean URL path (ignoring query strings/fragments)
    clean_url = url.split("?")[0].split("#")[0].lower()
    if clean_url.endswith(MEDIA_EXTENSIONS):
        return True

    # 2. Check query parameters & response-content-disposition filenames (e.g. S3/R2 presigned URLs)
    unquoted = unquote(url_lower)
    for ext in MEDIA_EXTENSIONS:
        if "filename=" in unquoted and ext in unquoted:
            filename_part = unquoted.split("filename=")[-1].split("&")[0].strip("\"' ")
            if filename_part.endswith(ext):
                return True
        elif (
            unquoted.endswith(ext)
            or f'{ext}"' in unquoted
            or f"{ext}'" in unquoted
            or f"{ext}&" in unquoted
        ):
            return True

    return False


def extract_media_links(text: str, max_links: int = 10) -> list[str]:
    """Extracts video and audio media links from text up to max_links."""
    if not text:
        return []

    found_urls = URL_REGEX.findall(text)
    media_links = []

    for url in found_urls:
        url = url.strip(".,;:!'\")}]")
        if is_direct_media_url(url) and url not in media_links:
            media_links.append(url)

    if max_links > 0:
        return media_links[:max_links]
    return media_links
