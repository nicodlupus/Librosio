"""
Adds books to books.csv.

You type the title and the author. Everything else is found automatically with whatever works
at that moment (the Ollama model, and OpenLibrary for the description when it is reachable).
The only things you answer are: status, approximate_time_to_read_in_days, times_read,
user_rate, would_read_again and notes.

Usage: python3 DRP.py [path/to/books.csv]      (empty title = stop)
"""
import csv
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests

HERE = Path(__file__).parent
CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "books.csv"
OLLAMA_URL = "https://ollama.com/api/chat"
MODEL = "gpt-oss:120b"      # can be changed with OLLAMA_MODEL in the .env
OL_SEARCH = "https://openlibrary.org/search.json"
OL_BASE = "https://openlibrary.org"
OL_HEADERS = {"User-Agent": "Librosio/1.0 (personal book recommender)"}
OLLAMA_SEARCH_URL = "https://ollama.com/api/web_search"
FALLBACK_MODELS = ["deepseek-v4-pro:0813", "kimi-k3"]     # tried when the first model does not know the book
TYPES = ["Fiction", "Non-Fiction"]
LIST_FIELDS = ("themes", "tone", "authors_mentioned")


def load_env():
    # reads KEY=value lines of the .env (repo root or Data/) without extra libraries
    for folder in (HERE.parent, HERE):
        path = folder / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def normalize(text):
    # lowercase, no accents, no punctuation, single spaces (used to compare titles and authors)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


# ---------- input helpers ----------

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
        if value.isdigit() and lo <= int(value) <= hi:
            return int(value)
        print(f"  type a whole number between {lo} and {hi}")


def ask_user_fields():
    """The only questions: status, days, times_read, user_rate, would_read_again, notes."""
    out = {"status": ask("Status (read / to_read)", "read").lower()}
    while out["status"] not in ("read", "to_read"):
        out["status"] = ask("Status must be read or to_read", "read").lower()
    if out["status"] == "read":
        out["approximate_time_to_read_in_days"] = ask_int("Approximate days to read", 1, 1000)
        out["times_read"] = ask_int("Times read", 1, 100, default=1)
        out["user_rate"] = ask_int("Your rating (1-5)", 1, 5, required=True)
        out["would_read_again"] = ask_int("Would read again (1 yes / 0 no)", 0, 1, required=True)
    else:
        out["times_read"] = 0
    out["notes"] = ask("Notes (Enter to skip)")
    return out


# ---------- finding the data ----------

def ask_model(title, author, examples, model=None, web=None):
    """Descriptive attributes from an Ollama model. Returns a dict or None."""
    model = model or os.environ.get("OLLAMA_MODEL", MODEL)
    context = ""
    if web:
        context = ("\nWeb search results (some may be about other books). If they clearly identify the book "
                   "below, even with typos in the title or author, or the title being a translation, use them "
                   "and answer known=true:\n" + "\n".join(
                       f"- {w.get('title', '')}: {w.get('content', '')[:500]}" for w in web[:6]) + "\n")
    prompt = (
        "You fill the descriptive attributes of a book for a personal book recommender.\n"
        f'Title: "{title}" (it may be a Greek or Italian translation of the title, or have typos)\n'
        f"Author: {author} (it may have typos)\n{context}\n"
        "Reply ONLY with JSON having exactly these keys:\n"
        '  "known": true only if you are confident you know this exact book, otherwise false '
        "(then set every other key to null; never invent a book)\n"
        '  "author": the full correct author name\n'
        '  "original_title": the title in the original language\n'
        '  "description": 2-3 neutral sentences about the book, in English, no spoilers\n'
        '  "type": "Fiction" or "Non-Fiction"\n'
        '  "form": one of Novel, Short stories, Essay, Treatise, Memoir, Biography, Poetry, Play, Guide\n'
        '  "genre": one genre, e.g. Thriller, Crime, Philosophy\n'
        '  "themes": 3-5 short tags (list of strings)\n'
        '  "tone": 1-3 words (list of strings)\n'
        '  "complexity": integer 1 (easy) to 5 (very demanding)\n'
        '  "authors_mentioned": list of authors the book discusses, or null\n'
        '  "series": name of the story/book series the book belongs to (like Hercule Poirot or Robert Langdon), '
        "NOT a publisher's collection; null if it is a standalone book. \"series_number\": integer or null\n"
        '  "year_published": year of the FIRST publication of the original book (integer)\n\n'
        f"Keep the same style as these existing rows:\n{examples}"
    )
    body = {"model": model, "stream": False, "format": "json", "messages": [{"role": "user", "content": prompt}]}
    if model.startswith("gpt-oss"):
        body["think"] = "low"                     # much faster, and enough for this
    for _ in range(2):
        try:
            r = requests.post(OLLAMA_URL, headers={"Authorization": f"Bearer {os.environ['API_KEY']}"},
                              json=body, timeout=90)
            r.raise_for_status()
            return json.loads(re.search(r"\{.*\}", r.json()["message"]["content"], re.DOTALL).group(0))
        except (requests.RequestException, KeyError, ValueError, AttributeError):
            continue
    return None


