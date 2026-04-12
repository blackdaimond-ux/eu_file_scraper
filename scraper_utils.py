"""
Shared utilities for the thesis file scrapers.
This file contains all the functions that are used by both the 
European Commission and European Council scrapers.
"""
import os
import re
import time
import csv
import json
import zipfile
import shutil
import sys
import concurrent.futures
import threading
from datetime import datetime
from typing import Dict, Tuple, Optional, Set, TypedDict
import pandas as pd
from filelock import FileLock
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError, Locator

# Libraries for reading PDFs
import pdfplumber
try:
    # Libraries for reading scanned images (OCR)
    import pytesseract
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ==========================================
# --- ADVANCED SETTINGS (Usually leave alone) ---
# ==========================================
TEMP_DOWNLOAD_DIR = "outputs/temp_downloads"
PROCESSED_LOG_CSV_FILE = "outputs/processed_documents_log.csv"
DOWNLOAD_PAUSE_S = 0.3 # Pause between downloads (seconds)
PAGINATION_PAUSE_S = 3 # Pause between pages (seconds)
MIN_CHARS_FOR_OCR_FALLBACK = 100 # If a PDF has fewer characters than this, try OCR

# Columns for our data spreadsheets
CSV_HEADER = [
    "file_id", "title", "publication_date", "document_url",
    "source_page_url", "document_reference", "total_keyword_count", "keyword_counts"
]
PROCESSED_CSV_HEADER = [
    "processed_timestamp", "document_reference", "keywords_used", "keyword_threshold", "status",
    "file_id", "title", "publication_date", "document_url", "source_page_url",
    "total_mentions", "keyword_counts", "failure_reason"
]

# We use this to run the background tasks without blocking the main scraper
BACKGROUND_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)

class FileIdManager:
    """A thread-safe counter to manage unique file IDs."""
    def __init__(self, initial_id: int):
        self._next_id = initial_id
        self._lock = threading.Lock()

    def get_next_id(self) -> int:
        """Get the current ID and atomically increment for the next caller."""
        with self._lock:
            current_id = self._next_id
            self._next_id += 1
            return current_id

class SiteConfig(TypedDict):
    name: str
    base_url: str
    search_url: str
    permanent_storage_dir: str
    file_id_prefix: str
    selectors: Dict[str, str]

def load_config(config_path: str, config_key: Optional[str] = None) -> Optional[SiteConfig]:
    """Loads the settings for the website we are scraping from the JSON file."""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            if config_key:
                config = config[config_key]
        return config
    except FileNotFoundError:
        print(f"Error: Configuration file not found at '{config_path}'")
        return None
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from '{config_path}'")
        return None

