"""Discord bot main entry point."""
import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
import aiohttp

from bot.config import config
from bot.downloader import VideoDownloader
from bot.reddit_handler import RedditHandler

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# URL patterns for TikTok, Instagram, YouTube Shorts, Reddit, and Facebook
TIKTOK_PATTERN = re.compile(
    r'https?://(?:(?:www|vm|vt)\.)?tiktok\.com/[^\s]+',
    re.IGNORECASE
)
INSTAGRAM_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[^\s]+',
    re.IGNORECASE
)
YOUTUBE_SHORTS_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/)[^\s]+',
    re.IGNORECASE
)
REDDIT_PATTERN = re.compile(
    r'https?://(?:'
    r'(?:(?:www\.|old\.|new\.)?reddit\.com/(?:(?:r/[^/\s]+/(?:comments|s)/[^\s]+)|comments/[^\s]+))'
    r'|(?:www\.)?redd\.it/[^\s]+'
    r')',
    re.IGNORECASE
)
FACEBOOK_PATTERN = re.compile(
    r'https?://(?:'
    r'(?:[a-z0-9-]+\.)?facebook\.com/(?:'
    r'reel/|watch/?\?|watch/|share/|story\.php|video\.php|[^/\s<>()]+/videos?/'
    r')[^\s<>()]*'
    r'|(?:www\.)?fb\.watch/[^\s<>()]+'
    r')',
    re.IGNORECASE
)


