# Mason Stores Scraper

Web scraper to extract all products from [masonstores.com](https://masonstores.com).

## Features

- Extracts 3,300+ products with full details
- Downloads all product images
- Exports to JSON and CSV formats
- Resume capability for interrupted scrapes
- Rate limiting to respect the server

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Run full scrape
python mason_scraper.py

# Resume interrupted scrape
python mason_scraper.py --resume

# Custom output directory
python mason_scraper.py --output ./my_data
```

## Output

- `output/products.json` - All products in JSON format
- `output/products.csv` - All products in CSV format
- `output/images/` - Downloaded product images

## Data Fields

| Field | Description |
|-------|-------------|
| id | Product identifier (URL slug) |
| name | Product title |
| sku | Product SKU code |
| price | Current sale price |
| original_price | MRP before discount |
| category | Product category |
| brand | Manufacturer |
| image_urls | List of image URLs |
| product_url | Full product URL |
