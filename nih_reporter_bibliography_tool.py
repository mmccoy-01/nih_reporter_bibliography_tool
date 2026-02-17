"""
NIH RePORTER + PubMed bibliography pipeline (single script)

Mode 1:
  - Paste a NIH RePORTER seed project URL (project-details/########)
  - Scrape the Similar Projects table (top N by Match Score)
  - For each similar project, scrape the Publications table
  - Write a tidy long CSV: similar_projects_long.csv

Mode 2:
  - Provide a CSV that contains PMIDs (auto-detects PMID column)
  - Export bibliography as MEDLINE (.txt), RIS (.ris), or BibTeX (.bib)

Runs fine from IDLE (no command line required).
"""

import os
import re
import time
import csv
import urllib.parse
import urllib.request
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# GLOBAL CONFIG
# ============================================================

HEADLESS = True
PAGE_TIMEOUT_MS = 120_000

OUT_CSV = "similar_projects_long.csv"

# Polite delays
NCBI_DELAY_S = 0.34
CTXp_DELAY_S = 0.40

# NIH project number examples: 1R21DE034541-01A1, 5R01AR082809-03, etc.
PROJECT_NUM_RE = re.compile(r"\b\d?[A-Z]{1,3}\d{2}[A-Z]{2}\d{6}(?:-\d{2}[A-Z0-9]{0,3})?\b")


# ============================================================
# MENU
# ============================================================

def prompt_mode() -> str:
    print("\nSelect an option:\n")
    print("1 = Create NIH RePORTER similar projects CSV")
    print("2 = Create bibliography file (MEDLINE, RIS, or BibTeX) from a CSV containing PMIDs\n")
    while True:
        c = input("Enter 1 or 2: ").strip()
        if c in ("1", "2"):
            return c
        print("Please enter 1 or 2.")


# ============================================================
# COMMON HELPERS
# ============================================================