class MemeBot(commands.Bot):
    """Discord bot that replaces social media links with uploaded media.
    
    Supports: TikTok, Instagram (posts/reels), YouTube Shorts, Reddit posts, and Facebook videos.
    """
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # Required to read message content
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.downloader = VideoDownloader(
            config.max_file_size_bytes,
            cookies_file=config.cookies_file
        )
        self.reddit_handler = RedditHandler(
            config.max_file_size_bytes,
            client_id=config.reddit_client_id,
            client_secret=config.reddit_client_secret
        )
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self._recent_urls: dict[str, float] = {}  # url -> timestamp for dedup
        self._dedup_ttl = 60.0  # seconds to ignore duplicate URLs
    
    async def setup_hook(self):
        """Called when the bot is starting up."""
        logger.info("Bot is starting up...")
    
    async def on_ready(self):
        """Called when the bot is ready."""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Monitoring {len(config.allowed_channel_ids)} channels")
        logger.info("Bot is ready!")
    
    async def on_message(self, message: discord.Message):
        """Handle incoming messages."""
        # Ignore messages from bots
        if message.author.bot:
            return
        
        # Check if channel is allowed
        if not config.is_channel_allowed(message.channel.id):
            return
        
        # Extract URLs from message
        urls = self._extract_video_urls(message.content)
        if not urls:
            return
        
        # Process each URL (with dedup)
        import time
        now = time.monotonic()
        # Cleanup old entries
        self._recent_urls = {u: t for u, t in self._recent_urls.items() if now - t < self._dedup_ttl}
        
        for url in urls:
            if url in self._recent_urls:
                logger.info(f"Skipping duplicate URL (seen {now - self._recent_urls[url]:.1f}s ago): {url}")
                continue
            self._recent_urls[url] = now
            await self._process_video_url(message, url)
    
    def _extract_video_urls(self, content: str) -> list[str]:
        """Extract supported video/post URLs from message content."""
        urls = []
        
        # Find TikTok URLs
        tiktok_matches = TIKTOK_PATTERN.findall(content)
        urls.extend(tiktok_matches)
        
        # Find Instagram URLs (Reels and Posts)
        instagram_matches = INSTAGRAM_PATTERN.findall(content)
        urls.extend(instagram_matches)
        
        # Find YouTube Shorts URLs
        youtube_matches = YOUTUBE_SHORTS_PATTERN.findall(content)
        urls.extend(youtube_matches)
        
        # Find Reddit URLs
        reddit_matches = REDDIT_PATTERN.findall(content)
        urls.extend(reddit_matches)

        # Find Facebook video/reel/share URLs
        facebook_matches = FACEBOOK_PATTERN.findall(content)
        urls.extend(facebook_matches)
        
        return urls
    
    def _is_reddit_url(self, url: str) -> bool:
        """Check if URL is a Reddit post."""
        return bool(REDDIT_PATTERN.search(url))
    
    async def _process_video_url(self, message: discord.Message, url: str):
        """Process a single video/post URL."""
        # Check if it's a Reddit post (different handling)
        if self._is_reddit_url(url):
            await self._process_reddit_post(message, url)
            return
        
        # Add processing reaction
        try:
            await message.add_reaction("⏳")
        except discord.errors.Forbidden:
            logger.warning(f"Cannot add reaction in channel {message.channel.id}")
        
        async with self.semaphore:
            filepaths: list[Path] = []
            try:
                # Download media (video or photo)
                logger.info(f"Downloading media from {url}")
                result = await self.downloader.download(url)

                if result is None:
                    await self._handle_download_failure(message, url, "No downloadable media found")
                    return

                filepaths, title = result
                for filepath in filepaths:
                    file_size_mb = filepath.stat().st_size / (1024 * 1024)
                    logger.info(f"Downloaded {filepath.name} ({file_size_mb:.2f} MB)")

                # Upload media to Discord
                await self._upload_and_replace(message, filepaths, title, url)
                
            except discord.errors.HTTPException as e:
                logger.error(f"Discord API error: {e}")
                await self._handle_download_failure(message, url, "Upload failed")
            except Exception as e:
                logger.error(f"Error processing {url}: {e}", exc_info=True)
                await self._handle_download_failure(message, url)
            finally:
                # Cleanup temp files
                for filepath in filepaths:
                    self.downloader.cleanup(filepath)
    
    async def _upload_and_replace(
        self, 
        message: discord.Message, 
        filepaths: list[Path],
        title: str,
        source_url: str
    ):
        """Upload media (prefer channel webhook) and delete original message.

        Several files (Instagram carousels) are sent in batches that respect
        Discord's 10-attachments-per-message limit and the configured size cap.
        """
        content = None

        batches: list[list[Path]] = []
        batch_size = 0
        for filepath in filepaths:
            size = filepath.stat().st_size
            if batches and len(batches[-1]) < 10 and batch_size + size <= config.max_file_size_bytes:
                batches[-1].append(filepath)
                batch_size += size
            else:
                batches.append([filepath])
                batch_size = size

        def make_files(batch: list[Path]) -> list[discord.File]:
            return [discord.File(str(p), filename=p.name) for p in batch]

        # Try webhook if configured for the channel; once it fails, use the bot for the rest
        webhook_url = config.webhook_url_for_channel(message.channel.id)
        use_webhook = bool(webhook_url)
        for batch in batches:
            sent_via_webhook = False
            if use_webhook:
                try:
                    async with aiohttp.ClientSession() as session:
                        webhook = discord.Webhook.from_url(webhook_url, session=session)
                        await webhook.send(
                            content=content,
                            files=make_files(batch),
                            username=message.author.display_name,
                            avatar_url=message.author.display_avatar.url,
                            allowed_mentions=discord.AllowedMentions.none(),
                            wait=True,
                        )
                        sent_via_webhook = True
                        logger.info(f"Uploaded {len(batch)} file(s) via webhook for message {message.id}")
                except Exception as e:
                    logger.warning(f"Webhook send failed for channel {message.channel.id}: {e}")
                    use_webhook = False

            # Fallback to normal send
            if not sent_via_webhook:
                await message.channel.send(
                    content=content,
                    files=make_files(batch),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                logger.info(f"Uploaded {len(batch)} file(s) for message {message.id}")
        
        # Delete original message
        try:
            await message.delete()
            logger.info(f"Deleted original message {message.id}")
        except discord.errors.Forbidden:
            logger.warning(f"Cannot delete message {message.id} - missing permissions")
        except discord.errors.NotFound:
            logger.warning(f"Message {message.id} already deleted")
    
    async def _process_reddit_post(self, message: discord.Message, url: str):
        """Process a Reddit post URL."""
        # Add processing reaction
        try:
            await message.add_reaction("⏳")
        except discord.errors.Forbidden:
            logger.warning(f"Cannot add reaction in channel {message.channel.id}")
        
        async with self.semaphore:
            downloaded_images = []
            downloaded_videos: list[Path] = []
            try:
                logger.info(f"Fetching Reddit post from {url}")

                # Fetch post in executor to avoid blocking
                loop = asyncio.get_event_loop()
                post = await loop.run_in_executor(None, self.reddit_handler.fetch_post, url)

                if post is None:
                    await self._handle_download_failure(message, url, "Could not fetch Reddit post")
                    return

                # If Reddit post is a video, try downloading/reuploading video directly.
                if post.video_url:
                    logger.info(f"Downloading Reddit video from {post.video_url}")
                    result = await self.downloader.download(post.video_url)
                    if result is not None:
                        downloaded_videos, _ = result
                        await self._upload_and_replace(
                            message,
                            downloaded_videos,
                            post.title,
                            post.video_url,
                        )
                        return
                    logger.warning(
                        f"Could not download Reddit video for {post.video_url}; falling back to embed"
                    )

                # Download images if any
                if post.image_urls:
                    downloaded_images = await loop.run_in_executor(
                        None, self.reddit_handler.download_images, post
                    )

                # Create Discord embed
                embed = discord.Embed(
                    title=post.title[:256],  # Discord title limit
                    description=post.text[:4096] if post.text else None,  # Discord description limit
                    color=discord.Color.orange(),
                    url=post.url or url
                )
                
                embed.set_author(name=f"r/{post.subreddit} • u/{post.author}")
                
                # Prepare files for images
                files = []
                if downloaded_images:
                    for idx, img_path in enumerate(downloaded_images):
                        ext = img_path.suffix.lstrip('.') or 'jpg'
                        filename = f"image_{idx}.{ext}"
                        file = discord.File(str(img_path), filename=filename)
                        files.append(file)
                        # Set first image as embed image
                        if idx == 0:
                            embed.set_image(url=f"attachment://{filename}")
                
                # Send embed with images (prefer webhook)
                sent_via_webhook = False
                webhook_url = config.webhook_url_for_channel(message.channel.id)
                if webhook_url:
                    try:
                        async with aiohttp.ClientSession() as session:
                            webhook = discord.Webhook.from_url(webhook_url, session=session)
                            await webhook.send(
                                embed=embed,
                                files=files if files else None,
                                username=message.author.display_name,
                                avatar_url=message.author.display_avatar.url,
                                allowed_mentions=discord.AllowedMentions.none(),
                                wait=True,
                            )
                            sent_via_webhook = True
                            logger.info(f"Posted Reddit content via webhook for message {message.id}")
                    except Exception as e:
                        logger.warning(f"Webhook send failed for Reddit content in channel {message.channel.id}: {e}")
                        sent_via_webhook = False

                if not sent_via_webhook:
                    await message.channel.send(
                        embed=embed,
                        files=files if files else None,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    logger.info(f"Posted Reddit content for message {message.id}")
                
                # Delete original message
                try:
                    await message.delete()
                    logger.info(f"Deleted original message {message.id}")
                except discord.errors.Forbidden:
                    logger.warning(f"Cannot delete message {message.id} - missing permissions")
                except discord.errors.NotFound:
                    logger.warning(f"Message {message.id} already deleted")
                
                # Remove processing reaction if message still exists
                try:
                    await message.remove_reaction("⏳", self.user)
                except Exception:
                    pass
                
            except discord.errors.HTTPException as e:
                logger.error(f"Discord API error: {e}")
                await self._handle_download_failure(message, url, "Upload failed")
            except Exception as e:
                logger.error(f"Error processing Reddit post {url}: {e}", exc_info=True)
                await self._handle_download_failure(message, url)
            finally:
                # Cleanup downloaded images/video
                for img_path in downloaded_images:
                    self.reddit_handler.cleanup(img_path)
                for video_path in downloaded_videos:
                    self.downloader.cleanup(video_path)
    
    async def _handle_download_failure(
        self, 
        message: discord.Message, 
        url: str,
        reason: str = "Download failed",
        silent: bool = False
    ):
        """Handle failed download by adding error reaction and optional reply."""
        # Forget the URL so reposting it retries immediately instead of being deduped
        self._recent_urls.pop(url, None)

        try:
            # Remove processing reaction
            await message.remove_reaction("⏳", self.user)
        except Exception:
            pass
        
        # Only add error reaction if not silent (silent = photo posts)
        if not silent:
            try:
                # Add error reaction
                await message.add_reaction("❌")
            except Exception:
                pass
        
        logger.warning(f"Failed to process {url}: {reason}")


def main():
    """Main entry point."""
    bot = MemeBot()
    
    try:
        bot.run(config.discord_token)
    except discord.LoginFailure:
        logger.error("Invalid Discord token. Check your .env file.")
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
