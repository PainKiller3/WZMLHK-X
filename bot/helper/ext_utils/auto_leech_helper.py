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

KNOWN_MEDIA_DOMAINS = ()

URL_REGEX = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def is_direct_media_url(url: str, custom_patterns: tuple = ()) -> bool:
    """Checks if a URL points strictly to a video or audio file by extension, presigned S3/R2 URL, or user custom patterns."""
    if not url or url.startswith("magnet:"):
        return False

    url_lower = url.lower()

    # 1. Check built-in media domains
    if any(domain in url_lower for domain in KNOWN_MEDIA_DOMAINS):
        return True

    # 2. Check clean URL path (ignoring query strings/fragments)
    clean_url = url.split("?")[0].split("#")[0].lower()
    if clean_url.endswith(MEDIA_EXTENSIONS):
        return True

    # 3. Check query parameters & response-content-disposition filenames (e.g. S3/R2 presigned URLs)
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

    # 4. Check user custom patterns (extensions or domain keywords)
    if custom_patterns:
        for pat in custom_patterns:
            pat = pat.lower().strip()
            if not pat:
                continue
            if "." in pat and not pat.startswith("."):
                # Keyword or domain pattern (e.g. video-downloads.googleusercontent.com)
                if pat in url_lower:
                    return True
            else:
                # Extension pattern (e.g. zip, .zip, rar, .rar)
                ext = pat if pat.startswith(".") else f".{pat}"
                if clean_url.endswith(ext):
                    return True
                if "filename=" in unquoted and ext in unquoted:
                    filename_part = (
                        unquoted.split("filename=")[-1].split("&")[0].strip("\"' ")
                    )
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


def extract_media_links(
    text: str, max_links: int = 10, custom_patterns: tuple = ()
) -> list[str]:
    """Extracts video and audio media links from text up to max_links, respecting custom_patterns."""
    if not text:
        return []

    found_urls = URL_REGEX.findall(text)
    media_links = []

    for url in found_urls:
        url = url.strip(".,;:!'\")}]")
        if is_direct_media_url(url, custom_patterns) and url not in media_links:
            media_links.append(url)

    if max_links > 0:
        return media_links[:max_links]
    return media_links
