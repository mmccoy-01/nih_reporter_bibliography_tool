# NIH RePORTER & PubMed Bibliography Pipeline

## What this tool does

This repository contains a single Python script with **two modes**:

### Mode 1 — NIH RePORTER scraper (Similar Projects + Publications → CSV)
- Start from a **seed NIH RePORTER project URL**
- Scrape the **Similar Projects** table (sorted by Match Score)
- For each similar project, scrape the **Publications** table
- Output a tidy, long-format CSV: `similar_projects_long.csv`

### Mode 2 — Bibliography export (PMIDs in a CSV → MEDLINE / RIS / BibTeX)
- Provide a **local .csv file** that contains a PMID column (auto-detected)
- Export citations in:
  - **MEDLINE** (`pubmed_medline.txt`) — best for EndNote bulk import
  - **RIS** (`pubmed.ris`) — universal format (Zotero/Mendeley/EndNote)
  - **BibTeX** (`pubmed.bib`) — good for LaTeX workflows

---

## 🧰 Requirements (IDLE / No Command Line)

### 1️⃣ Install Python

Download Python from:
👉 [https://www.python.org/downloads/](https://www.python.org/downloads/)

During installation on **Windows**, make sure to:

* ✅ Check **“Add Python to PATH”**
* ✅ Install **pip** (default option)

After installation, you should be able to open **IDLE (Python 3.x)** from the Start menu.

---

### 2️⃣ Install required Python packages (from IDLE)

Open **IDLE**, then in the Python shell run:

```python
import sys, subprocess

subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "requests"])
```

If this finishes without errors, the packages are installed correctly.

---

### 3️⃣ Install the Playwright browser (from IDLE)

Still in **IDLE**, run:

```python
import sys, subprocess

subprocess.check_call([sys.executable, "-m", "playwright", "install"])
```

This will download **Chromium** (required).
You should see download progress — this only needs to be done **once**.

---

## ▶️ How to Run

From the directory containing the script:

```bash
python nih_similar_and_pubs.py
```

The script will:

1. Load the **Similar Projects** table for the seed NIH project
2. Tell you how many similar projects were found
3. Prompt you:

>How many of the MOST similar projects do you want? (1–N):

⏱ **Runtime estimate**  
~5.5 minutes per 25 projects (≈13 seconds per project), depending on network speed.

---

## 🔍 What the Script Handles Automatically

- ✅ NIH RePORTER’s **two-row publication layout** (title row + citation row)
- ✅ Merges publication title + year + PubMed URL into a **single row**
- ✅ Removes duplicate / journal-only rows
- ✅ Handles projects with **zero publications**
- ✅ Preserves **exact funding text** as shown on NIH RePORTER
- ✅ Sorts similar projects by **Match Score (descending)**

---

## 🧪 Example Use in R

```r
df <- read.csv("similar_projects_long.csv")

# number of publications per similar project
df |>
  dplyr::filter(pub_url != "") |>
  dplyr::count(similar_appl_id)

# funding vs similarity
df |>
  dplyr::distinct(similar_appl_id, Match.Score, Fiscal.Year)
```

---

## ⚠️ Notes & Limitations

- The script **scrapes the NIH RePORTER website**, not just the API, to match  
    _exactly what the UI shows_.
- NIH RePORTER uses dynamic loading; scraping speed is intentionally throttled  
    to avoid errors.
- Publication metadata is taken **as displayed** (journal strings are not normalized).
    

---

## 📜 Disclaimer

This tool is for **research and analysis purposes** only.  
Please respect NIH RePORTER’s terms of use and avoid excessive automated requests.

---

## 🙌 Acknowledgments

NIH RePORTER: [https://reporter.nih.gov/](https://reporter.nih.gov/)