def web_search(query):
    """Web search through the Ollama API (same API_KEY). Returns a list of results or []."""
    try:
        r = requests.post(OLLAMA_SEARCH_URL, headers={"Authorization": f"Bearer {os.environ['API_KEY']}"},
                          json={"query": query, "max_results": 4}, timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])
    except (requests.RequestException, KeyError, ValueError):
        return []


def recognised(answer):
    return bool(answer) and answer.get("known") is True


def try_ways(title, author, examples):
    """Tries the ways one by one, telling the user. Returns (answer, source) or (None, None)."""
    answer = ask_model(title, author, examples)
    if recognised(answer):
        return answer, "model"
    print("  the model did not recognise the book, searching the web...")
    web = web_search(f"{title} {author} βιβλίο") + web_search(f"{author} book")
    if web:
        answer = ask_model(title, author, examples, web=web)
        if recognised(answer):
            return answer, "web search + model"
    else:
        print("  (the web search gave nothing)")
    for other in FALLBACK_MODELS:
        print(f"  still not found, trying another model ({other})...")
        answer = ask_model(title, author, examples, model=other, web=web)
        if recognised(answer):
            return answer, other
    return None, None


def find_info(title, author, examples):
    """Returns (known, ai, original_title, author, sources). Asks the user for help as a last way."""
    while True:
        answer, source = try_ways(title, author, examples)
        if answer:
            known, ai = clean_model(answer)
            return known, ai, answer.get("original_title"), answer.get("author") or author, [source]
        print("  COULD NOT FIND this book automatically.")
        title = ask("Type the English/original title or fix the spelling (Enter to save it without data)")
        if not title:
            return False, {}, None, author, []
        author = ask("Author", author)


def clean_model(data):
    known = data.get("known") is True
    out = {}
    for key in ("description", "form", "genre", "series"):
        out[key] = data.get(key) if known else None
    out["type"] = data.get("type") if known and data.get("type") in TYPES else None
    for key in LIST_FIELDS:
        value = data.get(key)
        out[key] = [str(v) for v in value] if known and isinstance(value, list) and value else None
    for key, low, high in (("complexity", 1, 5), ("series_number", 0, 1000), ("year_published", 0, 3000)):
        value = data.get(key)
        out[key] = value if known and isinstance(value, int) and low <= value <= high else None
    return known, out


def openlibrary_extra(title, author):
    """Best effort and quick: the real description and pages from OpenLibrary, or {} if it is down."""
    try:
        r = requests.get(OL_SEARCH, headers=OL_HEADERS, timeout=8, params={
            "title": title, "author": author, "limit": 3, "fields": "key,author_name,number_of_pages_median"})
        r.raise_for_status()
        wanted = normalize(author)
        for doc in r.json().get("docs", []):
            if any(wanted in normalize(a) or normalize(a) in wanted for a in doc.get("author_name", [])):
                work = requests.get(f"{OL_BASE}{doc['key']}.json", headers=OL_HEADERS, timeout=8).json()
                desc = work.get("description")
                desc = desc.get("value") if isinstance(desc, dict) else desc
                desc = re.sub(r"\(\[source\]\[\d+\]\)", "", (desc or "").split("----------")[0]).strip()
                return {"description": desc or None, "pages": doc.get("number_of_pages_median")}
    except (requests.RequestException, ValueError):
        pass
    return {}


# ---------- main ----------

def add_book(header, rows):
    title = ask("\nBook title (Enter to stop)")
    if not title:
        return False
    author = ask("Author", required=True)
    if normalize(title) in {normalize(r["title"]) for r in rows}:
        if (ask(f"'{title}' is already in the csv. Add anyway (y/n)", "n") or "n").lower() != "y":
            return True
    user = ask_user_fields()

    print("  looking for the book data...")
    examples = "\n".join(
        ", ".join(f"{k}={r[k]}" for k in ("type", "form", "genre", "themes", "tone", "complexity"))
        for r in rows[-3:])
    known, ai, original, author, sources = find_info(title, author, examples)
    extra = openlibrary_extra(original or title, author) if known else {}
    if extra.get("description"):
        ai["description"] = extra["description"]
        sources.append("OpenLibrary description")
    print(f"  data from: {', '.join(sources) or 'nothing found'}")
    if not known:
        print("  the row is saved with the descriptive fields empty, fill them by hand")

    row = {k: "" for k in header}
    row.update(ai)
    row.update(user)
    row.update(id=max((int(r["id"]) for r in rows if r["id"].isdigit()), default=0) + 1,
               title=title, author=author, pages=extra.get("pages"))
    row = {k: "" if v is None else v for k, v in row.items()}
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=header).writerow(row)

    print(f"\nRow added to {CSV_PATH.name}:")
    for k, v in row.items():
        text = str(v)
        print(f"  {k}: {text[:100] + '...' if len(text) > 100 else text}")
    return True


def main():
    load_env()
    if not os.environ.get("API_KEY"):
        print("No API_KEY found in the .env: the model cannot be used, only OpenLibrary")
    try:
        while True:
            with open(CSV_PATH, encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                header, rows = reader.fieldnames, list(reader)
            if not add_book(header, rows):
                break
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")


if __name__ == "__main__":
    main()
