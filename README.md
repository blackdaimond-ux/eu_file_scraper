# Thesis File Scraper

This tool automates the process of finding, downloading, and analyzing PDF documents from government websites (specifically the European Commission and the European Council). It's designed to help you quickly gather documents that mention specific keywords relevant to your thesis.

## What Does It Do?
1. **Navigates** to a search results page on the target website.
2. **Scans** through the list of documents.
3. **Downloads** each PDF it finds.
4. **Reads** the text inside the PDF. If the PDF is a scanned image, it will try to use Optical Character Recognition (OCR) to read the text.
5. **Counts** how many times your specific keywords appear in the document.
6. **Saves** the document *only if* it meets a minimum keyword count threshold. It renames the file neatly (e.g., `council_001.pdf`).
7. **Logs** all metadata (title, date, link, keyword counts) into a CSV spreadsheet so you can easily review what was found.
8. **Remembers** what it has already checked so it doesn't download the same document twice if you run the script again.

## How to Set It Up (First Time Installation)

### 1. Download the Project
First, download or clone this project to your computer. 
Open your terminal (Command Prompt on Windows, Terminal on Mac/Linux) and navigate to the project folder:
```bash
cd path/to/hyanne_file_scraper
```

### 2. Install Prerequisites using `uv`
This project uses `uv` to manage the required Python packages quickly and cleanly.
If you don't have `uv` installed, you can install it using pip:
```bash
pip install uv
```

Once `uv` is ready, create the virtual environment and install all dependencies by running:
```bash
uv sync
```
This will read the `pyproject.toml` file and set up a `.venv` folder containing all the tools the scraper needs (like `playwright`, `pdfplumber`, etc.).

### 3. Activate the Virtual Environment
Before running the scripts, you must tell your terminal to use the newly installed packages. 
*   **Windows:**
    ```bash
    .venv\Scripts\activate
    ```
*   **Mac/Linux:**
    ```bash
    source .venv/bin/activate
    ```
*(You will know it worked if you see `(.venv)` appear at the beginning of your terminal line).*

### 4. Install Browser Automation Tools
This script uses a tool called Playwright to simulate a real web browser. Run this command once to install the necessary browsers:
```bash
playwright install
```

### 5. Install OCR Engine (Optional, but recommended)
If you want the script to be able to read scanned documents (images of text), you need to install Tesseract-OCR.
*   **Windows:** Download the installer from [here](https://github.com/UB-Mannheim/tesseract/wiki) and install it. Make sure to check the option to add Tesseract to your system PATH during installation.
*   **Mac:** `brew install tesseract`
*   **Linux:** `sudo apt install tesseract-ocr`

---

## How to Use It

There are two main scripts, one for each website:
1.  `main_ec_scalper.py`: For the European Commission.
2.  `main_council_scalper.py`: For the European Council.

### Step 1: Set Your Keywords
Open the script you want to run (e.g., `main_ec_scalper.py`) in a text editor. Look for the "General Configuration" section near the top.

Change the `KEYWORDS_TO_FIND` list to the words you are looking for in your thesis.
Change `KEYWORD_THRESHOLD` to the minimum number of times *any* keyword must appear for the document to be saved.

```python
# --- General Configuration ---
# CHANGE THESE WORDS TO YOUR THESIS TOPICS!
KEYWORDS_TO_FIND = ["semiconductor", "chip", "integrated circuit", "microprocessor", "cpu"]

# How many times must a keyword appear before we save the file? (1 means at least once)
KEYWORD_THRESHOLD = 1
```

### Step 2: Configure the Search URL
The script needs to know where to start searching. This is set in the configuration JSON files (`ec_config.json` and `council_config.json`).

1. Go to the target website (e.g., the EU Commission document register) in your normal web browser.
2. Perform the exact search you want (enter keywords, select dates, document types, etc.).
3. Copy the URL from your browser's address bar.
4. Paste that URL into the `"search_url"` field in the corresponding JSON configuration file.

### Step 3: Run the Script
Make sure your virtual environment is activated (Step 3 above). Then run the script:

```bash
python main_ec_scalper.py
```
or
```bash
python main_council_scalper.py
```

A browser window will pop up, and you'll see the script navigating the site and downloading files automatically. You can see the progress in your terminal window.

You can safely run both scripts at the same time in two different terminal windows!

### Where are my files?
*   **Downloaded PDFs:** The scripts will create folders named `ec_official_documents` or `council_official_documents`. Your filtered PDFs will be saved here.
*   **Data Spreadsheet:** Inside those folders, you will find a file called `document_metadata.csv`. Open this in Excel or Numbers to see a list of all saved documents, their titles, dates, original links, and exactly how many times your keywords were found.
*   **Master Log:** A file named `processed_documents_log.csv` is created in the main folder. This keeps track of *everything* the script has looked at, including documents it rejected for not having enough keywords.

## Troubleshooting

*   **The script stopped unexpectedly:** Sometimes websites change or load slowly. Just run the script again! It remembers what it has already processed and will pick up where it left off (or skip the ones it already checked).
*   **A document failed to download:** Sometimes servers have temporary errors. You can force the script to retry *only* the documents that failed previously by running it with a special flag:
    ```bash
    python main_ec_scalper.py --retry-failed
    ```