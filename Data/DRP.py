"""
This is a pipeline for retrieving data of the rest of the books using the OpenLibrary

Steps (one book per round):
1. ask for the title
2. clean the text (and find the original title when it is Greek/Italian/etc.)
3. find the data that needs no answers from us (OpenLibrary)
4. ask the user for what only the user knows (reading info, rating, notes...)
5. ask Claude for the descriptive attributes (themes, tone, complexity...)
6. append the new row to books.csv
7. print the new row to check it

Usage: python3 DRP.py [path/to/books.csv]
"""
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

import requests

CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("books.csv")
OL_SEARCH = "https://openlibrary.org/search.json"
OL_BASE = "https://openlibrary.org"
OL_HEADERS = {"User-Agent": "Librosio/1.0 (personal book recommender)"}
OL_FIELDS = "key,title,subtitle,author_name,first_publish_year,number_of_pages_median,subject"
MODEL = "claude-opus-5-5"

TYPES = ["Fiction", "Non-Fiction"]
AI_FIELDS = ["type", "form", "genre", "themes", "tone", "complexity",
             "authors_mentioned", "series", "series_number", "year_published"]
LIST_FIELDS = {"themes", "tone", "authors_mentioned"}


# ---------- step 2: text management ----------

def normalize(text):
    # lowercase, no accents, no punctuation, single spaces (used to compare titles)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def clean_description(desc):
    # OpenLibrary descriptions are either a string or {"type":..., "value":...}
    if isinstance(desc, dict):
        desc = desc.get("value")
    if not desc:
        return None
    desc = desc.split("----------")[0]          # drops the "Source / Contributors" footer
    desc = re.sub(r"\(\[source\]\[\d+\]\)", "", desc)
    return re.sub(r"[ \t]+", " ", desc).strip() or None


# ---------- small input helpers ----------

def ask(prompt, default=None, required=False):
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default not in (None, ""):
            return str(default)
        if not required:
            return None
        print("  this one is required")


def ask_int(prompt, lo, hi, default=None, required=False):
    while True:
        value = ask(prompt, default, required)
        if value is None:
            return None
        if value.lstrip("-").isdigit() and lo <= int(value) <= hi:
            return int(value)
        print(f"  type a whole number between {lo} and {hi}")


def ask_date(prompt):
    while True:
        value = ask(f"{prompt} (YYYY-MM-DD, Enter to skip)")
        if value is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        print("  use the format YYYY-MM-DD")


def ask_yes_no(prompt, default="y"):
    return (ask(f"{prompt} (y/n)", default) or default).lower().startswith("y")


def read_multiline():
    # reads pasted text until an empty line
    lines = []
    while True:
        line = input()
        if not line.strip():
            return "\n".join(lines)
        lines.append(line)


# ---------- Claude ----------

def ask_claude(prompt, what):
    """Returns a dict. Uses the API if it is available, otherwise the user pastes Claude's reply."""
    text = None
    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            print(f"  Claude declined to answer ({what})")
        else:
            text = "".join(b.text for b in response.content if b.type == "text")
    except Exception as error:
        print(f"  (Claude API not used: {type(error).__name__}) ")
    if text is None:
        print(f"\nPaste this to Claude, then paste its JSON reply here and finish with an empty line:\n")
        print("-" * 60 + f"\n{prompt}\n" + "-" * 60)
        text = read_multiline()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def identify_book(title):
    # Greek/Italian/... titles are not in OpenLibrary, so Claude finds the original title
    prompt = (
        f'Identify the book with the title "{title}" (the title may be a Greek, Italian or other '
        "translation). Reply ONLY with JSON: "
        '{"original_title": "...", "author": "..."}. Use null for anything you are not sure about.'
    )
    return ask_claude(prompt, "identify the book") or {}


# ---------- step 3: OpenLibrary ----------

