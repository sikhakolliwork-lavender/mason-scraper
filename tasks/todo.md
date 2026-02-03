# Mason Scraper - Task List

## Phase 1: Project Setup
- [x] Create project folder at `/Users/ss/github/mason-scraper`
- [x] Initialize git repository
- [x] Create `.gitignore` file
- [x] Create `CLAUDE.md` with working instructions
- [x] Create `.claude/` folder for tracking
- [x] Create `tasks/todo.md` with this task list
- [x] Create `requirements.txt` with dependencies
- [ ] Create initial commit and push to GitHub

## Phase 2: Core Scraper
- [ ] Create `mason_scraper.py` with basic structure
- [ ] Implement `get_total_pages()` function
- [ ] Implement `scrape_listing_page()` function
- [ ] Implement `scrape_product_detail()` function
- [ ] Add rate limiting (1 sec delay between requests)
- [ ] Test with first 2 pages

## Phase 3: Data Export
- [ ] Implement `export_json()` function
- [ ] Implement `export_csv()` function
- [ ] Test exports with sample data

## Phase 4: Image Downloads
- [ ] Implement async `download_image()` function
- [ ] Implement `download_all_images()` with semaphore
- [ ] Test with 10 sample images

## Phase 5: Resume & Progress
- [ ] Implement `save_progress()` function
- [ ] Implement `load_progress()` function
- [ ] Add Ctrl+C handler to save on interrupt
- [ ] Test resume functionality

## Phase 6: Final Testing
- [ ] Run full scrape (all 139 pages)
- [ ] Verify JSON output
- [ ] Verify CSV output
- [ ] Verify images downloaded
- [ ] Final commit and push

---

## Review
_To be completed after implementation_
