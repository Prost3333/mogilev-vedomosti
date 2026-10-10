"""Склейка находок из перекрывающихся плиток в кандидатов и сравнение с эталонной таблицей."""
import csv
import re
from difflib import SequenceMatcher

RANK = {"high": 3, "medium": 2, "low": 1}

_OLD = str.maketrans({"ѣ": "е", "і": "и", "ї": "и", "ѳ": "ф", "ѵ": "и", "ё": "е"})


def norm(text):
    """Сравнение без оглядки на орфографию: «Вѣтковскій» ≈ «ветковский»."""
    t = (text or "").lower().translate(_OLD)
    t = re.sub(r"ъ\b", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _overlap(r1, r2):
    x1, y1 = max(r1[0], r2[0]), max(r1[1], r2[1])
    x2, y2 = min(r1[0] + r1[2], r2[0] + r2[2]), min(r1[1] + r1[3], r2[1] + r2[3])
    return (x1, y1, x2 - x1, y2 - y1) if x2 > x1 and y2 > y1 else None


def _union(r1, r2):
    x1, y1 = min(r1[0], r2[0]), min(r1[1], r2[1])
    x2, y2 = max(r1[0] + r1[2], r2[0] + r2[2]), max(r1[1] + r1[3], r2[1] + r2[3])
    return (x1, y1, x2 - x1, y2 - y1)


def cluster(rows):
    """rows: находки одной страницы (поля page, target, word, quote, certainty, tile_id, x, y, w, h).

    Одно и то же место видно в нескольких перекрывающихся плитках — такие находки
    объединяются, а область кандидата сужается до пересечения плиток.
    """
    clusters = []
    for r in rows:
        rect = (r["x"], r["y"], r["w"], r["h"])
        best = None
        for c in clusters:
            if c["target"] != r["target"] or not _overlap(c["union"], rect):
                continue
            if max(similar(r["quote"], q) for q in c["quotes"]) >= 0.6:
                best = c
                break
        if best is None:
            best = {"page": r["page"], "target": r["target"], "inter": rect, "union": rect,
                    "quotes": [], "tiles": [], "word": r["word"], "quote": r["quote"], "certainty": r["certainty"]}
            clusters.append(best)
        else:
            best["inter"] = _overlap(best["inter"], rect) if best["inter"] else None
            best["union"] = _union(best["union"], rect)
        best["quotes"].append(r["quote"])
        if r["tile_id"] not in best["tiles"]:
            best["tiles"].append(r["tile_id"])
        if (RANK[r["certainty"]], len(r["quote"])) > (RANK[best["certainty"]], len(best["quote"])):
            best.update(word=r["word"], quote=r["quote"], certainty=r["certainty"])
    for c in clusters:
        c["rect"] = c["inter"] or c["union"]
    return clusters


def page_set(spec):
    pages = set()
    for part in str(spec).replace("—", "-").replace("–", "-").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            pages.add(int(part))
    return pages


def load_truth(csv_path, doc_name, pages, targets):
    """Эталонные находки из данные/<год>/упоминания.csv для одного PDF."""
    alias = {}
    for t in targets:
        alias[t["id"]] = t["id"]
        for a in t.get("aliases", []):
            alias[a] = t["id"]
    items = []
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("файл") != doc_name:
                continue
            p = page_set(row.get("стр_pdf", ""))
            if not p or not (p & set(pages)):
                continue
            for obj in (row.get("объект") or "").split(";"):
                obj = obj.strip()
                if obj in alias:
                    items.append({"id": row.get("id"), "target": alias[obj], "pages": p,
                                  "quote": row.get("цитата", "")})
    return items
