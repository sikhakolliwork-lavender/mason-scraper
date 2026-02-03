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
        """Download all image variations for products."""
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
