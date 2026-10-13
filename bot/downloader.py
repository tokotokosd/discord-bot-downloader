"""Video downloader using yt-dlp with size-aware format selection."""
import asyncio
import logging
import os
import subprocess
import tempfile
from html import unescape
from pathlib import Path
from typing import Optional, Tuple
import yt_dlp
import re
import requests

try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False

logger = logging.getLogger(__name__)


class VideoDownloader:
    """Downloads videos and images from supported social platforms using yt-dlp."""
    
    def __init__(self, max_file_size_bytes: int, cookies_file: str = None):
        self.max_file_size_bytes = max_file_size_bytes
        self.cookies_file = cookies_file
        self.temp_dir = Path(tempfile.gettempdir()) / "discord_meme_bot"
        self.temp_dir.mkdir(exist_ok=True)
    
    async def download(self, url: str) -> Optional[Tuple[Path, str]]:
        """
        Download video or image from URL.
        
        Returns:
            Tuple of (file_path, title) if successful, None if failed
        """
        # Run download in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._download_sync, url)
    
    def _download_sync(self, url: str) -> Optional[Tuple[Path, str]]:
        """Synchronous download implementation for videos and images."""
        # Clean URL - remove tracking parameters for Instagram
        if 'instagram.com' in url:
            # Remove query parameters like ?igsh=... 
            original_url = url
            url = url.split('?')[0]
            logger.debug(f"Cleaned Instagram URL: {original_url} -> {url}")
            
            # For Instagram, try instaloader first if available
            if INSTALOADER_AVAILABLE:
                logger.info(f"Trying Instagram download with instaloader for {url}")
                result = self._try_download_instagram(url)
                if result:
                    return result
                logger.info(f"Instaloader failed, falling back to yt-dlp for {url}")
        
        output_template = str(self.temp_dir / "%(id)s.%(ext)s")
        
        # First, try to download as video
        result = self._try_download_video(url, output_template)
        if result:
            return result
        
        # If video download failed, try as image (for Instagram photo posts)
        if 'instagram.com' in url:
            logger.info(f"Video download failed, trying HTML scrape for {url}")
            return self._try_download_image(url, output_template)
        
        return None
    
    def _try_download_instagram(self, url: str) -> Optional[Tuple[Path, str]]:
        """Try to download Instagram content using instaloader.
        
        This method handles both photos and videos from Instagram posts and reels.
        """
        if not INSTALOADER_AVAILABLE:
            return None
        
        try:
            # Extract shortcode from URL
            # URLs can be like: https://www.instagram.com/p/SHORTCODE/ or /reel/SHORTCODE/
            match = re.search(r'/(p|reel|reels)/([A-Za-z0-9_-]+)', url)
            if not match:
                logger.debug(f"Could not extract shortcode from {url}")
                return None
            
            shortcode = match.group(2)
            logger.debug(f"Extracted shortcode: {shortcode}")
            
            # Create instaloader instance
            L = instaloader.Instaloader(
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                dirname_pattern=str(self.temp_dir),
                filename_pattern="{shortcode}"
            )
            
            # Disable logging from instaloader
            instaloader_logger = logging.getLogger('instaloader')
            instaloader_logger.setLevel(logging.ERROR)
            
            # Get post by shortcode
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            
            # Check if it's a video or image
            if post.is_video:
                # Download video
                L.download_post(post, target=str(self.temp_dir))
                
                # Find the downloaded video file
                video_files = list(self.temp_dir.glob(f"{shortcode}.mp4"))
                if video_files:
                    filepath = video_files[0]
                    
                    # Check size
                    if filepath.stat().st_size > self.max_file_size_bytes:
                        logger.warning("Instagram video exceeds size limit; trying compression")
                        compressed = self._compress_video(filepath)
                        if compressed:
                            title = post.caption[:100] if post.caption else "Instagram Video"
                            return (compressed, title)
                        filepath.unlink(missing_ok=True)
                        return None
                    
                    title = post.caption[:100] if post.caption else "Instagram Video"
                    return (filepath, title)
            else:
                # Download image
                L.download_post(post, target=str(self.temp_dir))
                
                # Find the downloaded image file
                image_files = list(self.temp_dir.glob(f"{shortcode}.*"))
                # Filter to only image extensions
                image_files = [f for f in image_files if f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']]
                
                if image_files:
                    filepath = image_files[0]
                    
                    # Check size
                    if filepath.stat().st_size > self.max_file_size_bytes:
                        logger.warning(f"Instagram image exceeds size limit")
                        filepath.unlink(missing_ok=True)
                        return None
                    
                    title = post.caption[:100] if post.caption else "Instagram Photo"
                    return (filepath, title)
            
            logger.debug(f"No media files found after instaloader download")
            return None
            
        except instaloader.exceptions.InstaloaderException as e:
            logger.debug(f"Instaloader error for {url}: {str(e)}")
            return None
        except Exception as e:
            logger.debug(f"Unexpected error in instaloader: {type(e).__name__}: {str(e)}")
            return None
    
    def _try_download_image(self, url: str, output_template: str) -> Optional[Tuple[Path, str]]:
        """Try to download content as image (for Instagram photo posts) via HTML scrape.

        Strategy: fetch HTML, extract og:image (or display_url), then download the image.
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except Exception as e:
            logger.debug(f"HTTP GET failed for {url}: {type(e).__name__}: {str(e)}")
            return None

        if resp.status_code != 200 or not resp.text:
            logger.debug(f"Unexpected status/code for {url}: {resp.status_code}")
            return None

        html = resp.text
        image_url = None

        # Try og:image first
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if m:
            image_url = m.group(1)

        # Fallback: display_url in JSON
        if not image_url:
            m = re.search(r'"display_url"\s*:\s*"([^"\\]+(?:\\/[^"\\]+)*)"', html)
            if m:
                image_url = m.group(1)

        if not image_url:
            logger.debug(f"No image URL found in HTML for {url}")
            return None

        # Unescape common sequences
        image_url = image_url.replace('\\/','/').replace('\\u0026','&')
        # og:image content is HTML-escaped (&amp;); the signed CDN URL returns 403 unless decoded
        image_url = unescape(image_url)

        # Download the image
        try:
            with requests.get(image_url, headers=headers, stream=True, timeout=20) as r:
                if r.status_code != 200:
                    logger.debug(f"Image GET failed {image_url}: {r.status_code}")
                    return None

                content_type = r.headers.get('Content-Type', '')
                if not content_type.startswith('image/'):
                    logger.debug(f"Not an image content-type: {content_type}")
                    return None

                # Determine extension
                ext = 'jpg'
                if 'png' in content_type:
                    ext = 'png'
                elif 'webp' in content_type:
                    ext = 'webp'
                elif 'jpeg' in content_type or 'jpg' in content_type:
                    ext = 'jpg'

                file_path = self.temp_dir / f"inst_{hash(url)}.{ext}"

                # Stream to disk with size cap
                total = 0
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.max_file_size_bytes:
                            logger.warning(f"Image exceeds size limit: {total} bytes")
                            try:
                                file_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return None
                        f.write(chunk)

                title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                title = title_match.group(1) if title_match else 'Image'
                return (file_path, title)
        except Exception as e:
            logger.debug(f"Failed to download image content: {type(e).__name__}: {str(e)}")
            return None
    
    def _try_download_video(self, url: str, output_template: str) -> Optional[Tuple[Path, str]]:
        """Try to download content as video."""

        is_reddit = 'reddit.com' in url or 'redd.it' in url
        is_facebook = 'facebook.com' in url or 'fb.watch' in url

        # Reddit commonly uses separate video/audio streams. Use a Reddit-specific
        # selector first, then generic fallbacks.
        if is_reddit:
            format_strategies = [
                "bestvideo*+bestaudio/bestvideo+bestaudio/best",
                "best",
            ]
        else:
            # Try multiple format strategies, from smallest to largest
            # This helps us stay under the file size limit
            format_strategies = [
                # Strategy 1: Smallest reasonable quality
                "worst[ext=mp4][filesize<{}]/worst[ext=mp4]/worst".format(
                    self.max_file_size_bytes
                ),
                # Strategy 2: Medium quality with size limit
                "best[height<=480][ext=mp4][filesize<{}]/best[height<=480][ext=mp4]".format(
                    self.max_file_size_bytes
                ),
                # Strategy 3: Good quality with size limit
                "best[height<=720][ext=mp4][filesize<{}]/best[height<=720][ext=mp4]".format(
                    self.max_file_size_bytes
                ),
                # Strategy 4: Best available that fits
                "best[filesize<{}]/best[ext=mp4]/best".format(
                    self.max_file_size_bytes
                ),
            ]

        for format_selector in format_strategies:
            ydl_opts = {
                'format': format_selector,
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'socket_timeout': 30,
                'retries': 3,
                'no_playlist': True,
                # Prefer mp4 container
                'merge_output_format': 'mp4',
                # Better user agent for social pages that block default clients.
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Sec-Fetch-Mode': 'navigate',
                },
            }

            # Keep strict max_filesize for non-Reddit. For Reddit, many formats don't
            # expose deterministic filesize in selectors, so enforce size after download.
            if not is_reddit:
                ydl_opts['max_filesize'] = self.max_file_size_bytes

            if is_facebook:
                ydl_opts['http_headers']['Referer'] = 'https://www.facebook.com/'

            # Add cookies if available (needed for some Instagram/Facebook content)
            if self.cookies_file and os.path.isfile(self.cookies_file):
                ydl_opts['cookiefile'] = self.cookies_file
                logger.debug(f"Using cookies from {self.cookies_file}")

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if not info:
                        continue

                    filepath: Optional[Path] = None

                    # Prefer requested_downloads path when available.
                    if 'requested_downloads' in info and info['requested_downloads']:
                        maybe = info['requested_downloads'][0].get('filepath')
                        if maybe:
                            filepath = Path(maybe)

                    # Fallback to yt-dlp merged filename.
                    if (not filepath or not filepath.exists()) and info.get('_filename'):
                        maybe = Path(info['_filename'])
                        if maybe.exists():
                            filepath = maybe

                    # Final fallback: construct path from info id/ext.
                    if not filepath or not filepath.exists():
                        video_id = info.get('id', 'video')
                        ext = info.get('ext', 'mp4')
                        maybe = self.temp_dir / f"{video_id}.{ext}"
                        if maybe.exists():
                            filepath = maybe

                    # Check if file exists and is within size limit
                    if filepath and filepath.exists():
                        file_size = filepath.stat().st_size
                        if file_size <= self.max_file_size_bytes:
                            title = info.get('title', 'Video')
                            return (filepath, title)
                        else:
                            compressed = self._compress_video(filepath)
                            if compressed:
                                title = info.get('title', 'Video')
                                return (compressed, title)

                            # File too large, clean up and try next strategy
                            filepath.unlink(missing_ok=True)
                            continue

            except yt_dlp.utils.DownloadError as e:
                # Try next format strategy
                error_msg = str(e).lower()
                logger.debug(f"Video download error for {url}: {error_msg}")

                # If it's a private video or login required, don't retry
                if 'private' in error_msg or 'login' in error_msg or 'unavailable' in error_msg:
                    logger.info(f"Content unavailable or requires login: {url}")
                    return None

                continue
            except Exception as e:
                # Try next strategy
                logger.debug(f"Exception downloading {url}: {type(e).__name__}: {str(e)}")
                continue

        # All video download strategies failed
        return None

    def _compress_video(self, filepath: Path) -> Optional[Path]:
        """Compress a downloaded video so it fits within the configured size cap."""
        if not filepath.exists():
            return None

        input_size = filepath.stat().st_size
        if input_size <= self.max_file_size_bytes:
            return filepath

        duration = self._probe_duration_seconds(filepath)
        if not duration:
            logger.warning(f"Could not probe duration for {filepath.name}; skipping compression")
            return None

        target_total_bps = int((self.max_file_size_bytes * 8 * 0.88) / duration)
        if target_total_bps < 180_000:
            logger.warning(
                f"Video {filepath.name} is too long to compress under size limit "
                f"at usable quality"
            )
            return None

        attempts = [
            (720, 96_000, 1.00),
            (540, 80_000, 0.90),
            (480, 64_000, 0.80),
            (360, 48_000, 0.65),
        ]

        for max_height, audio_bps, video_factor in attempts:
            video_bps = int((target_total_bps - audio_bps) * video_factor)
            if video_bps < 120_000:
                continue

            output = filepath.with_name(
                f"{filepath.stem}_compressed_{max_height}p{filepath.suffix or '.mp4'}"
            )
            output.unlink(missing_ok=True)

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(filepath),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                f"scale=-2:{max_height}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                str(video_bps),
                "-maxrate",
                str(int(video_bps * 1.25)),
                "-bufsize",
                str(int(video_bps * 2)),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                str(audio_bps),
                "-movflags",
                "+faststart",
                str(output),
            ]

            try:
                logger.info(
                    f"Compressing {filepath.name} to <= "
                    f"{self.max_file_size_bytes / (1024 * 1024):.0f} MB "
                    f"({max_height}p, {video_bps // 1000}k video)"
                )
                completed = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning(f"Video compression failed for {filepath.name}: {e}")
                output.unlink(missing_ok=True)
                continue

            if completed.returncode != 0:
                logger.debug(
                    f"ffmpeg failed for {filepath.name}: {completed.stderr[-1000:]}"
                )
                output.unlink(missing_ok=True)
                continue

            if output.exists() and output.stat().st_size <= self.max_file_size_bytes:
                compressed_size = output.stat().st_size / (1024 * 1024)
                original_size = input_size / (1024 * 1024)
                logger.info(
                    f"Compressed {filepath.name}: {original_size:.2f} MB -> "
                    f"{compressed_size:.2f} MB"
                )
                filepath.unlink(missing_ok=True)
                return output

            if output.exists():
                logger.warning(
                    f"Compressed file still exceeds size limit: "
                    f"{output.stat().st_size / (1024 * 1024):.2f} MB"
                )
                output.unlink(missing_ok=True)

        logger.warning(f"Could not compress {filepath.name} under size limit")
        return None

    def _probe_duration_seconds(self, filepath: Path) -> Optional[float]:
        """Return video duration using ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(filepath),
        ]

        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug(f"ffprobe failed for {filepath.name}: {e}")
            return None

        if completed.returncode != 0:
            return None

        try:
            duration = float(completed.stdout.strip())
        except ValueError:
            return None

        return duration if duration > 0 else None
    
    def cleanup(self, filepath: Path) -> None:
        """Delete downloaded file."""
        try:
            if filepath.exists():
                filepath.unlink()
        except Exception:
            pass
