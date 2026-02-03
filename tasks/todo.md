# Mason Scraper - Task List

## Phase 1: Project Setup
- [x] Create project folder at `/Users/ss/github/mason-scraper`
- [x] Initialize git repository
- [x] Create `.gitignore` file
- [x] Create `CLAUDE.md` with working instructions
- [x] Create `.claude/` folder for tracking
- [x] Create `tasks/todo.md` with this task list
- [x] Create `requirements.txt` with dependencies
- [x] Create initial commit and push to GitHub

## Phase 2: Core Scraper
- [x] Create `mason_scraper.py` with basic structure
- [x] Implement `get_total_pages()` function
- [x] Implement `scrape_listing_page()` function
- [x] Implement `scrape_product_detail()` function
- [x] Add rate limiting (1 sec delay between requests)
- [x] Test with first 2 pages

## Phase 3: Data Export
- [x] Implement `export_json()` function
- [x] Implement `export_csv()` function
- [x] Test exports with sample data

## Phase 4: Image Downloads
- [x] Implement async `download_image()` function
- [x] Implement `download_all_images()` with semaphore
- [x] Test with 10 sample images

## Phase 5: Resume & Progress
- [x] Implement `save_progress()` function
- [x] Implement `load_progress()` function
- [x] Add Ctrl+C handler to save on interrupt
- [x] Test resume functionality

## Phase 6: Final Testing
- [ ] Run full scrape (all 139 pages)
- [ ] Verify JSON output
- [ ] Verify CSV output
- [ ] Verify images downloaded
- [ ] Final commit and push

---

## Review
_To be completed after implementation_