def clean_text(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s


def parse_int(text: str) -> str:
    t = clean_text(text)
    m = re.search(r"(\d+)", t)
    return m.group(1) if m else ""


def parse_year_from_text(s: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", s or "")
    return m.group(0) if m else ""


def extract_project_number(text: str) -> str:
    if not text:
        return ""
    m = PROJECT_NUM_RE.search(text.replace("\n", " "))
    return m.group(0) if m else ""


def normalize_pi(text: str) -> str:
    t = clean_text(text)
    t = re.sub(r"\s*Principal Investigator\(s\)\s*/\s*Project Leader\(s\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*Principal Investigator\(s\)\s*/\s*Project Leader\(s\).*?$", "", t, flags=re.IGNORECASE)
    return t.strip()


def extract_appl_id_from_url(url: str) -> Optional[int]:
    if not url:
        return None
    m = re.search(r"/project-details/(\d+)", url)
    return int(m.group(1)) if m else None


def extract_pmid_from_pubmed_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
    return m.group(1) if m else ""


def prompt_for_top_n(max_n: int) -> int:
    print("\nScrape sizing note:")
    print("  • Based on prior runs: ~5.5 minutes per 25 projects (≈13 seconds per project).")
    print(f"  • There are {max_n} similar projects available from the site table.\n")
    while True:
        raw = input(f"How many of the MOST similar projects do you want? (1–{max_n}): ").strip()
        try:
            n = int(raw)
            if 1 <= n <= max_n:
                return n
        except Exception:
            pass
        print(f"Please enter an integer between 1 and {max_n}.")


def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def safe_goto(page, url: str):
    """
    NIH RePORTER sometimes never reaches 'networkidle' due to background requests.
    Use domcontentloaded instead.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(800)


def find_table_element_by_header(page, required_headers: List[str]):
    """
    Returns an ElementHandle for a table whose innerText includes all required header labels.
    IMPORTANT: evaluate_handle can return a JSHandle to null; convert to ElementHandle and check for None.
    """
    js = """
    (requiredHeaders) => {
      const tables = Array.from(document.querySelectorAll("table"));
      const norm = (s) => (s || "").toLowerCase().replace(/\\s+/g, " ").trim();
      const req = requiredHeaders.map(norm);
      for (const t of tables) {
        const txt = norm(t.innerText || "");
        if (req.every(h => txt.includes(h))) return t;
      }
      return null;
    }
    """
    h = page.evaluate_handle(js, required_headers)
    return h.as_element()  # None if JS returned null


# ============================================================
# MODE 2: BIBLIOGRAPHY EXPORT FROM CSV (AUTO PMID + FORMAT CHOICE)
# ============================================================

def detect_pmid_column(csv_path: str) -> str:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    preferred = ["pub_pmid", "PMID", "pmid", "pubmed_id", "pubmedid", "PubMedID"]
    for name in preferred:
        if name in headers:
            print(f"\nDetected PMID column by name: {name}")
            return name

    scores: Dict[str, int] = {}
    for col in headers:
        score = 0
        for row in rows:
            v = str(row.get(col, "")).strip()
            if v.isdigit() and 6 <= len(v) <= 9:
                score += 1
        scores[col] = score

    if not scores:
        raise RuntimeError("CSV has no headers/columns.")

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        print("\nCould not auto-detect a PMID column.")
        print("Available columns:", headers)
        return input("Enter PMID column manually: ").strip()

    print(f"\nDetected PMID column based on data: {best} (PMID-like rows: {scores[best]})")
    ans = input("Use this column? (Y/n): ").strip().lower()
    if ans == "n":
        print("Available columns:", headers)
        return input("Enter PMID column manually: ").strip()
    return best


def extract_pmids_from_csv_auto(csv_path: str) -> List[str]:
    pmid_col = detect_pmid_column(csv_path)

    pmids: List[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            v = str(row.get(pmid_col, "")).strip()
            if v.isdigit():
                pmids.append(v)

    # de-dupe preserving order
    seen = set()
    out: List[str] = []
    for p in pmids:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def prompt_biblio_format() -> str:
    print("\nChoose export format:")
    print("  1 = MEDLINE (.txt)  [best for EndNote; fast batch export]")
    print("  2 = RIS (.ris)      [universal; good for Zotero/Mendeley/EndNote]")
    print("  3 = BibTeX (.bib)   [best for LaTeX; generated by converting RIS]\n")
    while True:
        c = input("Enter 1, 2, or 3: ").strip()
        if c == "1":
            return "medline"
        if c == "2":
            return "ris"
        if c == "3":
            return "bibtex"
        print("Please enter 1, 2, or 3.")


def chunks(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


def fetch_efetch_medline(pmids: List[str], out_path: str, batch_size: int = 200):
    print(f"\nExporting MEDLINE to {out_path}")
    with open(out_path, "w", encoding="utf-8") as out:
        for i, batch in enumerate(chunks(pmids, batch_size), start=1):
            ids = ",".join(batch)
            url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
                + urllib.parse.urlencode({
                    "db": "pubmed",
                    "id": ids,
                    "rettype": "medline",
                    "retmode": "text",
                })
            )
            print(f"Fetching batch {i} ({len(batch)} PMIDs)...")
            with urllib.request.urlopen(url) as r:
                txt = r.read().decode("utf-8", errors="replace")
            out.write(txt)
            out.write("\n")
            time.sleep(NCBI_DELAY_S)


def fetch_ctxp(pmids: List[str], out_path: str, fmt: str, batch_size: int = 50):
    print(f"\nExporting {fmt.upper()} to {out_path}")
    base = "https://pmc.ncbi.nlm.nih.gov/api/ctxp/v1/pubmed/"
    with open(out_path, "w", encoding="utf-8") as out:
        for i, batch in enumerate(chunks(pmids, batch_size), start=1):
            ids = ",".join(batch)
            url = base + "?" + urllib.parse.urlencode({"format": fmt, "id": ids})
            print(f"Fetching batch {i} ({len(batch)} PMIDs)...")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as r:
                txt = r.read().decode("utf-8", errors="replace")
            out.write(txt)
            if not txt.endswith("\n"):
                out.write("\n")
            time.sleep(CTXp_DELAY_S)


def ris_to_bibtex(ris_text: str) -> str:
    entries: List[Dict[str, List[str]]] = []
    current: Dict[str, List[str]] = {}

    def flush():
        nonlocal current
        if current:
            entries.append(current)
            current = {}

    for line in ris_text.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("ER  -"):
            flush()
            continue
        m = re.match(r"^([A-Z0-9]{2})\s{2}-\s(.*)$", line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        current.setdefault(tag, []).append(val)

    flush()

    def esc(s: str) -> str:
        return s.replace("{", "\\{").replace("}", "\\}")

    out: List[str] = []

    for e in entries:
        title = (e.get("TI", [""])[0] if e.get("TI") else "") or (e.get("T1", [""])[0] if e.get("T1") else "")
        year = ""
        for tag in ("PY", "Y1"):
            if e.get(tag):
                m = re.search(r"(19|20)\d{2}", e[tag][0])
                if m:
                    year = m.group(0)
                    break

        authors = e.get("AU", []) or e.get("A1", [])
        journal = (e.get("JO", [""])[0] if e.get("JO") else "") or (e.get("JF", [""])[0] if e.get("JF") else "")
        volume = (e.get("VL", [""])[0] if e.get("VL") else "")
        number = (e.get("IS", [""])[0] if e.get("IS") else "")
        sp = (e.get("SP", [""])[0] if e.get("SP") else "")
        ep = (e.get("EP", [""])[0] if e.get("EP") else "")
        pages = f"{sp}-{ep}" if (sp and ep) else (sp or "")
        doi = (e.get("DO", [""])[0] if e.get("DO") else "")
        url = (e.get("UR", [""])[0] if e.get("UR") else "")
        pmid = (e.get("AN", [""])[0] if e.get("AN") else "")
        if pmid:
            pmid = re.sub(r"\D", "", pmid)

        key = "pmid" + pmid if pmid else (re.sub(r"\W+", "", (authors[0].split(",")[0] if authors else "ref")) + (year or ""))

        bib: List[str] = []
        bib.append(f"@article{{{key},")
        if authors:
            bib.append(f"  author = {{{esc(' and '.join(authors))}}},")
        if title:
            bib.append(f"  title = {{{esc(title)}}},")
        if journal:
            bib.append(f"  journal = {{{esc(journal)}}},")
        if year:
            bib.append(f"  year = {{{year}}},")
        if volume:
            bib.append(f"  volume = {{{esc(volume)}}},")
        if number:
            bib.append(f"  number = {{{esc(number)}}},")
        if pages:
            bib.append(f"  pages = {{{esc(pages)}}},")
        if doi:
            bib.append(f"  doi = {{{esc(doi)}}},")
        if url:
            bib.append(f"  url = {{{esc(url)}}},")
        if pmid:
            bib.append(f"  note = {{PMID: {esc(pmid)}}},")
        bib.append("}\n")
        out.append("\n".join(bib))

    return "\n".join(out)


def prompt_csv_path() -> str:
    while True:
        p = input("\nEnter path to CSV file: ").strip().strip('"')

        # common mistake: user pastes a URL
        if p.lower().startswith(("http://", "https://")):
            print("Invalid input: that looks like a web link. Please paste a LOCAL .csv file path.")
            continue

        if not p.lower().endswith(".csv"):
            print("Invalid file type: please provide a .csv file.")
            continue

        if not os.path.isfile(p):
            print("CSV not found: please check the file path and try again.")
            continue

        return p


def create_bibliography_from_csv():
    csv_path = prompt_csv_path()

    try:
        pmids = extract_pmids_from_csv_auto(csv_path)
    except Exception:
        # Catch any parsing issues (bad headers, unreadable file, etc.)
        print("Could not read that CSV (format/encoding issue). Please try a different CSV file.")
        return

    if not pmids:
        print("No PMIDs found in that CSV. Please check the file and try again.")
        return

    print(f"\nLoaded {len(pmids)} unique PMIDs")

    fmt = prompt_biblio_format()

    if fmt == "medline":
        out_path = "pubmed_medline.txt"
        fetch_efetch_medline(pmids, out_path, batch_size=200)
        print(f"\nDone. Wrote {out_path}")
        return

    if fmt == "ris":
        out_path = "pubmed.ris"
        fetch_ctxp(pmids, out_path, fmt="ris", batch_size=50)
        print(f"\nDone. Wrote {out_path}")
        return

    # bibtex (convert from RIS)
    ris_path = "_tmp_pubmed.ris"
    fetch_ctxp(pmids, ris_path, fmt="ris", batch_size=50)
    with open(ris_path, "r", encoding="utf-8") as f:
        ris_text = f.read()

    bib_text = ris_to_bibtex(ris_text)
    out_path = "pubmed.bib"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(bib_text)

    print(f"\nDone. Wrote {out_path}")
    print("(Note: BibTeX is generated from RIS; for edge cases you may want to spot-check a few entries.)")


# ============================================================
# MODE 1: NIH REPORTER SCRAPER (ROBUST: no networkidle)
# ============================================================

OUTPUT_COLS = [
    "seed_appl_id",
    "similar_appl_id",
    "project_url",
    "Match Score",
    "Project Title",
    "Project Number",
    "Admin IC",
    "FY Total Cost by IC",
    "Fiscal Year",
    "Funding IC",
    "Organization",
    "Principal Investigator(s)/Project Leader(s)",
    "pub_title",
    "pub_year",
    "pub_url",
    "pub_pmid",
]


def prompt_seed_url() -> str:
    while True:
        url = input("\nPaste NIH RePORTER seed URL: ").strip()
        appl = extract_appl_id_from_url(url)
        if appl:
            return url
        print("Invalid URL. Make sure it contains '/project-details/########'.")


def scrape_similar_projects_clean(page, seed_appl_id: int) -> List[Dict[str, Any]]:
    url = f"https://reporter.nih.gov/project-details/{seed_appl_id}#similar-Projects"
    print("Opening Similar Projects tab...")
    safe_goto(page, url)

    page.mouse.wheel(0, 3500)
    page.wait_for_timeout(1200)

    # Wait until table appears (prevents null-handle evaluate crashes)
    try:
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll("table"))
              .some(t => (t.innerText || "").includes("Match Score") &&
                        (t.innerText || "").includes("Project Number"))
            """,
            timeout=30_000
        )
    except PlaywrightTimeoutError:
        raise RuntimeError("Timed out waiting for the Similar Projects table to load.")

    table = find_table_element_by_header(page, ["Match Score", "Project Number"])
    if table is None:
        raise RuntimeError("Could not find the Similar Projects table.")

    payload = page.evaluate(
        """
        (table) => {
          if (!table) return null;

          const headers = Array.from(table.querySelectorAll("thead th"))
            .map(th => (th.innerText || "").trim());

          const trs = Array.from(table.querySelectorAll("tbody tr"));
          const rows = trs.map(tr => {
            const tds = Array.from(tr.querySelectorAll("td"));
            const cells = tds.map(td => (td.innerText || "").trim());
            const a = tr.querySelector("a[href*='project-details']");
            const href = a ? a.href : "";
            const linkText = a ? (a.innerText || "").trim() : "";
            return { cellCount: tds.length, cells, href, linkText };
          });

          return { headers, rows };
        }
        """,
        table
    )

    if not payload:
        raise RuntimeError("Similar Projects table was not readable (null payload).")

    headers: List[str] = payload.get("headers") or []
    rows: List[Dict[str, Any]] = payload.get("rows") or []

    def hnorm(h: str) -> str:
        return clean_text(h).lower()

    hmap = {hnorm(h): i for i, h in enumerate(headers)}

    def get_cell(cells: List[str], header_name: str) -> str:
        idx = hmap.get(hnorm(header_name))
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx]

    pending_title: Dict[int, str] = {}
    merged: Dict[int, Dict[str, Any]] = {}

    for r in rows:
        href = r.get("href", "")
        appl_id = extract_appl_id_from_url(href)
        if not isinstance(appl_id, int):
            continue

        cells = r.get("cells") or []
        link_text = clean_text(r.get("linkText", ""))
        cell_count = int(r.get("cellCount", 0))

        # NIH 2-row layout: title row (usually 1 cell)
        if cell_count <= 2 and link_text:
            pending_title[appl_id] = link_text
            continue

        match_score = parse_int(get_cell(cells, "Match Score") or (cells[0] if cells else ""))
        projnum_raw = get_cell(cells, "Project Number")
        projnum = extract_project_number(projnum_raw) or clean_text(projnum_raw)

        rec = {
            "seed_appl_id": str(seed_appl_id),
            "similar_appl_id": str(appl_id),
            "project_url": href,
            "Match Score": match_score,
            "Project Title": pending_title.get(appl_id, ""),
            "Project Number": projnum,
            "Admin IC": clean_text(get_cell(cells, "Admin IC")),
            "FY Total Cost by IC": clean_text(get_cell(cells, "FY Total Cost by IC")),
            "Fiscal Year": parse_int(get_cell(cells, "Fiscal Year")),
            "Funding IC": clean_text(get_cell(cells, "Funding IC")),
            "Organization": clean_text(get_cell(cells, "Organization")),
            "Principal Investigator(s)/Project Leader(s)": normalize_pi(
                get_cell(cells, "Principal Investigator(s)/Project Leader(s)") or
                get_cell(cells, "Principal Investigator(s)/ Project Leader(s)") or
                get_cell(cells, "Principal Investigator(s)/\nProject Leader(s)")
            ),
        }

        if appl_id not in merged:
            merged[appl_id] = rec
        else:
            # keep higher match score if duplicates appear
            try:
                if int(rec["Match Score"] or 0) > int(merged[appl_id]["Match Score"] or 0):
                    merged[appl_id] = rec
            except Exception:
                pass

    return list(merged.values())


