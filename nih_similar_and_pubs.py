import re
import time
import csv
from typing import List, Dict, Any, Optional
from playwright.sync_api import sync_playwright

# ---------------- CONFIG ----------------
HEADLESS = True
PAGE_TIMEOUT_MS = 120_000
BROWSER_ACTION_SLEEP_S = 0.25

OUT_CSV = "similar_projects_long.csv"

# NIH project number format examples: 1R21DE034541-01A1, 5R01AR082809-03, etc.
PROJECT_NUM_RE = re.compile(r"\b\d?[A-Z]{1,3}\d{2}[A-Z]{2}\d{6}(?:-\d{2}[A-Z0-9]{0,3})?\b")

# ---------------- OUTPUT COLUMNS (EXACT) ----------------
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
]

# ---------------- HELPERS ----------------
def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s


def sleep_brief():
    time.sleep(BROWSER_ACTION_SLEEP_S)


def parse_year_from_text(s: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", s or "")
    return m.group(0) if m else ""


def parse_int(text: str) -> str:
    t = clean_text(text)
    m = re.search(r"(\d+)", t)
    return m.group(1) if m else ""


def normalize_pi(text: str) -> str:
    t = clean_text(text)
    t = re.sub(r"\s*Principal Investigator\(s\)\s*/\s*Project Leader\(s\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*Principal Investigator\(s\)\s*/\s*Project Leader\(s\).*?$", "", t, flags=re.IGNORECASE)
    return t.strip()


def extract_project_number(text: str) -> str:
    if not text:
        return ""
    m = PROJECT_NUM_RE.search(text.replace("\n", " "))
    return m.group(0) if m else ""


def extract_appl_id_from_url(url: str) -> Optional[int]:
    """
    Accepts URLs like:
      https://reporter.nih.gov/project-details/11178033#description
      https://reporter.nih.gov/search/.../project-details/11178033#similar-Projects
    """
    if not url:
        return None
    m = re.search(r"/project-details/(\d+)", url)
    return int(m.group(1)) if m else None


def prompt_seed_url() -> str:
    while True:
        url = input("Paste the NIH RePORTER seed project URL (project-details/...): ").strip()
        appl = extract_appl_id_from_url(url)
        if appl:
            return url
        print("Could not find an appl_id in that URL. Make sure it contains '/project-details/########'.")


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


# ---------------- SCRAPE: SIMILAR PROJECTS ----------------
def scrape_similar_projects_clean(page, seed_appl_id: int) -> List[Dict[str, Any]]:
    """
    Scrapes Similar Projects table and returns one record per similar appl_id
    with cleaned columns matching the UI.
    Handles NIH's 2-row layout (title row + data row).
    """
    similar_url = f"https://reporter.nih.gov/project-details/{seed_appl_id}#similar-Projects"
    page.goto(similar_url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    page.mouse.wheel(0, 3500)
    page.wait_for_timeout(1500)

    table = page.evaluate_handle(
        """
        () => {
          const tables = Array.from(document.querySelectorAll("table"));
          return tables.find(t =>
            (t.innerText || "").includes("Match Score") &&
            (t.innerText || "").includes("Project Number")
          ) || null;
        }
        """
    )
    if not table:
        raise RuntimeError("Could not find the Similar Projects table.")

    payload = page.evaluate(
        """
        (table) => {
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

        # Title row (usually 1 cell with the title link)
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
            try:
                if int(rec["Match Score"] or 0) > int(merged[appl_id]["Match Score"] or 0):
                    merged[appl_id] = rec
            except Exception:
                pass

    return list(merged.values())


# ---------------- SCRAPE: PUBLICATIONS (MERGE TITLE ROW + CITATION ROW) ----------------
def scrape_publications_minimal(page, appl_id: int) -> List[Dict[str, str]]:
    pub_url = f"https://reporter.nih.gov/project-details/{appl_id}#publications"
    page.goto(pub_url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    sleep_brief()
    page.mouse.wheel(0, 2500)
    page.wait_for_timeout(1200)

    def get_pub_table():
        return page.evaluate(
            """
            () => {
              const tables = Array.from(document.querySelectorAll("table"));
              const table = tables.find(t => (t.innerText || "").includes("Publication Year"));
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

              return { headers, rows, rowCount: rows.length };
            }
            """
        )

    # scroll until stable
    prev, stable = -1, 0
    for _ in range(40):
        blob = get_pub_table()
        if blob is None:
            page.wait_for_timeout(800)
            page.mouse.wheel(0, 1500)
            continue

        count = blob.get("rowCount", 0)
        if count == prev:
            stable += 1
        else:
            stable = 0
            prev = count
        if stable >= 3:
            break

        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)

    blob = get_pub_table()
    if blob is None:
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
        jtxt = cells[idx_journal] if (idx_journal >= 0 and idx_journal < len(cells)) else ""
        ytxt = cells[idx_year] if (idx_year >= 0 and idx_year < len(cells)) else ""
        url = rr.get("pubmed_url", "") or ""
        items.append({"j": jtxt, "y": clean_text(ytxt), "url": url})

    def looks_like_citation_line(s: str) -> bool:
        return bool(re.search(r"\b(19|20)\d{2}\b", s or "")) and (";" in (s or ""))

    out: List[Dict[str, str]] = []
    pending_title: Optional[str] = None

    for it in items:
        j, y, url = it["j"], it["y"], it["url"]

        is_title_row = (not url) and (not y) and (not looks_like_citation_line(j))
        is_citation_row = bool(url) or bool(y) or looks_like_citation_line(j)

        if is_title_row:
            pending_title = j
            continue

        if is_citation_row:
            title = pending_title or j
            pub_year = y or parse_year_from_text(j)
            out.append({"pub_title": title, "pub_year": pub_year, "pub_url": url})
            pending_title = None
            continue

    # ignore dangling title-only rows (prevents extra blank rows)
    return out


# ---------------- WRITE OUTPUT ----------------
def write_long_csv(out_path: str, projects: List[Dict[str, Any]], page) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()

        for i, sp in enumerate(projects, start=1):
            appl_id = int(sp["similar_appl_id"])
            print(f"[{i}/{len(projects)}] appl_id={appl_id} (Match Score={sp.get('Match Score')}) ...")

            try:
                pubs = scrape_publications_minimal(page, appl_id)
            except Exception as e:
                pubs = []
                print(f"   !! publications error: {type(e).__name__}: {e}")

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

                w.writerow(row)


# ---------------- MAIN ----------------
def main():
    seed_url = prompt_seed_url()
    seed_appl_id = extract_appl_id_from_url(seed_url)
    if not seed_appl_id:
        raise RuntimeError("Could not parse seed appl_id from URL.")

    print(f"\nSeed appl_id = {seed_appl_id}")
    print("Collecting similar projects from NIH RePORTER...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        similars = scrape_similar_projects_clean(page, seed_appl_id)
        print(f"Found {len(similars)} similar projects from the site table.")

        similars = sorted(similars, key=lambda r: int(r.get("Match Score") or 0), reverse=True)
        top_n = prompt_for_top_n(len(similars))
        similars = similars[:top_n]

        print(f"\nWill scrape publications for the top {top_n} projects by Match Score.\n")
        write_long_csv(OUT_CSV, similars, page)

        browser.close()

    print(f"\nWrote: {OUT_CSV}")


if __name__ == "__main__":
    main()
