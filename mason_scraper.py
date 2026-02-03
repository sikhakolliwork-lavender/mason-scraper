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
import re
import signal
import sys
import time
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
DEFAULT_DELAY = 1.0
MAX_CONCURRENT_DOWNLOADS = 10

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

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR, delay: float = DEFAULT_DELAY):
        self.output_dir = Path(output_dir)
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        self.products = []
        self.progress_file = self.output_dir / "progress.json"
        self.interrupted = False

        # Create output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "images").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page(self, url: str) -> BeautifulSoup:
        """Fetch a page and return parsed HTML."""
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")

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

            # SKU - look for text containing "SKU"
            sku_text = soup.find(string=re.compile(r"SKU\s*:?\s*\d+", re.I))
            if sku_text:
                match = re.search(r"SKU\s*:?\s*(\d+)", sku_text, re.I)
                if match:
                    product["sku"] = match.group(1)

            # Brand - look for link to /brands/ page
            brand_link = soup.select_one("a[href*='/brands/']")
            if brand_link:
                product["brand"] = brand_link.get_text(strip=True)

            # Description
            desc_elem = soup.select_one(".description, .product-description, [class*='description']")
            if desc_elem:
                product["description"] = desc_elem.get_text(strip=True)[:1000]

            # Product images - look for main product gallery
            product_images = []

            # The main product images are in div.detail-gallery or div.product-image-slider
            gallery = soup.select_one("div.detail-gallery, div.product-image-slider")
            if gallery:
                for img in gallery.select("img"):
                    src = img.get("src") or img.get("data-src", "")
                    if src and "/storage/products/" in src and "150x150" not in src:
                        product_images.append(urljoin(BASE_URL, src))

            # Fallback: get first img with /storage/products/ that looks like main image
            if not product_images:
                all_imgs = soup.select("img[src*='/storage/products/']")
                for img in all_imgs[:5]:  # Check first 5 images
                    src = img.get("src") or img.get("data-src", "")
                    if src and "150x150" not in src:
                        img_name = src.split("/")[-1].lower()
                        # Skip obvious non-product images
                        skip = ["icon", "logo", "banner", "placeholder"]
                        if not any(s in img_name for s in skip):
                            product_images.append(urljoin(BASE_URL, src))
                            break  # Just take first valid one as fallback

            product["image_urls"] = list(dict.fromkeys(product_images))[:3]

            # Specifications (if table exists)
            specs = {}
            spec_rows = soup.select("table tr, .specifications tr, [class*='spec'] tr")
            for row in spec_rows:
                cells = row.select("td, th")
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True)
                    value = cells[1].get_text(strip=True)
                    if key and value:
                        specs[key] = value
            if specs:
                product["specifications"] = specs

            # Stock status
            stock_elem = soup.select_one(".stock, .availability, [class*='stock']")
            if stock_elem:
                stock_text = stock_elem.get_text(strip=True).lower()
                product["in_stock"] = "out" not in stock_text

        except Exception as e:
            logger.warning(f"Error fetching detail for {product.get('id')}: {e}")

        return product

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
        """Download all images for products."""
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        async def download_with_semaphore(session, url, filepath):
            async with semaphore:
                return await self.download_image(session, url, filepath)

        tasks = []
        async with aiohttp.ClientSession() as session:
            for product in products:
                product_id = product.get("id", "unknown")
                product["local_images"] = []

                for idx, img_url in enumerate(product.get("image_urls", [])):
                    ext = Path(img_url).suffix or ".jpg"
                    ext = ext.split("?")[0]  # Remove query params
                    filename = f"{product_id}_{idx + 1}{ext}"
                    filepath = self.output_dir / "images" / filename

                    if not filepath.exists():
                        tasks.append((
                            download_with_semaphore(session, img_url, filepath),
                            product,
                            filename
                        ))
                    else:
                        product["local_images"].append(str(filepath))

            # Execute downloads with progress bar
            if tasks:
                logger.info(f"Downloading {len(tasks)} images...")
                for coro, product, filename in tqdm(tasks, desc="Downloading images"):
                    success = await coro
                    if success:
                        product["local_images"].append(
                            str(self.output_dir / "images" / filename)
                        )

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

    def run(self, resume: bool = False):
        """Main execution."""
        # Handle Ctrl+C gracefully
        def signal_handler(sig, frame):
            logger.info("\nInterrupted! Saving progress...")
            self.interrupted = True

        signal.signal(signal.SIGINT, signal_handler)

        # Load previous progress if resuming
        start_page = 1
        if resume:
            progress = self.load_progress()
            start_page = progress.get("last_page", 0) + 1
            logger.info(f"Resuming from page {start_page}")

        # Get total pages
        logger.info("Fetching total page count...")
        total_pages = self.get_total_pages()
        logger.info(f"Total pages: {total_pages}")

        # Scrape listing pages
        logger.info("Scraping product listings...")
        for page in tqdm(range(start_page, total_pages + 1), desc="Pages"):
            if self.interrupted:
                break

            try:
                page_products = self.scrape_listing_page(page)
                self.products.extend(page_products)

                # Save progress every 5 pages
                if page % 5 == 0:
                    self.save_progress(page, self.products)

                time.sleep(self.delay)
            except Exception as e:
                logger.error(f"Error on page {page}: {e}")
                continue

        if self.interrupted:
            self.save_progress(page - 1, self.products)
            logger.info(f"Progress saved. Scraped {len(self.products)} products.")
            return

        # Scrape product details
        logger.info("Fetching product details...")
        for product in tqdm(self.products, desc="Details"):
            if self.interrupted:
                break
            self.scrape_product_detail(product)
            time.sleep(self.delay / 2)

        # Download images
        if not self.interrupted:
            logger.info("Downloading images...")
            asyncio.run(self.download_all_images(self.products))

        # Export data
        logger.info("Exporting data...")
        self.export_json(self.products)
        self.export_csv(self.products)

        # Clean up progress file
        if self.progress_file.exists() and not self.interrupted:
            self.progress_file.unlink()

        logger.info(f"Done! Scraped {len(self.products)} products.")


def main():
    parser = argparse.ArgumentParser(description="Scrape products from masonstores.com")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("--delay", "-d", type=float, default=DEFAULT_DELAY, help="Delay between requests (seconds)")
    parser.add_argument("--resume", "-r", action="store_true", help="Resume from previous progress")

    args = parser.parse_args()

    scraper = MasonStoreScraper(output_dir=args.output, delay=args.delay)
    scraper.run(resume=args.resume)


if __name__ == "__main__":
    main()