def scrape_publications_minimal(page, appl_id: int) -> List[Dict[str, str]]:
    """
    Robustly scrape publications from the Publications tab.
    Returns [] cleanly if there is no publications table.
    """
    url = f"https://reporter.nih.gov/project-details/{appl_id}#publications"
    print(f"  - Opening Publications tab for appl_id={appl_id}...")
    safe_goto(page, url)

    page.wait_for_timeout(800)
    page.mouse.wheel(0, 2200)
    page.wait_for_timeout(800)

    # Wait until either publications table exists OR UI indicates no publications
    try:
        page.wait_for_function(
            """
            () => {
              const hasPubTable = Array.from(document.querySelectorAll("table"))
                .some(t => (t.innerText || "").includes("Publication Year"));
              const txt = (document.body.innerText || "");
              const noPubs =
                txt.includes("No publications") ||
                txt.includes("No Publications") ||
                txt.includes("No publications found") ||
                txt.includes("No Publications Found");
              return hasPubTable || noPubs;
            }
            """,
            timeout=30_000
        )
    except PlaywrightTimeoutError:
        return []

    table = find_table_element_by_header(page, ["Publication Year"])
    if table is None:
        return []

    blob = page.evaluate(
        """
        (table) => {
          if (!table) return null;

          const headers = Array.from(table.querySelectorAll("thead th"))
            .map(th => (th.innerText || "").trim());

          const rows = Array.from(table.querySelectorAll("tbody tr")).map(tr => {
            const tds = Array.from(tr.querySelectorAll("td"));
            const cells = tds.map(td => (td.innerText || "").trim());
            const a = tr.querySelector("a[href*='pubmed']");
            const pubmed_url = a ? a.href : "";
            return { cells, pubmed_url };
          });

          return { headers, rows };
        }
        """,
        table
    )
    if not blob:
        return []

    headers = [clean_text(h) for h in (blob.get("headers") or [])]
    raw_rows = blob.get("rows") or []

    def find_col(name: str) -> int:
        target = clean_text(name).lower()
        for i, h in enumerate(headers):
            if clean_text(h).lower() == target:
                return i
        return -1

    idx_journal = find_col("Journal (Link to PubMed abstract)")
    idx_year = find_col("Publication Year")

    items = []
    for rr in raw_rows:
        cells = [clean_text(c) for c in (rr.get("cells") or [])]
        jtxt = cells[idx_journal] if (0 <= idx_journal < len(cells)) else ""
        ytxt = cells[idx_year] if (0 <= idx_year < len(cells)) else ""
        purl = rr.get("pubmed_url", "") or ""
        items.append({"j": jtxt, "y": clean_text(ytxt), "url": purl})

    def looks_like_citation_line(s: str) -> bool:
        return bool(re.search(r"\b(19|20)\d{2}\b", s or "")) and (";" in (s or ""))

    out: List[Dict[str, str]] = []
    pending_title: Optional[str] = None

    for it in items:
        j, y, purl = it["j"], it["y"], it["url"]

        is_title_row = (not purl) and (not y) and (not looks_like_citation_line(j))
        is_citation_row = bool(purl) or bool(y) or looks_like_citation_line(j)

        if is_title_row:
            pending_title = j
            continue

        if is_citation_row:
            title = pending_title or j
            pub_year = y or parse_year_from_text(j)
            out.append({
                "pub_title": title,
                "pub_year": pub_year,
                "pub_url": purl,
                "pub_pmid": extract_pmid_from_pubmed_url(purl),
            })
            pending_title = None

    return out


