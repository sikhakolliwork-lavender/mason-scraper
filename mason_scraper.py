#!/usr/bin/env python3
"""
Mason Stores Product Scraper
Extracts all products from masonstores.com with images.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import aiofiles
import aiohttp
import pandas as pd
import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

# Configuration
BASE_URL = "https://masonstores.com"
PRODUCTS_URL = f"{BASE_URL}/products"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_DELAY_MIN = 3.0  # Minimum delay between requests
DEFAULT_DELAY_MAX = 6.0  # Maximum delay between requests
MAX_CONCURRENT_DOWNLOADS = 3  # Conservative for safety
IMAGE_DOWNLOAD_DELAY = 0.5  # Delay between image downloads
CHECKPOINT_INTERVAL = 25  # Save progress every N products
BREAK_INTERVAL = 100  # Take a longer break every N products
BREAK_DURATION_MIN = 30  # Minimum break duration (seconds)
BREAK_DURATION_MAX = 60  # Maximum break duration (seconds)

# User-Agent rotation pool (common browsers)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/scraper.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class MasonStoreScraper:
    """Scraper for masonstores.com products."""

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR, delay_min: float = DEFAULT_DELAY_MIN, delay_max: float = DEFAULT_DELAY_MAX):
        self.output_dir = Path(output_dir)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.session = requests.Session()
        self._rotate_user_agent()  # Set initial UA
        self.products = []
        self.progress_file = self.output_dir / "progress.json"
        self.interrupted = False
        self.start_time = None
        self.request_count = 0

        # Create output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "images").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)

    def _rotate_user_agent(self):
        """Rotate to a random User-Agent and set browser-like headers."""
        ua = random.choice(USER_AGENTS)
        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",  # No brotli - requests doesn't support it by default
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })

    def _random_delay(self):
        """Sleep for a random duration between min and max delay."""
        delay = random.uniform(self.delay_min, self.delay_max)
        time.sleep(delay)
        return delay

    def _take_break(self, reason: str = "periodic break"):
        """Take a longer break to appear more human-like."""
        duration = random.uniform(BREAK_DURATION_MIN, BREAK_DURATION_MAX)
        logger.info(f"☕ Taking {reason} ({duration:.0f}s)...")
        time.sleep(duration)
        self._rotate_user_agent()  # Also rotate UA after break
        logger.info("Resuming...")

    def _get_eta(self, completed: int, total: int) -> str:
        """Calculate estimated time remaining."""
        if not self.start_time or completed == 0:
            return "calculating..."
        elapsed = (datetime.now() - self.start_time).total_seconds()
        rate = completed / elapsed  # products per second
        remaining = total - completed
        if rate > 0:
            eta_seconds = remaining / rate
            eta = timedelta(seconds=int(eta_seconds))
            return str(eta)
        return "calculating..."

    def _log_status(self, completed: int, total: int, errors: int = 0):
        """Log current scraping status."""
        pct = (completed / total) * 100 if total > 0 else 0
        eta = self._get_eta(completed, total)
        logger.info(f"📊 Progress: {completed}/{total} ({pct:.1f}%) | Errors: {errors} | ETA: {eta}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=5, max=30))
    def _fetch_page(self, url: str, referer: str = None) -> BeautifulSoup:
        """Fetch a page and return parsed HTML."""
        # Add referer to look more like natural browsing
        headers = {}
        if referer:
            headers["Referer"] = referer
        else:
            headers["Referer"] = BASE_URL

        self.request_count += 1

        # Rotate UA every 50 requests
        if self.request_count % 50 == 0:
            self._rotate_user_agent()

        response = self.session.get(url, timeout=30, headers=headers)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")

    def get_product_urls_from_sitemap(self, local_file: str = None) -> list:
        """Get all product URLs from sitemap.xml or local file."""
        if local_file and Path(local_file).exists():
            logger.info(f"Reading sitemap from local file: {local_file}")
            with open(local_file, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            sitemap_url = f"{BASE_URL}/sitemap.xml"
            logger.info(f"Fetching sitemap from {sitemap_url}")
            response = self.session.get(sitemap_url, timeout=120)
            response.raise_for_status()
            content = response.text

        # Extract URLs - handle both XML (<loc>) and HTML (<td class="url">) formats
        urls = re.findall(r'(?:<loc>|<td class="url">)(https://masonstores\.com/products/[^<]+)(?:</loc>|</td>)', content)

        logger.info(f"Found {len(urls)} product URLs in sitemap")
        return urls

    def get_total_pages(self) -> int:
        """Get the total number of product listing pages."""
        soup = self._fetch_page(PRODUCTS_URL)

        # Find pagination info - look for "Page X of Y" or last page number
        pagination = soup.select("ul.pagination li a")
        if pagination:
            # Get the last numbered page link
            page_numbers = []
            for link in pagination:
                href = link.get("href", "")
                match = re.search(r"page=(\d+)", href)
                if match:
                    page_numbers.append(int(match.group(1)))
            if page_numbers:
                return max(page_numbers)

        # Fallback: check for total count text
        count_text = soup.find(string=re.compile(r"(\d+)\s*items"))
        if count_text:
            match = re.search(r"(\d+)", count_text)
            if match:
                total_items = int(match.group(1))
                return (total_items + 23) // 24  # 24 items per page

        logger.warning("Could not determine total pages, defaulting to 139")
        return 139

    def scrape_listing_page(self, page_num: int) -> list:
        """Scrape products from a listing page."""
        url = f"{PRODUCTS_URL}?page={page_num}"
        soup = self._fetch_page(url)
        products = []

        # Find all product cards
        product_cards = soup.select("div.product-card, div.product-item, article.product")

        if not product_cards:
            # Try alternative selectors
            product_cards = soup.select("[class*='product']")

        for card in product_cards:
            try:
                product = self._parse_product_card(card)
                if product and product.get("name"):
                    products.append(product)
            except Exception as e:
                logger.debug(f"Error parsing product card: {e}")
                continue

        return products

    def _parse_product_card(self, card) -> dict:
        """Parse a single product card from listing page."""
        product = {}

        # Product link and name
        link = card.select_one("a[href*='/products/']")
        if link:
            product["product_url"] = urljoin(BASE_URL, link.get("href", ""))
            product["id"] = link.get("href", "").split("/products/")[-1].strip("/")

            # Name from link text or title
            name_elem = card.select_one("h3, h4, h5, .product-title, .product-name, a[href*='/products/']")
            if name_elem:
                product["name"] = name_elem.get_text(strip=True)

        # Prices
        price_elem = card.select_one(".price, .sale-price, .current-price, [class*='price']")
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            product["price"] = self._parse_price(price_text)

        original_price_elem = card.select_one(".original-price, .old-price, del, s, [class*='original']")
        if original_price_elem:
            orig_text = original_price_elem.get_text(strip=True)
            product["original_price"] = self._parse_price(orig_text)

        # Image
        img = card.select_one("img")
        if img:
            img_src = img.get("src") or img.get("data-src") or img.get("data-lazy")
            if img_src:
                product["image_urls"] = [urljoin(BASE_URL, img_src)]

        # Category
        category_elem = card.select_one(".category, .product-category, [class*='category']")
        if category_elem:
            product["category"] = category_elem.get_text(strip=True)

        return product

    def _parse_price(self, price_text: str) -> float:
        """Extract numeric price from text."""
        if not price_text:
            return None
        # Remove currency symbols and extract number
        numbers = re.findall(r"[\d,]+\.?\d*", price_text.replace(",", ""))
        if numbers:
            try:
                return float(numbers[0])
            except ValueError:
                pass
        return None

    def scrape_product_detail(self, product: dict) -> dict:
        """Scrape additional details from product detail page."""
        if not product.get("product_url"):
            return product

        try:
            soup = self._fetch_page(product["product_url"])
            product_id = product.get("id", "")

            # Title - from h2.title-detail
            title_elem = soup.select_one("h2.title-detail")
            if title_elem:
                product["name"] = title_elem.get_text(strip=True)

            # Prices - current and original
            current_price = soup.select_one(".current-price")
            if current_price:
                product["price"] = self._parse_price(current_price.get_text())

            old_price = soup.select_one(".old-price")
            if old_price:
                product["original_price"] = self._parse_price(old_price.get_text())

            # SKU - from #product-sku .sku-text or hidden input
            sku_elem = soup.select_one("#product-sku .sku-text")
            if sku_elem:
                sku_text = sku_elem.get_text(strip=True)
                if sku_text and sku_text not in [":", ""]:
                    product["sku"] = sku_text
            # Fallback: try hidden input or product ID
            if not product.get("sku"):
                hidden_id = soup.select_one("input.hidden-product-id")
                if hidden_id:
                    product["sku"] = hidden_id.get("value")

            # Brand - from link to /brands/
            brand_link = soup.select_one("a[href*='/brands/']")
            if brand_link:
                product["brand"] = brand_link.get_text(strip=True)

            # Categories - from links in detail-info
            categories = []
            cat_links = soup.select(".detail-info a[href*='/product-categories/']")
            for link in cat_links:
                cat_name = link.get_text(strip=True)
                if cat_name and cat_name not in categories:
                    categories.append(cat_name)
            if categories:
                product["categories"] = categories

            # Tags - from links in detail-info
            tags = []
            tag_links = soup.select(".detail-info a[href*='/product-tags/']")
            for link in tag_links:
                tag_name = link.get_text(strip=True)
                if tag_name and tag_name not in tags:
                    tags.append(tag_name)
            if tags:
                product["tags"] = tags

            # Description - from tab content
            desc_elem = soup.select_one(".tab-pane.active, .tab-content .tab-pane")
            if desc_elem:
                desc_text = desc_elem.get_text(strip=True)
                if desc_text:
                    product["description"] = desc_text[:2000]

            # Parse specifications from description (Key: Value patterns)
            if product.get("description"):
                specs = {}
                desc = product["description"]

                # Known specification keys to look for
                known_keys = [
                    "Material", "Brand", "Colour", "Color", "Product Dimensions",
                    "Dimensions", "Exterior Finish", "Finish", "Handle Type",
                    "Shape", "Special Feature", "Included Components", "Lock Type",
                    "Type", "Size", "Weight", "Warranty", "Model", "Power",
                    "Voltage", "Wattage", "Capacity", "Country of Origin"
                ]

                # Try to extract each known key
                for key in known_keys:
                    # Pattern: Key : Value or Key: Value (with possible space variations)
                    pattern = rf'{re.escape(key)}\s*:\s*([^:]+?)(?=(?:{"|".join(re.escape(k) for k in known_keys)})\s*:|$)'
                    match = re.search(pattern, desc, re.IGNORECASE)
                    if match:
                        value = match.group(1).strip()
                        if value and len(value) < 100:
                            specs[key] = value

                if specs:
                    product["specifications"] = specs

            # Seller - from short-desc
            seller_link = soup.select_one(".short-desc a[href*='/stores/']")
            if seller_link:
                product["seller"] = seller_link.get_text(strip=True)

            # Availability / Stock status
            stock_elem = soup.select_one(".number-items-available")
            if stock_elem:
                stock_text = stock_elem.get_text(strip=True).lower()
                product["in_stock"] = "in stock" in stock_text
                product["availability"] = stock_elem.get_text(strip=True)

            # Product images - from detail-gallery
            product_images = []
            gallery = soup.select_one("div.detail-gallery, div.product-image-slider")
            if gallery:
                for img in gallery.select("img"):
                    src = img.get("src") or img.get("data-src", "")
                    if src and "/storage/products/" in src and "150x150" not in src:
                        product_images.append(urljoin(BASE_URL, src))

            # Fallback for images
            if not product_images:
                all_imgs = soup.select("img[src*='/storage/products/']")
                for img in all_imgs[:5]:
                    src = img.get("src") or img.get("data-src", "")
                    if src and "150x150" not in src:
                        img_name = src.split("/")[-1].lower()
                        skip = ["icon", "logo", "banner", "placeholder"]
                        if not any(s in img_name for s in skip):
                            product_images.append(urljoin(BASE_URL, src))
                            break

            product["image_urls"] = list(dict.fromkeys(product_images))[:5]

        except Exception as e:
            logger.warning(f"Error fetching detail for {product.get('id')}: {e}")

        return product

    def _get_all_image_variations(self, img_url: str) -> list:
        """Get all available image variations (original, 800x800, 400x400)."""
        variations = []

        # Extract base name and extension
        # e.g., "aianna-1-800x800.jpg" -> base="aianna-1", ext=".jpg"
        match = re.match(r'(.+?)(-\d+x\d+)?(\.\w+)$', img_url.split('/')[-1])
        if not match:
            return [(img_url, 'original')]

        base_name = match.group(1)
        ext = match.group(3)
        base_url = img_url.rsplit('/', 1)[0]

        # Define variations to try
        variation_suffixes = [
            ('', 'original'),
            ('-800x800', '800x800'),
            ('-400x400', '400x400'),
        ]

        for suffix, label in variation_suffixes:
            var_url = f"{base_url}/{base_name}{suffix}{ext}"
            try:
                resp = self.session.head(var_url, timeout=5)
                if resp.status_code == 200:
                    size = int(resp.headers.get('Content-Length', 0))
                    if size > 0:
                        variations.append((var_url, label, size))
            except Exception:
                pass

        return variations

    async def download_image(self, session: aiohttp.ClientSession, url: str, filepath: Path) -> bool:
        """Download a single image."""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    async with aiofiles.open(filepath, "wb") as f:
                        await f.write(await response.read())
                    return True
        except Exception as e:
            logger.debug(f"Failed to download {url}: {e}")
        return False

    async def download_all_images(self, products: list):
        """Download all image variations for products with rate limiting."""
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        download_count = 0

        async def download_with_semaphore(session, url, filepath):
            async with semaphore:
                result = await self.download_image(session, url, filepath)
                # Small delay between downloads
                await asyncio.sleep(IMAGE_DOWNLOAD_DELAY)
                return result

        tasks = []
        headers = {"User-Agent": random.choice(USER_AGENTS)}

        async with aiohttp.ClientSession(headers=headers) as session:
            for product in products:
                product_id = product.get("id", "unknown")
                product["local_images"] = []

                for idx, img_url in enumerate(product.get("image_urls", [])):
                    # Get all available variations
                    variations = self._get_all_image_variations(img_url)

                    for var_url, var_label, var_size in variations:
                        ext = Path(var_url).suffix or ".jpg"
                        ext = ext.split("?")[0]  # Remove query params
                        filename = f"{product_id}_{idx + 1}_{var_label}{ext}"
                        filepath = self.output_dir / "images" / filename

                        if not filepath.exists():
                            tasks.append((
                                download_with_semaphore(session, var_url, filepath),
                                product,
                                filename
                            ))
                        else:
                            product["local_images"].append(str(filepath))

            # Execute downloads with progress bar
            if tasks:
                logger.info(f"📷 Downloading {len(tasks)} images (max {MAX_CONCURRENT_DOWNLOADS} concurrent)...")
                for coro, product, filename in tqdm(tasks, desc="Downloading images"):
                    success = await coro
                    if success:
                        product["local_images"].append(
                            str(self.output_dir / "images" / filename)
                        )
                        download_count += 1

                    # Take a break every 200 images
                    if download_count > 0 and download_count % 200 == 0:
                        logger.info(f"📷 Downloaded {download_count} images, taking short break...")
                        await asyncio.sleep(random.uniform(10, 20))

    def save_progress(self, last_page: int, products: list):
        """Save current progress for resume."""
        state = {
            "last_page": last_page,
            "completed_ids": [p.get("id") for p in products if p.get("id")],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(self.progress_file, "w") as f:
            json.dump(state, f, indent=2)

    def load_progress(self) -> dict:
        """Load previous progress if exists."""
        if self.progress_file.exists():
            try:
                with open(self.progress_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"last_page": 0, "completed_ids": []}

    def export_json(self, products: list):
        """Export products to JSON."""
        filepath = self.output_dir / "products.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(products, f, indent=2, ensure_ascii=False)
        logger.info(f"Exported {len(products)} products to {filepath}")

    def export_csv(self, products: list):
        """Export products to CSV."""
        filepath = self.output_dir / "products.csv"

        # Flatten for CSV
        flat_products = []
        for p in products:
            flat = {k: v for k, v in p.items() if not isinstance(v, (list, dict))}
            flat["image_urls"] = "|".join(p.get("image_urls", []))
            flat["local_images"] = "|".join(p.get("local_images", []))
            if p.get("specifications"):
                flat["specifications"] = json.dumps(p["specifications"])
            flat_products.append(flat)

        df = pd.DataFrame(flat_products)
        df.to_csv(filepath, index=False, encoding="utf-8")
        logger.info(f"Exported {len(products)} products to {filepath}")

    def run(self, resume: bool = False, sitemap_file: str = None):
        """Main execution using sitemap for product URLs."""
        # Handle Ctrl+C gracefully
        def signal_handler(sig, frame):
            logger.info("\n⚠️  Interrupted! Saving progress...")
            self.interrupted = True

        signal.signal(signal.SIGINT, signal_handler)

        self.start_time = datetime.now()
        error_count = 0

        # Load previous progress if resuming
        completed_ids = set()
        if resume:
            progress = self.load_progress()
            completed_ids = set(progress.get("completed_ids", []))
            # Also load any previously scraped products from JSON
            existing_json = self.output_dir / "products.json"
            if existing_json.exists():
                try:
                    with open(existing_json) as f:
                        self.products = json.load(f)
                    logger.info(f"📂 Loaded {len(self.products)} existing products from checkpoint")
                except Exception:
                    pass
            logger.info(f"🔄 Resuming - {len(completed_ids)} products already completed")

        # Get product URLs from sitemap
        logger.info("📋 Fetching product URLs from sitemap...")
        all_urls = self.get_product_urls_from_sitemap(sitemap_file)

        # Filter out already completed products
        urls_to_scrape = []
        for url in all_urls:
            product_id = url.split("/products/")[-1].strip("/")
            if product_id not in completed_ids:
                urls_to_scrape.append((product_id, url))

        total_to_scrape = len(urls_to_scrape)
        logger.info(f"🎯 Products to scrape: {total_to_scrape}")
        logger.info(f"⚙️  Settings: delay={self.delay_min}-{self.delay_max}s, checkpoint every {CHECKPOINT_INTERVAL}, break every {BREAK_INTERVAL}")

        # Scrape product details
        logger.info("🚀 Starting product scraping...")
        for i, (product_id, url) in enumerate(urls_to_scrape):
            if self.interrupted:
                break

            try:
                product = {"id": product_id, "product_url": url}
                self.scrape_product_detail(product)
                self.products.append(product)

                # Progress indicator
                completed = i + 1
                pct = (completed / total_to_scrape) * 100

                # Log every 10 products or at milestones
                if completed % 10 == 0 or completed in [1, 5]:
                    self._log_status(completed, total_to_scrape, error_count)

                # Save checkpoint every CHECKPOINT_INTERVAL products
                if completed % CHECKPOINT_INTERVAL == 0:
                    logger.info(f"💾 Saving checkpoint at {completed} products...")
                    self.save_progress(i, self.products)
                    self.export_json(self.products)
                    self.export_csv(self.products)

                # Take a break every BREAK_INTERVAL products
                if completed % BREAK_INTERVAL == 0 and completed < total_to_scrape:
                    self._take_break(f"periodic break after {completed} products")

                # Random delay before next request
                self._random_delay()

            except Exception as e:
                error_count += 1
                logger.error(f"❌ Error scraping {product_id}: {e}")
                # Take extra delay on error
                time.sleep(random.uniform(5, 10))
                continue

        # Final save on interrupt
        if self.interrupted:
            self.save_progress(len(self.products), self.products)
            logger.info(f"💾 Progress saved. Scraped {len(self.products)} products.")
            self.export_json(self.products)
            self.export_csv(self.products)
            logger.info("ℹ️  Run with --resume to continue later")
            return

        # Download images
        logger.info(f"📷 Starting image downloads for {len(self.products)} products...")
        asyncio.run(self.download_all_images(self.products))

        # Export final data
        logger.info("📁 Exporting final data...")
        self.export_json(self.products)
        self.export_csv(self.products)

        # Clean up progress file
        if self.progress_file.exists() and not self.interrupted:
            self.progress_file.unlink()

        # Final summary
        elapsed = datetime.now() - self.start_time
        logger.info(f"✅ Done! Scraped {len(self.products)} products in {elapsed}")
        logger.info(f"📊 Final stats: {error_count} errors, {self.request_count} requests")


def main():
    parser = argparse.ArgumentParser(description="Scrape products from masonstores.com")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN, help="Minimum delay between requests (seconds)")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX, help="Maximum delay between requests (seconds)")
    parser.add_argument("--resume", "-r", action="store_true", help="Resume from previous progress")
    parser.add_argument("--sitemap", "-s", help="Path to local sitemap XML file")

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Mason Stores Product Scraper - Safe Mode")
    logger.info("=" * 60)
    logger.info(f"Output directory: {args.output}")
    logger.info(f"Delay range: {args.delay_min}-{args.delay_max} seconds")
    logger.info(f"Checkpoint interval: every {CHECKPOINT_INTERVAL} products")
    logger.info(f"Break interval: every {BREAK_INTERVAL} products ({BREAK_DURATION_MIN}-{BREAK_DURATION_MAX}s)")
    logger.info(f"Max concurrent image downloads: {MAX_CONCURRENT_DOWNLOADS}")
    logger.info("=" * 60)

    scraper = MasonStoreScraper(output_dir=args.output, delay_min=args.delay_min, delay_max=args.delay_max)
    scraper.run(resume=args.resume, sitemap_file=args.sitemap)


if __name__ == "__main__":
    main()
