"""
This script automates the process of finding, downloading, and analyzing PDF documents
from government websites (European Council).

It uses a simulated web browser to click through search results, downloads PDFs,
reads their text, and counts specific keywords. If a document has enough keywords,
it is saved. Otherwise, it is discarded.

To change what it searches for, update the KEYWORDS_TO_FIND list below.
To change the website or the search query, update the council_config.json file.
"""
import os
import time
import shutil
import sys
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError, Locator
from playwright._impl._errors import Error as PlaywrightError
from typing import Tuple

from scraper_utils import (
    TEMP_DOWNLOAD_DIR, PROCESSED_LOG_CSV_FILE, DOWNLOAD_PAUSE_S, PAGINATION_PAUSE_S,
    CSV_HEADER, PROCESSED_CSV_HEADER, SiteConfig, OCR_AVAILABLE, BACKGROUND_EXECUTOR, BACKGROUND_STATE,
    load_config, get_processed_entries, get_next_file_id, setup_csv_file, FileIdManager,
    append_to_csv, log_processed_entry, analyze_pdf, handle_download, process_and_save_pdf_background,
    update_progress_bar, get_failed_document_references, cleanup_failed_log_entries
)

# ==========================================
# --- CONFIGURATION (Change these!) ---
# ==========================================

# The JSON file that contains the instructions on how to navigate the website
CONFIG_FILE = "configs/council_config.json"
# The specific search configuration to use from that JSON file
CONFIG_KEYS = ["2025/semiconductor"]

# The words you are looking for. The script will count how many times these appear.
KEYWORDS_TO_FIND = ["semiconductor", "chip", "integrated circuit", "microprocessor", "cpu"]

# How many times must ANY keyword appear before we decide to keep the document?
# E.g., if set to 1, a document with 1 mention of "chip" is saved.
KEYWORD_THRESHOLD = 5

# Whether to show the browser window while it works. 
# Set to False to run it silently in the background.
SHOW_BROWSER = True

def process_document(page: Page, context_locator: Locator, file_id_manager: FileIdManager, config: SiteConfig, retry_mode: bool) -> Tuple[str, dict]:
    """
    This is the main workflow for a single document found on the search page.
    1. Read its title, date, and link.
    2. Download it.
    3. Analyze it for keywords (in the background).
    4. Save it or throw it away based on the keyword count.
    """
    log_data = {
        "keywords_used": str(KEYWORDS_TO_FIND),
        "keyword_threshold": str(KEYWORD_THRESHOLD),
    }
    latest_action = ""
    selectors = config["selectors"]

    try:
        # --- Step 1: Read Information from the Web Page ---
        ref_title_loc = context_locator.locator(selectors["reference_title"])
        doc_ref = ref_title_loc.inner_text().strip() if ref_title_loc.count() > 0 else ""
        log_data["document_reference"] = doc_ref

        doc_link_loc = context_locator.locator(selectors["document_link"])
        log_data["title"] = doc_link_loc.inner_text().strip() if doc_link_loc.count() > 0 else "N/A"
        
        doc_url = doc_link_loc.get_attribute("href") if doc_link_loc.count() > 0 else ""
        if doc_url and not doc_url.startswith('http'):
            doc_url = f"{config['base_url']}{doc_url}" # Fix broken links
        log_data["document_url"] = doc_url
        
        log_data["source_page_url"] = page.url
        date_loc = context_locator.locator(selectors["date"])
        log_data["publication_date"] = date_loc.inner_text().strip() if date_loc.count() > 0 else ""
        
        # --- Step 2: Download the Document ---
        if not doc_url:
            raise ValueError("No download link found for this document.")

        # Let the browser download the file naturally to avoid corruption
        temp_dl_page = page.context.new_page()
        try:
            response = page.context.request.get(doc_url, timeout=60000)
            if not response or not response.ok:
                raise ValueError(f"Download failed (Error code {response.status if response else 'Unknown'})")
            
            suggested_filename = doc_url.split('/')[-3].split('?')[0]
            if not suggested_filename or suggested_filename.lower() == "pdf":
                suggested_filename = f"downloaded_{int(time.time())}.pdf"
            if "." not in suggested_filename:
                suggested_filename += ".pdf"
            
            temp_path = os.path.join(TEMP_DOWNLOAD_DIR, suggested_filename)
            with open(temp_path, "wb") as f:
                f.write(response.body())
        finally:
            temp_dl_page.close()

        if not temp_path:
            raise ValueError("File failed to save to computer.")

        # Ensure it's a real PDF (handles ZIPs)
        pdf_to_analyze, reason = handle_download(temp_path)
        if not pdf_to_analyze:
            raise ValueError(reason)

        # --- Step 3 & 4: Analyze and Save (in background) ---
        # We send the PDF off to a background worker to be analyzed.
        # The worker will decide whether to save it and assign a file_id.
        BACKGROUND_EXECUTOR.submit(
            process_and_save_pdf_background, 
            pdf_to_analyze, log_data, file_id_manager,
            config, KEYWORDS_TO_FIND, KEYWORD_THRESHOLD, retry_mode, doc_ref
        )
        
        latest_action = f"Sent '{os.path.basename(pdf_to_analyze)[:20]}...' to background analyzer"
        BACKGROUND_STATE["latest_action"] = latest_action
        
    except Exception as e:
        # Something broke (e.g. website timeout, corrupted file). Record the error.
        latest_action = f"Error reading document: {log_data.get('title', 'Unknown')[:20]}..."
        BACKGROUND_STATE["latest_action"] = latest_action
        log_data.update({"status": "Failed (technical issue)", "failure_reason": str(e)})
        log_processed_entry(log_data)
    
    return latest_action, log_data