def write_long_csv(out_path: str, projects: List[Dict[str, Any]], page) -> None:
    start = time.time()
    per_proj_secs: List[float] = []

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()

        for i, sp in enumerate(projects, start=1):
            appl_id = int(sp["similar_appl_id"])

            elapsed = time.time() - start
            avg = (sum(per_proj_secs) / len(per_proj_secs)) if per_proj_secs else None
            remaining = (avg * (len(projects) - (i - 1))) if avg else None
            eta_txt = f", ETA ~ {fmt_hms(remaining)}" if remaining is not None else ""

            print(
                f"\n[{i}/{len(projects)}] Scraping appl_id={appl_id} (Match Score={sp.get('Match Score')})"
                f" | elapsed {fmt_hms(elapsed)}{eta_txt}"
            )

            t0 = time.time()

            # Clean, non-scary messaging for missing pubs
            try:
                pubs = scrape_publications_minimal(page, appl_id)
            except Exception:
                pubs = []

            dt = time.time() - t0
            per_proj_secs.append(dt)

            if pubs:
                print(f"   → Found {len(pubs)} publications.")
            else:
                print("   → No publications found for this project.")

            # If no pubs, write ONE row with blank pub fields
            if not pubs:
                row = {c: "" for c in OUTPUT_COLS}
                for k in OUTPUT_COLS:
                    if k in sp:
                        row[k] = sp.get(k, "")
                w.writerow(row)
                continue

            for pub in pubs:
                row = {c: "" for c in OUTPUT_COLS}

                for k in [
                    "seed_appl_id", "similar_appl_id", "project_url",
                    "Match Score", "Project Title", "Project Number",
                    "Admin IC", "FY Total Cost by IC", "Fiscal Year",
                    "Funding IC", "Organization", "Principal Investigator(s)/Project Leader(s)"
                ]:
                    row[k] = sp.get(k, "")

                row["pub_title"] = pub.get("pub_title", "")
                row["pub_year"] = pub.get("pub_year", "")
                row["pub_url"] = pub.get("pub_url", "")
                row["pub_pmid"] = pub.get("pub_pmid", "")

                w.writerow(row)


def run_reporter_scraper():
    seed_url = prompt_seed_url()
    seed_appl_id = extract_appl_id_from_url(seed_url)
    if not seed_appl_id:
        raise RuntimeError("Could not parse seed appl_id from URL.")

    print(f"\nSeed appl_id = {seed_appl_id}")
    print("Collecting similar projects from NIH RePORTER...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        similars = scrape_similar_projects_clean(page, seed_appl_id)
        print(f"\nFound {len(similars)} similar projects from the site table.")

        similars = sorted(similars, key=lambda r: int(r.get("Match Score") or 0), reverse=True)
        top_n = prompt_for_top_n(len(similars))
        similars = similars[:top_n]

        print(f"\nWill scrape publications for the top {top_n} projects by Match Score.")
        write_long_csv(OUT_CSV, similars, page)

        browser.close()

    print(f"\nWrote: {OUT_CSV}")


# ============================================================
# MAIN
# ============================================================

def main():
    mode = prompt_mode()
    if mode == "1":
        run_reporter_scraper()
    else:
        create_bibliography_from_csv()


if __name__ == "__main__":
    main()