def search_openlibrary(title, author=None):
    params = {"title": title, "limit": 5, "fields": OL_FIELDS}
    if author:
        params["author"] = author
    try:
        r = requests.get(OL_SEARCH, params=params, headers=OL_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json().get("docs", [])
    except requests.RequestException as error:
        print(f"  OpenLibrary error: {error}")
        return []


def get_work(key):
    try:
        r = requests.get(f"{OL_BASE}{key}.json", headers=OL_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return {}


def pick_candidate(docs):
    for i, d in enumerate(docs, 1):
        authors = ", ".join(d.get("author_name", [])[:2]) or "unknown author"
        print(f"  {i}) {d.get('title')} - {authors} ({d.get('first_publish_year', '?')})")
    print("  0) none of these")
    choice = ask_int("Which one", 0, len(docs), default=1)
    return docs[choice - 1] if choice else None


def find_book(title):
    """Returns (doc, work) of the chosen OpenLibrary result, or (None, {})."""
    query, author = title, None
    if not title.isascii():                       # OpenLibrary has no Greek titles
        info = identify_book(title)
        query, author = info.get("original_title") or title, info.get("author")
        print(f"  looking for: {query}" + (f" - {author}" if author else ""))
    docs = search_openlibrary(query, author)
    if not docs and title.isascii():
        info = identify_book(title)
        query, author = info.get("original_title") or title, info.get("author")
        print(f"  looking for: {query}" + (f" - {author}" if author else ""))
        docs = search_openlibrary(query, author)
    while not docs:
        query = ask("Nothing found. Type the original/English title (Enter to continue without OpenLibrary)")
        if not query:
            return None, {}
        docs = search_openlibrary(query)
    doc = pick_candidate(docs)
    return (doc, get_work(doc["key"])) if doc else (None, {})


# ---------- step 4: answers from the user ----------

def ask_user_fields(found):
    row = {}
    row["status"] = ask("Status (read / to_read)", "read", required=True).lower()
    while row["status"] not in ("read", "to_read"):
        row["status"] = ask("Status must be read or to_read", "read").lower()
    row["house"] = ask("Publishing house (of your edition)")
    row["pages"] = ask_int("Pages of your edition", 1, 10000, default=found.get("pages"))
    if row["status"] == "read":
        row["times_read"] = ask_int("Times read", 1, 100, default=1)
        row["approximate_time_to_read_in_days"] = ask_int("Approximate days to read", 1, 1000)
        row["date_started"] = ask_date("Date started")
        row["date_finished"] = ask_date("Date finished")
        row["user_rate"] = ask_int("Your rating", 1, 5, required=True)
        row["would_read_again"] = ask_int("Would read again (1 yes / 0 no)", 0, 1, required=True)
    else:
        row["times_read"] = 0
    row["notes"] = ask("Notes (Enter to skip)")
    return row


# ---------- step 5: answers from Claude ----------

def examples_from_csv(rows):
    keys = ["type", "form", "genre", "themes", "tone", "complexity"]
    return "\n".join(", ".join(f"{k}={r[k]}" for k in keys) for r in rows[-3:])


def claude_fields(found, rows):
    prompt = (
        "You are filling the descriptive attributes of a book for a personal book recommender.\n"
        f"Title: {found['search_title']}\nAuthor: {found.get('author')}\n"
        f"OpenLibrary year: {found.get('year_published')}\nOpenLibrary subjects: {found.get('subjects')}\n"
        f"Description: {found.get('description')}\n\n"
        "Reply ONLY with JSON having exactly these keys:\n"
        '  "type": "Fiction" or "Non-Fiction"\n'
        '  "form": one of Novel, Short stories, Essay, Treatise, Memoir, Biography, Poetry, Play, Guide\n'
        '  "genre": one genre, e.g. Thriller, Crime, Philosophy\n'
        '  "themes": 3-5 short tags (list of strings)\n'
        '  "tone": 1-3 words (list of strings)\n'
        '  "complexity": integer 1 (easy) to 5 (very demanding)\n'
        '  "authors_mentioned": list of authors the book discusses, or null\n'
        '  "series": series name or null, "series_number": integer or null\n'
        '  "year_published": year of the FIRST publication of the original book (integer)\n\n'
        f"Keep the same style as these existing rows:\n{examples_from_csv(rows)}"
    )
    data = ask_claude(prompt, "describe the book") or {}
    out = {}
    out["type"] = data.get("type") if data.get("type") in TYPES else None
    out["form"] = data.get("form")
    out["genre"] = data.get("genre")
    for key in ("themes", "tone", "authors_mentioned"):
        value = data.get(key)
        out[key] = [str(v) for v in value] if isinstance(value, list) and value else None
    c = data.get("complexity")
    out["complexity"] = c if isinstance(c, int) and 1 <= c <= 5 else None
    out["series"] = data.get("series")
    n = data.get("series_number")
    out["series_number"] = n if isinstance(n, int) else None
    y = data.get("year_published")
    out["year_published"] = y if isinstance(y, int) else found.get("year_published")
    if found.get("year_published") and out["year_published"] != found["year_published"]:
        print(f"  note: OpenLibrary says {found['year_published']}, Claude says {out['year_published']} (using Claude's)")
    return out


def review_claude_fields(fields):
    print("\nClaude suggests:")
    for k in AI_FIELDS:
        print(f"  {k}: {fields[k]}")
    if ask_yes_no("Accept", "y"):
        return fields
    for k in AI_FIELDS:
        current = ", ".join(fields[k]) if k in LIST_FIELDS and fields[k] else fields[k]
        value = ask(f"{k}", current)
        if value is None:
            fields[k] = None
        elif k in LIST_FIELDS:
            fields[k] = [v.strip() for v in value.split(",") if v.strip()]
        elif k in ("complexity", "series_number", "year_published"):
            fields[k] = int(value) if value.isdigit() else fields[k]
        else:
            fields[k] = value
    return fields


# ---------- steps 6 and 7: csv ----------

def to_csv_value(value):
    # same format json_to_csv.py produces: None -> empty, lists -> python list text
    return "" if value is None else value


def next_id(rows):
    return max((int(r["id"]) for r in rows if r["id"].isdigit()), default=0) + 1


def run_once(header, rows):
    # step 1
    title = ask("\nBook title", required=True)
    if normalize(title) in {normalize(r["title"]) for r in rows}:
        if not ask_yes_no(f"'{title}' is already in the csv. Continue anyway", "n"):
            return None
    # steps 2 and 3
    doc, work = find_book(title)
    found = {"search_title": doc["title"] if doc else title}
    if doc:
        found.update(
            author=(doc.get("author_name") or [None])[0],
            subtitle=doc.get("subtitle"),
            year_published=doc.get("first_publish_year"),
            pages=doc.get("number_of_pages_median"),
            subjects=(doc.get("subject") or [])[:15],
            description=clean_description(work.get("description")),
        )
        print(f"  found: {found['search_title']} - {found['author']}, {found['year_published']}, {found['pages']} pages")
    else:
        found["author"] = ask("Author", required=True)
    # step 4
    user = ask_user_fields(found)
    # step 5
    ai = review_claude_fields(claude_fields(found, rows))
    # step 6
    row = {k: "" for k in header}
    row.update(id=next_id(rows), title=title, subtitle=found.get("subtitle"),
               description=found.get("description"), author=found["author"], **user, **ai)
    row = {k: to_csv_value(row.get(k)) for k in header}
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=header).writerow(row)
    # step 7
    print("\nNew row added to", CSV_PATH.name)
    for k, v in row.items():
        text = str(v)
        print(f"  {k}: {text[:100] + '...' if len(text) > 100 else text}")
    return row


def main():
    try:
        while True:
            with open(CSV_PATH, encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                header, rows = reader.fieldnames, list(reader)
            run_once(header, rows)
            if not ask_yes_no("\nAdd another book", "y"):
                break
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")


if __name__ == "__main__":
    main()