def run_scraper(config: SiteConfig, retry_mode: bool = False):
    """
    The main engine of the program. It starts the browser, goes to the search URL,
    and loops through all the pages of results.
    """
    if not OCR_AVAILABLE:
        print("--- WARNING: OCR software not found. Cannot read scanned images. ---")

    # Set up our folders and tracking spreadsheets
    permanent_storage_dir = config["permanent_storage_dir"]
    metadata_csv_file = os.path.join(permanent_storage_dir, "document_metadata.csv")
    
    os.makedirs(permanent_storage_dir, exist_ok=True)
    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    setup_csv_file(metadata_csv_file, CSV_HEADER)
    setup_csv_file(PROCESSED_LOG_CSV_FILE, PROCESSED_CSV_HEADER)
    
    # Check what we already did in the past
    processed_entries = get_processed_entries()
    
    failed_refs_to_retry = set()
    if retry_mode:
        failed_refs_to_retry = get_failed_document_references()
        if not failed_refs_to_retry:
            print("No failed documents to retry. Everything looks good!")
            return
        print(f"Retrying {len(failed_refs_to_retry)} documents that failed last time.")

    # Find out what number we should name the next file (e.g., start at 1, or continue from 45)
    initial_file_id = get_next_file_id(permanent_storage_dir, config["file_id_prefix"])
    file_id_manager = FileIdManager(initial_file_id)
    
    # Track statistics for the progress bar
    docs_processed = 0

    # Start the automated browser
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not SHOW_BROWSER,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--window-size=1920,1080'
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
            bypass_csp=True
        )
        page = context.new_page()

        # Hide the fact that this is an automated browser
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            print(f"Opening website: {config['name']}...")
            page.goto(config["search_url"], wait_until="load", timeout=60000)
        except PlaywrightTimeoutError:
            print(f"Error: The website took too long to load.")
            context.close()
            browser.close()
            return

        page_num = 1
        # Loop through pages until we run out
        while True:
            print(f"\n--- Checking Page {page_num} ---")
            
            # Wait for both network to be idle and for at least one publication item to be visible
            page.wait_for_load_state("networkidle")
            try:
                page.wait_for_selector(config["selectors"]["publication_item"], state="visible", timeout=30000)
            except PlaywrightTimeoutError:
                # It might be that there are genuinely no results, or we got blocked.
                pass

            # Find all the documents listed on this page
            publication_items = page.locator(config["selectors"]["publication_item"])
            item_count = publication_items.count()
            
            if item_count == 0:
                print("\nNo documents found on this page. (Or we were blocked from seeing them)")
                break

            # Process each document one by one
            for i in range(item_count):
                item = publication_items.nth(i)
                docs_processed += 1
                
                # Make sure the element is actually scrolled into view before trying to interact with it
                try:
                    item.scroll_into_view_if_needed(timeout=5000)
                except (PlaywrightTimeoutError, PlaywrightError) as e:
                    # Ignore the error if the element is not stable or detached
                    pass

                # Identify the document by its unique reference number
                doc_ref = ""
                ref_selector_key = "reference_title" if "reference_title" in config["selectors"] else "reference"
                ref_selector = config["selectors"][ref_selector_key]
                if item.locator(ref_selector).count() > 0:
                    doc_ref = item.locator(ref_selector).inner_text().strip()

                # Should we skip this one?
                if retry_mode:
                    if doc_ref not in failed_refs_to_retry:
                        update_progress_bar(page_num, docs_processed)
                        continue
                else:
                    check_tuple = (doc_ref, str(KEYWORDS_TO_FIND), str(KEYWORD_THRESHOLD))
                    if doc_ref and check_tuple in processed_entries:
                        update_progress_bar(page_num, docs_processed)
                        continue
                
                # Actually do the work (download and send to background to analyze)
                latest_action, log_data = process_document(page, item, file_id_manager, config, retry_mode)
                
                update_progress_bar(page_num, docs_processed)
                time.sleep(DOWNLOAD_PAUSE_S) # Brief pause to be polite to the server

            # Look for the 'Next Page' button
            next_button = page.locator(config["selectors"]["next_button"])
            if next_button.count() > 0 and next_button.is_visible() and next_button.is_enabled():
                page_num += 1
                try:
                    next_button.scroll_into_view_if_needed(timeout=5000)
                except (PlaywrightTimeoutError, PlaywrightError) as e:
                    # Ignore the error if the element is not stable or detached
                    pass
                next_button.click() # Click to go to next page
                time.sleep(PAGINATION_PAUSE_S)
            else:
                print("\n\nNo more pages. We are finished loading documents!")
                break

        print("\n\nWaiting for background text analysis to finish...")
        BACKGROUND_EXECUTOR.shutdown(wait=True)

        print("\n\n=== Script Finished ===")
        print(f"Total documents seen: {docs_processed}")
        print(f"Total PDFs opened and read: {BACKGROUND_STATE['analyzed']}")
        print(f"Total documents saved for your thesis: {BACKGROUND_STATE['saved']}")
        context.close()
        browser.close()

if __name__ == "__main__":
    # If the user typed '--retry-failed' in the terminal, run in retry mode
    is_retry_mode = "--retry-failed" in sys.argv
    for config_key in CONFIG_KEYS:
        active_config = load_config(CONFIG_FILE, config_key)

        if active_config:
            if is_retry_mode:
                print(f"--- Running RETRY mode for {active_config['name']} ---")
                run_scraper(active_config, retry_mode=True)
            else:
                print(f"--- Running NORMAL mode for {active_config['name']} ---")
                run_scraper(active_config, retry_mode=False)
        else:
            continue