def get_processed_entries() -> Set[Tuple[str, str, str]]:
    """
    Looks at the log file to see which documents we have already downloaded and checked.
    This prevents the script from doing the same work twice if you stop and restart it.
    """
    if not os.path.exists(PROCESSED_LOG_CSV_FILE):
        return set()
    
    processed_entries = set()
    try:
        with FileLock(f"{PROCESSED_LOG_CSV_FILE}.lock"):
            with open(PROCESSED_LOG_CSV_FILE, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Remember any document that has been processed, regardless of status
                    if all(k in row for k in ['document_reference', 'keywords_used', 'keyword_threshold']):
                        entry = (row['document_reference'], row['keywords_used'], row['keyword_threshold'])
                        processed_entries.add(entry)
    except (FileNotFoundError, KeyError, Exception) as e:
        print(f"Warning: Could not read log. Will process all items. Error: {e}")
        return set()
    print(f"Loaded {len(processed_entries)} previously checked documents to skip.")
    return processed_entries

def get_next_file_id(storage_dir: str, prefix: str) -> int:
    """Finds the highest existing file number (e.g., council_005.pdf) and returns the next one (6)."""
    if not os.path.exists(storage_dir):
        return 1
    max_id = 0
    regex = re.compile(rf"{re.escape(prefix)}_(\d+)\.pdf")
    for filename in os.listdir(storage_dir):
        match = regex.match(filename)
        if match:
            file_id = int(match.group(1))
            if file_id > max_id:
                max_id = file_id
    return max_id + 1

# We need a lock so that multiple background tasks don't try to write to the CSV file at the same exact time
CSV_WRITE_LOCK = threading.Lock()

def setup_csv_file(filepath: str, header: list):
    """Creates a new empty spreadsheet file and writes the column names at the top."""
    # We use both a thread lock (for within this script) and a file lock (for across multiple scripts)
    with CSV_WRITE_LOCK, FileLock(f"{filepath}.lock"):
        if not os.path.exists(filepath):
            dir_name = os.path.dirname(filepath)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(header)
            print(f"Created data file: {filepath}")

def append_to_csv(filepath: str, data: dict, header: list):
    """Adds a new row of data to the bottom of the spreadsheet."""
    with CSV_WRITE_LOCK, FileLock(f"{filepath}.lock"):
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writerow(data)

def log_processed_entry(data: dict):
    """Records what happened with a document (saved, rejected, failed) into the master log."""
    full_data = {key: data.get(key, "") for key in PROCESSED_CSV_HEADER}
    full_data["processed_timestamp"] = datetime.now().isoformat()
    append_to_csv(PROCESSED_LOG_CSV_FILE, full_data, PROCESSED_CSV_HEADER)

def analyze_pdf(pdf_path: str, keywords_to_find: list) -> Tuple[int, Dict[str, int], Optional[str], str]:
    """
    This is the core analysis function!
    It opens the PDF, reads all the text inside it, and counts how many times
    our keywords appear. It processes multiple pages at the same time to be faster.
    """
    keyword_counts = {key: 0 for key in keywords_to_find}
    total_count = 0
    full_text = ""
    error_reason = None

    def extract_page_text(page):
        """Helper to read text from one single page."""
        try:
            return page.extract_text()
        except Exception:
            return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            # We already run analyze_pdf in the background, so doing ThreadPoolExecutor inside here 
            # might spawn too many threads. For simplicity we'll just read pages sequentially if we 
            # are already in a background thread, to avoid overwhelming the CPU.
            
            # Combine all the pages into one giant block of text
            full_text = "\n".join(filter(None, (extract_page_text(p) for p in pdf.pages)))
    except Exception as e:
        error_reason = f"Could not read PDF: {e}"

    # If we found almost no text, the PDF might be a scanned image.
    # We will try to 'read' the image using OCR (Optical Character Recognition).
    if len(full_text) < MIN_CHARS_FOR_OCR_FALLBACK and OCR_AVAILABLE and not error_reason:
        # print("Document looks like a scan. Trying to read image (OCR) in the background...")
        try:
            images = convert_from_path(pdf_path)
            ocr_text = ""
            for i, image in enumerate(images):
                ocr_text += pytesseract.image_to_string(image) + "\n"
            full_text = ocr_text
        except Exception as e:
            error_reason = f"Image reading (OCR) failed: {e}"

    # Now that we have the text, let's search for our keywords!
    if full_text and not error_reason:
        for keyword in keywords_to_find:
            # Look for the exact word (ignoring upper/lower case). 
            # The (?:s|es)? handles basic plurals automatically.
            matches = re.findall(r'\b' + re.escape(keyword) + r'(?:s|es)?\b', full_text, re.IGNORECASE)
            count = len(matches)
            keyword_counts[keyword] += count
            total_count += count
    elif not full_text and not error_reason:
        error_reason = "The document was completely blank."
            
    return total_count, keyword_counts, error_reason, full_text

def handle_download(temp_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Checks the downloaded file. Sometimes websites give us a .zip folder 
    instead of a .pdf directly. This function unpacks the folder to find the real PDF.
    """
    if temp_path.lower().endswith(".zip"):
        extract_folder = os.path.join(TEMP_DOWNLOAD_DIR, os.path.basename(temp_path) + "_extracted")
        os.makedirs(extract_folder, exist_ok=True)
        
        try:
            with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                zip_ref.extractall(extract_folder)
        except zipfile.BadZipFile:
            shutil.rmtree(extract_folder)
            os.remove(temp_path)
            return None, "File was a broken ZIP archive"

        os.remove(temp_path)

        main_pdf_path = None
        # Look through the unzipped folder for the main PDF
        for root, _, files in os.walk(extract_folder):
            for file in files:
                if "main" in file.lower() and file.lower().endswith(".pdf"):
                    main_pdf_path = os.path.join(root, file)
                    break
            if main_pdf_path:
                break
        
        if not main_pdf_path:
            shutil.rmtree(extract_folder)
            return None, "Could not find a main PDF inside the ZIP"

        # Move the found PDF out to our temporary folder
        final_pdf_path = os.path.join(TEMP_DOWNLOAD_DIR, os.path.basename(main_pdf_path))
        shutil.move(main_pdf_path, final_pdf_path)
        shutil.rmtree(extract_folder) # Clean up the messy unzipped folder
        return final_pdf_path, None

    elif temp_path.lower().endswith(".pdf"):
        return temp_path, None
    else:
        os.remove(temp_path)
        return None, f"Website gave us a weird file type: {os.path.basename(temp_path)}"

# Lock for console output so progress prints don't get jumbled
PRINT_LOCK = threading.Lock()

# Global state to keep track of background task progress
BACKGROUND_STATE = {
    "analyzed": 0,
    "saved": 0,
    "latest_action": "Starting..."
}

def update_progress_bar(page: int, processed: int, state: dict = None):
    """Refreshes the status line at the bottom of your screen."""
    if state is None:
        state = BACKGROUND_STATE
        
    progress_text = (
        f"\rPage: {page} | Checked: {processed} | Analyzed: {state['analyzed']} | Saved: {state['saved']} | Status: {state['latest_action'][:50]:<50}"
    )
    with PRINT_LOCK:
        sys.stdout.write("\r" + " " * 150) # Clear line
        sys.stdout.write(f"\r{progress_text}")
        sys.stdout.flush()

def get_failed_document_references() -> Set[str]:
    """Finds documents that had an error last time so we can try them again."""
    if not os.path.exists(PROCESSED_LOG_CSV_FILE):
        return set()
    
    failed_refs = set()
    try:
        with FileLock(f"{PROCESSED_LOG_CSV_FILE}.lock"):
            df = pd.read_csv(PROCESSED_LOG_CSV_FILE)
            failed_rows = df[df['status'].str.contains("Failed", na=False)]
            failed_refs = set(failed_rows['document_reference'].unique())
    except Exception as e:
        print(f"Error reading log: {e}")
    return failed_refs

def cleanup_failed_log_entries(document_reference: str):
    """If a previously failed document succeeds this time, remove the old error from the log."""
    if not os.path.exists(PROCESSED_LOG_CSV_FILE):
        return
    
    with CSV_WRITE_LOCK, FileLock(f"{PROCESSED_LOG_CSV_FILE}.lock"):
        try:
            df = pd.read_csv(PROCESSED_LOG_CSV_FILE)
            mask = ~((df['document_reference'] == document_reference) & (df['status'].str.contains("Failed", na=False)))
            cleaned_df = df[mask]
            cleaned_df.to_csv(PROCESSED_LOG_CSV_FILE, index=False)
        except Exception as e:
            print(f"Warning: Could not clean up failed log entry for '{document_reference}'. Error: {e}")


def process_and_save_pdf_background(pdf_path: str, log_data: dict, file_id_manager: "FileIdManager", config: SiteConfig, keywords_to_find: list, keyword_threshold: int, is_retry_mode: bool, document_reference: str):
    """
    This function runs in the background. It reads the PDF (and performs OCR if needed),
    counts the keywords, and then either saves or deletes the file.
    """
    try:
        total_keywords, keyword_details, reason, full_text = analyze_pdf(pdf_path, keywords_to_find)
        log_data.update({
            "total_mentions": total_keywords,
            "total_keyword_count": total_keywords,
            "keyword_counts": str(keyword_details)
        })
        
        if reason:
            raise ValueError(reason)

        if total_keywords >= keyword_threshold:
            # Get the next available file ID ONLY when we are sure we're saving it.
            file_id = file_id_manager.get_next_id()
            file_id_str = f"{config['file_id_prefix']}_{file_id:03d}"
            
            # --- GOOGLE CLOUD DOCUMENT TRANSLATION ---
            detected_lang = "en"
            translated_temp_path = None
            google_project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
            
            # To use Google Cloud Translation, you typically need to authenticate via
            # the GOOGLE_APPLICATION_CREDENTIALS environment variable pointing to your JSON key file.
            
            if google_project_id and full_text and full_text.strip():
                try:
                    from google.cloud import translate
                    # Create a client.
                    client = translate.TranslationServiceClient()
                    
                    location = "global"
                    parent = f"projects/{google_project_id}/locations/{location}"

                    # 1. Detect source language using a text snippet
                    snippet = full_text[:1000]
                    # We pass the snippet in a list
                    response = client.detect_language(
                        content=snippet,
                        parent=parent,
                        mime_type="text/plain",
                    )
                    
                    # Get the most likely language
                    if response.languages:
                        detected_lang = response.languages[0].language_code.lower()
                    
                    # 2. If not English, translate the entire document
                    # Note: Google Cloud's translate_document requires the document content
                    # as bytes or via Cloud Storage. We will read the local file bytes.
                    if detected_lang != "en" and not detected_lang.startswith("en"):
                        BACKGROUND_STATE["latest_action"] = f"Translating {detected_lang.upper()} document..."
                        translated_temp_path = pdf_path.replace(".pdf", "_translated_temp.pdf")
                        
                        with open(pdf_path, "rb") as document:
                            document_content = document.read()

                        document_input_config = {
                            "content": document_content,
                            "mime_type": "application/pdf",
                        }

                        translate_response = client.translate_document(
                            request={
                                "parent": parent,
                                "target_language_code": "en-US",
                                "source_language_code": detected_lang,
                                "document_input_config": document_input_config,
                            }
                        )

                        # Write the translated document bytes to our temp file
                        with open(translated_temp_path, "wb") as f_out:
                            f_out.write(translate_response.document_translation.byte_stream_outputs[0])
                            
                except ImportError:
                    print("\nWarning: 'google-cloud-translate' library is not installed. Run 'pip install google-cloud-translate' to enable translation.")
                except Exception as e:
                    print(f"\nWarning: Google Cloud Translation failed for {pdf_path}. Error: {e}")
                    # Ensure we don't crash, clean up temp file if it was partially created
                    if translated_temp_path and os.path.exists(translated_temp_path):
                        os.remove(translated_temp_path)
                    translated_temp_path = None
            # --- END TRANSLATION ---

            # Save Original File
            if detected_lang != "en" and not detected_lang.startswith("en") and translated_temp_path:
                original_filename = f"{file_id_str}_{detected_lang}.pdf"
            else:
                original_filename = f"{file_id_str}.pdf"
                
            final_path = os.path.join(config['permanent_storage_dir'], original_filename)
            shutil.move(pdf_path, final_path) # Move original to final folder
            
            # Save Translated File if it was created
            if translated_temp_path and os.path.exists(translated_temp_path):
                translated_filename = f"{file_id_str}_en.pdf"
                final_translated_path = os.path.join(config['permanent_storage_dir'], translated_filename)
                shutil.move(translated_temp_path, final_translated_path)

            BACKGROUND_STATE["latest_action"] = f"Saved: {os.path.basename(final_path)}"
            log_data.update({"status": "Saved", "file_id": file_id_str})
            
            # Record it in our final spreadsheet
            metadata_to_write = {k: log_data.get(k) for k in CSV_HEADER}
            append_to_csv(os.path.join(config['permanent_storage_dir'], "document_metadata.csv"), metadata_to_write, CSV_HEADER)
            
            # Update our global state safely
            with CSV_WRITE_LOCK:
                BACKGROUND_STATE["saved"] += 1
        else:
            # Not enough keywords. Delete the file to save space.
            os.remove(pdf_path)
            BACKGROUND_STATE["latest_action"] = f"Rejected (Only {total_keywords} keywords)"
            log_data["status"] = "Rejected (low keywords)"

        with CSV_WRITE_LOCK:
            BACKGROUND_STATE["analyzed"] += 1

        # Clean up error logs if it succeeded this time
        if is_retry_mode and document_reference and "Failed" not in log_data["status"]:
            cleanup_failed_log_entries(document_reference)

        # Remember that we finished this document
        log_processed_entry(log_data)
        
    except Exception as e:
        BACKGROUND_STATE["latest_action"] = f"Error reading document: {log_data.get('title', 'Unknown')[:20]}..."
        log_data.update({"status": "Failed (technical issue)", "failure_reason": str(e)})
        log_processed_entry(log_data)
        if os.path.exists(pdf_path):
             os.remove(pdf_path)
