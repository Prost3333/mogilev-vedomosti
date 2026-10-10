"""Поиск упоминаний в сканах дореволюционных газет.

  py -3.13 poisk.py scan  ФАЙЛ.pdf [--pages 1-70] [--batch] [--dry-run]
  py -3.13 poisk.py collect [--wait]        результаты Batch API
  py -3.13 poisk.py verify                  проверка кандидатов сильной моделью + вырезки
  py -3.13 poisk.py report                  находки.csv и находки.md
  py -3.13 poisk.py eval ФАЙЛ.pdf --truth упоминания.csv --pages 1-70
  py -3.13 poisk.py transcribe ФАЙЛ.pdf --pages 57-59
  py -3.13 poisk.py status

Подробно — README.md рядом.
"""
import argparse
import concurrent.futures as cf
import csv
import json
import os
import pathlib
import re
import sys
import time
import tomllib

import anthropic
import pymupdf
from anthropic.types.messages.batch_create_params import Request

import candidates as C
import llm
import pages as P
from store import Store, now

HERE = pathlib.Path(__file__).resolve().parent
STATUS_RU = {"confirmed": "подтверждено", "rejected": "отклонено", "new": "не проверено", "error": "ошибка проверки"}


# ---------- общее ----------

def load_config(path):
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg["work"] = (pathlib.Path(path).resolve().parent / cfg.get("work_dir", "работа")).resolve()
    cfg["work"].mkdir(parents=True, exist_ok=True)
    cfg["targets_by_id"] = {t["id"]: t for t in cfg["target"]}
    return cfg


def cost(cfg, model, in_tok, out_tok, batched=False):
    pin, pout = cfg.get("prices", {}).get(model, (0, 0))
    usd = (in_tok * pin + out_tok * pout) / 1e6
    return usd / 2 if batched else usd


def open_doc(st, path):
    path = pathlib.Path(path).resolve()
    if not path.exists():
        sys.exit(f"Нет файла: {path}")
    pdf = pymupdf.open(path)
    row = st.doc(str(path), path.name, len(pdf), path.stat().st_size)
    return row, pdf


def page_size(pdf, pno):
    page = pdf[pno - 1]
    z = P.page_zoom(page)
    r = (page.rect * pymupdf.Matrix(z, z)).irect
    return r.width, r.height


def ensure_tiles(cfg, st, doc, pdf, pages, model):
    for pno in pages:
        w, h = page_size(pdf, pno)
        st.ensure_tiles(doc["id"], pno, P.tile_grid(w, h, cfg["tiles"]), model)


def pending_tiles(st, doc_id, pages, model, statuses=("pending", "error")):
    marks = ",".join("?" * len(statuses))
    rows = st.q(f"SELECT * FROM tiles WHERE doc_id=? AND model=? AND status IN ({marks}) ORDER BY page, idx",
                doc_id, model, *statuses)
    want = set(pages)
    return [r for r in rows if r["page"] in want]


def by_page(rows):
    out = {}
    for r in rows:
        out.setdefault(r["page"], []).append(r)
    return out


def tile_jpeg(cfg, img, t):
    return P.to_jpeg(P.crop(img, (t["x"], t["y"], t["w"], t["h"]))[0], cfg["tiles"]["jpeg_quality"])


def rebuild_candidates(st, doc_id, model):
    rows = st.q("SELECT h.*, t.page, t.x, t.y, t.w, t.h FROM hits h JOIN tiles t ON t.id=h.tile_id"
                " WHERE t.doc_id=? AND t.model=? AND t.status='done' ORDER BY t.page, t.idx", doc_id, model)
    clusters = []
    for page_rows in by_page(rows).values():
        clusters += C.cluster(page_rows)
    st.save_candidates(doc_id, model, clusters)
    return clusters


def auth_fail(e):
    sys.exit(f"\nОшибка доступа к API ({e.__class__.__name__}): {e}\n"
             "Задайте ключ в PowerShell:  $env:ANTHROPIC_API_KEY = \"sk-ant-...\"")


# ---------- scan ----------

def cmd_scan(cfg, st, args):
    model = args.model or cfg["models"]["scan"]
    effort = args.effort or cfg["models"]["scan_effort"]
    targets = cfg["target"]
    for path in args.pdf:
        doc, pdf = open_doc(st, path)
        pages = P.parse_pages(args.pages, doc["pages"])
        ensure_tiles(cfg, st, doc, pdf, pages, model)
        todo = pending_tiles(st, doc["id"], pages, model)
        print(f"{doc['name']}: страниц {len(pages)}, плиток к обработке {len(todo)} (модель {model})")
        if args.dry_run:
            dry_run(cfg, doc, pdf, pages, todo, model, args.batch)
        elif not todo:
            pass
        elif args.batch:
            submit_batches(cfg, st, doc, pdf, todo, model, effort, targets)
        else:
            run_sync(cfg, st, doc, pdf, todo, model, effort, targets)
        if not args.dry_run:
            cl = rebuild_candidates(st, doc["id"], model)
            print(f"{doc['name']}: кандидатов {len(cl)}")


def dry_run(cfg, doc, pdf, pages, todo, model, batched):
    in_tok = sum(t["w"] * t["h"] / 784 + 900 for t in todo)
    out_tok = 400 * len(todo)
    usd = cost(cfg, model, in_tok, out_tok, batched)
    print(f"  оценка: ~{in_tok / 1e6:.2f}M входных токенов, ~{out_tok / 1e6:.2f}M выходных, ≈ ${usd:.2f}"
          f"{' (Batch API, −50%)' if batched else ''}")
    prev = cfg["work"] / "превью"
    prev.mkdir(exist_ok=True)
    pno = pages[0]
    img = P.render_page(pdf, pno, cfg["tiles"]["autocontrast"])
    grid = P.tile_grid(img.width, img.height, cfg["tiles"])
    rgb = img.convert("RGB")
    from PIL import ImageDraw
    d = ImageDraw.Draw(rgb)
    colors = [(220, 0, 0), (0, 120, 220), (0, 160, 0), (200, 0, 200)]
    for i, (x, y, w, h) in enumerate(grid):
        d.rectangle([x + 3 * (i % 4), y + 3 * (i % 4), x + w - 1, y + h - 1], outline=colors[i % 4], width=5)
    stem = pathlib.Path(doc["name"]).stem
    rgb.resize((rgb.width // 3, rgb.height // 3)).save(prev / f"{stem}_стр{pno}_сетка.png")
    for i in (0, len(grid) // 2):
        P.crop(img, grid[i])[0].save(prev / f"{stem}_стр{pno}_плитка{i}.jpg", quality=85)
    print(f"  страница {pno}: {img.width}×{img.height} px, плиток {len(grid)}; превью — {prev}")


def run_sync(cfg, st, doc, pdf, todo, model, effort, targets):
    client = llm.client()
    workers = cfg["run"]["workers"]
    done = errors = found = 0
    total = len(todo)
    started = time.time()

    def handle(fut, t):
        nonlocal done, errors, found
        try:
            hits, raw, i, o = fut.result()
            st.tile_done(t["id"], hits, raw, i, o)
            found += len(hits)
            for h in hits:
                print(f"  стр. {t['page']}: {h['target']} — «{h['word']}» ({h['certainty']}): {h['quote'][:80]}")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            auth_fail(e)
        except anthropic.NotFoundError as e:
            sys.exit(f"Модель {model} не найдена: {e}")
        except Exception as e:  # noqa: BLE001 — ошибка одной плитки не должна останавливать файл
            errors += 1
            st.tile_error(t["id"], f"{e.__class__.__name__}: {e}")
        done += 1
        if done % 25 == 0 or done == total:
            rate = done / max(1e-9, time.time() - started)
            print(f"  … {done}/{total} плиток, находок {found}, ошибок {errors}, {rate * 60:.0f} плиток/мин")

    ex = cf.ThreadPoolExecutor(workers)
    inflight = {}
    try:
        for pno, tiles in by_page(todo).items():
            img = P.render_page(pdf, pno, cfg["tiles"]["autocontrast"])
            for t in tiles:
                while len(inflight) >= workers * 2:
                    ready, _ = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
                    for f in ready:
                        handle(f, inflight.pop(f))
                fut = ex.submit(llm.scan_tile, client, model, effort, targets, tile_jpeg(cfg, img, t))
                inflight[fut] = t
        for f in cf.as_completed(list(inflight)):
            handle(f, inflight.pop(f))
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        sys.exit("\nПрервано. Готовые плитки сохранены — повторный запуск продолжит.")
    ex.shutdown()
    if errors:
        print(f"  плиток с ошибкой: {errors} — повторный запуск scan попробует их снова")


def submit_batches(cfg, st, doc, pdf, todo, model, effort, targets):
    client = llm.client()
    limit = cfg["run"]["batch_max_mb"] * 1024 * 1024
    chunk, size = [], 0

    def flush():
        nonlocal chunk, size
        if not chunk:
            return
        try:
            batch = client.messages.batches.create(requests=[r for r, _ in chunk])
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            auth_fail(e)
        st.x("INSERT INTO batches(id, model, created, status, n) VALUES(?,?,?,?,?)",
             batch.id, model, now(), "submitted", len(chunk))
        with st.lock:
            st.db.execute("BEGIN")
            for _, tid in chunk:
                st.db.execute("UPDATE tiles SET status='batched', batch_id=?, updated=? WHERE id=?", (batch.id, now(), tid))
            st.db.execute("COMMIT")
        print(f"  отправлен пакет {batch.id}: {len(chunk)} плиток")
        chunk, size = [], 0

    for pno, tiles in by_page(todo).items():
        img = P.render_page(pdf, pno, cfg["tiles"]["autocontrast"])
        for t in tiles:
            params = llm.scan_params(model, effort, targets, tile_jpeg(cfg, img, t))
            req = Request(custom_id=f"t{t['id']}", params=params)
            req_size = len(params["messages"][0]["content"][0]["source"]["data"]) + 8000
            if chunk and (size + req_size > limit or len(chunk) >= 20000):
                flush()
            chunk.append((req, t["id"]))
            size += req_size
    flush()
    print("  Обычно пакет готов за минуты–час (максимум 24 ч). Забрать результаты: py -3.13 poisk.py collect --wait")


# ---------- collect ----------

def cmd_collect(cfg, st, args):
    client = llm.client()
    while True:
        open_batches = st.q("SELECT * FROM batches WHERE status != 'collected' ORDER BY created")
        if not open_batches:
            print("Незабранных пакетов нет.")
            return
        waiting = 0
        for b in open_batches:
            info = client.messages.batches.retrieve(b["id"])
            c = info.request_counts
            if info.processing_status != "ended":
                waiting += 1
                print(f"{b['id']}: {info.processing_status}, в работе {c.processing}, готово {c.succeeded}, ошибок {c.errored}")
                continue
            ok = bad = 0
            docs = set()
            for res in client.messages.batches.results(b["id"]):
                tid = int(res.custom_id[1:])
                row = st.q("SELECT doc_id FROM tiles WHERE id=?", tid)
                if not row:
                    continue
                docs.add(row[0]["doc_id"])
                if res.result.type == "succeeded":
                    try:
                        hits, raw, i, o = llm.parse_scan(res.result.message)
                        st.tile_done(tid, hits, raw, i, o)
                        ok += 1
                        continue
                    except Exception as e:  # noqa: BLE001
                        err = f"{e.__class__.__name__}: {e}"
                else:
                    err = f"batch: {res.result.type} {getattr(res.result, 'error', '')}"
                st.tile_error(tid, err, status="pending")
                bad += 1
            st.x("UPDATE batches SET status='collected' WHERE id=?", b["id"])
            print(f"{b['id']}: забрано {ok}, вернулось в очередь {bad}")
            for doc_id in docs:
                rebuild_candidates(st, doc_id, b["model"])
        if not waiting or not args.wait:
            if waiting:
                print("Пакеты ещё обрабатываются. Повторите collect позже или с --wait.")
            return
        time.sleep(60)


# ---------- verify ----------

def cmd_verify(cfg, st, args):
    scan_model = args.scan_model or cfg["models"]["scan"]
    model = args.model or cfg["models"]["verify"]
    effort = cfg["models"]["verify_effort"]
    min_rank = C.RANK[args.min_certainty]
    statuses = ("new", "error", "confirmed") if args.again else ("new", "error")
    marks = ",".join("?" * len(statuses))
    rows = st.q(f"SELECT c.*, d.path, d.name FROM candidates c JOIN docs d ON d.id=c.doc_id"
                f" WHERE c.model=? AND c.status IN ({marks}) ORDER BY d.name, c.page", scan_model, *statuses)
    rows = [r for r in rows if C.RANK.get(r["certainty"], 1) >= min_rank]
    if args.doc:
        rows = [r for r in rows if r["name"] == pathlib.Path(args.doc).name]
    if not rows:
        print("Нет кандидатов для проверки.")
        return
    print(f"Проверка {len(rows)} кандидатов моделью {model}")
    frag_dir = cfg["work"] / "фрагменты"
    (frag_dir / "отклонено").mkdir(parents=True, exist_ok=True)
    client = llm.client()

    def job(r, img):
        target = cfg["targets_by_id"].get(r["target"], {"id": r["target"], "description": r["target"]})
        region, box = P.crop(img, (r["x"], r["y"], r["w"], r["h"]), pad=150)
        big, factor = P.upscale(region, 2400)
        data = P.to_jpeg(big, 92)
        header = P.crop(img, (0, 0, img.width, round(img.height * 0.09)))[0]
        header_jpeg = None if box[1] < header.height else P.to_jpeg(header, 88)  # шапка уже во фрагменте
        verdict, i, o = llm.verify(client, model, effort, target, cfg["target"], r["quote"], data, big.size,
                                   header_jpeg, cfg["models"]["fallbacks"])
        return verdict, i, o, region, box, factor

    pdfs = {}
    for (path, pno), group in group_by(rows, lambda r: (r["path"], r["page"])).items():
        pdf = pdfs.setdefault(path, pymupdf.open(path))
        img = P.render_page(pdf, pno, autocontrast=False)
        with cf.ThreadPoolExecutor(min(4, len(group))) as ex:
            futs = {ex.submit(job, r, img): r for r in group}
            for f in cf.as_completed(futs):
                r = futs[f]
                try:
                    verdict, i, o, region, box, factor = f.result()
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                    auth_fail(e)
                except Exception as e:  # noqa: BLE001
                    st.x("UPDATE candidates SET status='error', error=? WHERE id=?", f"{e.__class__.__name__}: {e}", r["id"])
                    print(f"  {r['name']} стр. {r['page']} {r['target']}: ошибка — {e}")
                    continue
                ok = bool(verdict.get("found"))
                frag = save_fragment(frag_dir if ok else frag_dir / "отклонено", r, img, region, box, factor,
                                     verdict.get("bbox") or [])
                st.x("UPDATE candidates SET status=?, verify_model=?, verdict=?, fragment=?, in_tok=?, out_tok=?, error=NULL"
                     " WHERE id=?", "confirmed" if ok else "rejected", model, json.dumps(verdict, ensure_ascii=False),
                     str(frag), i, o, r["id"])
                mark = "✔" if ok else "✘"
                text = verdict.get("quote") if ok else (verdict.get("reason") or verdict.get("word"))
                print(f"  {mark} {r['name']} стр. {r['page']} {r['target']}: {str(text)[:100]}")


def group_by(rows, key):
    out = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def save_fragment(folder, r, img, region, box, factor, bbox):
    """Вырезка для архива: по прямоугольнику, который назвала модель, иначе вся область."""
    stem = pathlib.Path(r["name"]).stem
    path = folder / f"{stem}_стр{r['page']}_{r['target']}_{r['id']}.png"
    piece = region
    if len(bbox) == 4:
        x0, y0, x1, y1 = (round(v / factor) for v in bbox)
        if 0 <= x0 < x1 <= region.width + 5 and 0 <= y0 < y1 <= region.height + 5 and (x1 - x0) * (y1 - y0) > 2500:
            piece = P.crop(img, (box[0] + x0, box[1] + y0, x1 - x0, y1 - y0), pad=40)[0]
    P.to_png(piece, path)
    return path


# ---------- report ----------

def year_of(name):
    m = re.match(r"(\d{4})", name)
    return m.group(1) if m else ""


def report_rows(cfg, st, model):
    """Строки по схеме упоминания.csv (данные/СХЕМА.md) + «статус»."""
    rows = st.q("SELECT c.*, d.name FROM candidates c JOIN docs d ON d.id=c.doc_id WHERE c.model=?"
                " ORDER BY d.name, c.page, c.y", model)
    out = []
    for r in rows:
        v = json.loads(r["verdict"]) if r["verdict"] else {}
        frag = os.path.relpath(r["fragment"], cfg["work"]).replace("\\", "/") if r["fragment"] else ""
        out.append({
            "год": year_of(r["name"]), "раздел": "", "объект": v.get("object") or r["target"],
            "тема": v.get("topic", ""), "выпуск": v.get("issue", ""), "дата": v.get("date", ""),
            "файл": r["name"], "стр_pdf": str(r["page"]), "стр_газеты": v.get("paper_page", ""),
            "рубрика": v.get("rubric", ""), "публикация": v.get("publication", ""), "лица": v.get("persons", ""),
            "цитата": v.get("quote") or r["quote"], "смысл": v.get("summary") or v.get("reason", ""),
            "фрагменты": frag, "статус": STATUS_RU.get(r["status"], r["status"]),
            "_context": v.get("context", ""), "_certainty": v.get("certainty") or r["certainty"],
        })
    out.sort(key=lambda r: (r["год"], r["файл"], int(r["стр_pdf"])))
    return out


def assign_ids(rows):
    counter = {}
    for r in rows:
        counter[r["год"]] = counter.get(r["год"], 0) + 1
        r["id"] = f"{r['год']}-{counter[r['год']]:03d}"


def pages_rows(st, model, mentions):
    """Журнал просмотра: подряд идущие пустые страницы — одной строкой, страницы с находками — отдельно."""
    out = []
    for d in st.q("SELECT * FROM docs ORDER BY name"):
        tiles = st.q("SELECT page, status FROM tiles WHERE doc_id=? AND model=?", d["id"], model)
        if not tiles:
            continue
        found = group_by([m for m in mentions if m["файл"] == d["name"]], lambda m: int(m["стр_pdf"]))
        entries = []
        for pno, ts in sorted(by_page(tiles).items()):
            if not all(t["status"] == "done" for t in ts):
                entries.append((pno, "не просмотрено полностью (ошибки или пакет не забран)", None))
            elif pno in found:
                ms = found[pno]
                text = "; ".join(f"{m['объект']} ({m['тема'] or m['цитата'][:60]})"
                                 + ("" if m["статус"] == "подтверждено" else f" — {m['статус']}") for m in ms)
                entries.append((pno, text, ms[0]))
            else:
                entries.append((pno, "ничего не найдено", None))
        i = 0
        while i < len(entries):
            j = i
            while (entries[i][2] is None and j + 1 < len(entries) and entries[j + 1][1] == entries[i][1]
                   and entries[j + 1][0] == entries[j][0] + 1 and entries[j + 1][2] is None):
                j += 1
            first, text, m = entries[i]
            last = entries[j][0]
            out.append({"год": year_of(d["name"]), "файл": d["name"],
                        "стр_pdf": str(first) if first == last else f"{first}–{last}",
                        "выпуск": m["выпуск"] if m else "", "дата": m["дата"] if m else "",
                        "стр_газеты": m["стр_газеты"] if m else "", "результат": text})
            i = j + 1
    return out


def cmd_report(cfg, st, args):
    import excel
    model = args.model or cfg["models"]["scan"]
    rows = report_rows(cfg, st, model)
    if not args.all:
        rows = [r for r in rows if r["статус"] != "отклонено"]
    assign_ids(rows)
    with_status = args.all or any(r["статус"] != "подтверждено" for r in rows)
    cols = excel.MENTION_FIELDS + (["статус"] if with_status else [])
    out_csv = cfg["work"] / "находки.csv"
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    pages = pages_rows(st, model, rows)
    out_xlsx = cfg["work"] / "находки.xlsx"
    excel.write(out_xlsx, rows, pages, with_status)

    md = ["# Находки", "", f"Модель поиска: `{model}`. Обновлено: {now()}.", ""]
    md += coverage_lines(st, model)
    for name, group in group_by(rows, lambda r: r["файл"]).items():
        md += ["", f"## {name}", ""]
        for r in group:
            head = " · ".join(x for x in (f"№ {r['выпуск']}" if r["выпуск"] else "", r["дата"],
                                          f"стр. газеты {r['стр_газеты']}" if r["стр_газеты"] else "") if x)
            md.append(f"### {r['id']} · стр. PDF {r['стр_pdf']} — {r['объект']}: {r['тема'] or '—'}")
            md.append("")
            md.append(f"*{r['статус']}, уверенность {r['_certainty']}*" + (f" · {head}" if head else "") + "  ")
            if r["рубрика"]:
                md.append(f"**Рубрика:** {r['рубрика']}" + (f" ({r['публикация']} публикация)" if r["публикация"] else "") + "  ")
            md.append(f"**Цитата:** {r['цитата']}  ")
            if r["лица"]:
                md.append(f"**Лица:** {r['лица']}  ")
            if r["смысл"]:
                md.append(f"**Смысл:** {r['смысл']}  ")
            if r["фрагменты"]:
                md.append(f"\n![фрагмент]({r['фрагменты']})")
            md.append("")
    (cfg["work"] / "находки.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Записано {len(rows)} находок: {out_xlsx.name}, {out_csv.name}, находки.md в {cfg['work']}")


def coverage_lines(st, model):
    lines = ["| Файл | Страниц просмотрено | Плиток с ошибкой | В пакетах |", "|---|---|---|---|"]
    for d in st.q("SELECT * FROM docs ORDER BY name"):
        rows = st.q("SELECT page, status FROM tiles WHERE doc_id=? AND model=?", d["id"], model)
        if not rows:
            continue
        pages = by_page(rows)
        full = [p for p, ts in pages.items() if all(t["status"] == "done" for t in ts)]
        err = sum(r["status"] == "error" for r in rows)
        bat = sum(r["status"] == "batched" for r in rows)
        lines.append(f"| {d['name']} | {len(full)} из {d['pages']} ({compress(full)}) | {err} | {bat} |")
    return lines


def compress(nums):
    nums = sorted(nums)
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(parts) or "—"


# ---------- eval ----------

def cmd_eval(cfg, st, args):
    model = args.model or cfg["models"]["scan"]
    doc, pdf = open_doc(st, args.pdf)
    pages = P.parse_pages(args.pages, doc["pages"])
    truth = C.load_truth(args.truth, doc["name"], pages, cfg["target"])
    cands = [c for c in st.q("SELECT * FROM candidates WHERE doc_id=? AND model=?", doc["id"], model)
             if c["page"] in set(pages)]
    tiles = [t for t in st.q("SELECT page, status, in_tok, out_tok, batch_id FROM tiles WHERE doc_id=? AND model=?",
                             doc["id"], model) if t["page"] in set(pages)]
    not_done = sum(t["status"] != "done" for t in tiles)
    print(f"{doc['name']}, стр. {compress(pages)}, модель поиска {model}")
    if not tiles or not_done:
        print(f"  внимание: не просмотрено плиток {not_done if tiles else 'все'} — сначала scan")
    used = set()
    found = confirmed = 0
    for item in truth:
        match = [c for c in cands if c["target"] == item["target"] and c["page"] in item["pages"]]
        used.update(c["id"] for c in match)
        ok = [c for c in match if c["status"] == "confirmed"]
        found += bool(match)
        confirmed += bool(ok)
        state = "подтверждено" if ok else ("найдено, не подтверждено" if match else "ПРОПУЩЕНО")
        print(f"  {item['id']:10} {item['target']:10} стр. {compress(item['pages']):7} — {state}")
    n = len(truth) or 1
    print(f"Полнота поиска: {found}/{len(truth)} ({found / n:.0%}); после проверки: {confirmed}/{len(truth)} ({confirmed / n:.0%})")
    extra = [c for c in cands if c["id"] not in used]
    by_status = group_by(extra, lambda c: STATUS_RU.get(c["status"], c["status"]))
    print(f"Кандидатов вне эталона: {len(extra)} — " + ", ".join(f"{k}: {len(v)}" for k, v in by_status.items()))
    for c in extra:
        if c["status"] == "confirmed":
            v = json.loads(c["verdict"])
            print(f"  ? стр. {c['page']} {c['target']}: {v.get('quote', '')[:100]}  ← нет в эталоне, проверьте глазами")
    in_tok = sum(t["in_tok"] for t in tiles)
    out_tok = sum(t["out_tok"] for t in tiles)
    usd = sum(cost(cfg, model, t["in_tok"], t["out_tok"], bool(t["batch_id"])) for t in tiles)
    print(f"Расход на поиск: {in_tok / 1e6:.2f}M вход, {out_tok / 1e6:.3f}M выход, ≈ ${usd:.2f}"
          f" (≈ ${usd / max(1, len(pages)):.4f} за страницу)")


# ---------- transcribe ----------

def cmd_transcribe(cfg, st, args):
    model = args.model or cfg["models"]["transcribe"]
    effort = cfg["models"]["transcribe_effort"]
    tc = cfg["transcribe"]
    doc, pdf = open_doc(st, args.pdf)
    pages = P.parse_pages(args.pages, doc["pages"])
    client = llm.client()
    for pno in pages:
        w, h = page_size(pdf, pno)
        bands = P.band_grid(w, h, tc["band_height"], tc["band_overlap"])
        for i, (_, y, _, bh) in enumerate(bands):
            st.x("INSERT OR IGNORE INTO transcripts(doc_id, page, band, model, y, h) VALUES(?,?,?,?,?,?)",
                 doc["id"], pno, i, model, y, bh)
        todo = st.q("SELECT * FROM transcripts WHERE doc_id=? AND page=? AND model=? AND status!='done' ORDER BY band",
                    doc["id"], pno, model)
        if not todo:
            continue
        img = P.render_page(pdf, pno, cfg["tiles"]["autocontrast"])

        def job(t):
            band = P.crop(img, (0, t["y"], img.width, t["h"]))[0]
            if t["band"] > 0:
                prev = bands[t["band"] - 1]
                band = P.mark_line(band, prev[1] + prev[3] - t["y"])
            return llm.transcribe(client, model, effort, P.to_jpeg(band.convert("RGB"), 92), t["band"] == 0,
                                  cfg["models"]["fallbacks"])

        with cf.ThreadPoolExecutor(min(4, len(todo))) as ex:
            futs = {ex.submit(job, t): t for t in todo}
            for f in cf.as_completed(futs):
                t = futs[f]
                try:
                    text, i, o = f.result()
                    st.x("UPDATE transcripts SET status='done', text=?, in_tok=?, out_tok=?, error=NULL WHERE id=?",
                         text, i, o, t["id"])
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                    auth_fail(e)
                except Exception as e:  # noqa: BLE001
                    st.x("UPDATE transcripts SET status='error', error=? WHERE id=?", f"{e.__class__.__name__}: {e}", t["id"])
                    print(f"  стр. {pno}, полоса {t['band']}: ошибка — {e}")
        print(f"  стр. {pno}: полос {len(bands)}")
    out_dir = cfg["work"] / "тексты"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{pathlib.Path(doc['name']).stem}.txt"
    parts = []
    for pno, rows in by_page(st.q("SELECT * FROM transcripts WHERE doc_id=? AND model=? ORDER BY page, band",
                                  doc["id"], model)).items():
        parts.append(f"[Страница PDF {pno}]")
        for r in rows:
            parts.append(f"--- полоса {r['band'] + 1} ---")
            parts.append(r["text"] if r["status"] == "done" else f"[ошибка распознавания: {r['error']}]")
        parts.append("")
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"Текст: {out}")


# ---------- status ----------

def cmd_status(cfg, st, args):
    total_usd = 0.0
    for d in st.q("SELECT * FROM docs ORDER BY name"):
        for m in st.q("SELECT DISTINCT model FROM tiles WHERE doc_id=?", d["id"]):
            model = m["model"]
            tiles = st.q("SELECT status, in_tok, out_tok, batch_id FROM tiles WHERE doc_id=? AND model=?", d["id"], model)
            counts = group_by(tiles, lambda t: t["status"])
            usd = sum(cost(cfg, model, t["in_tok"], t["out_tok"], bool(t["batch_id"])) for t in tiles)
            total_usd += usd
            cands = group_by(st.q("SELECT status FROM candidates WHERE doc_id=? AND model=?", d["id"], model),
                             lambda c: STATUS_RU.get(c["status"], c["status"]))
            print(f"{d['name']} [{model}]: плитки " + ", ".join(f"{k} {len(v)}" for k, v in counts.items())
                  + f"; кандидаты " + (", ".join(f"{k} {len(v)}" for k, v in cands.items()) or "0") + f"; ≈ ${usd:.2f}")
    for r in st.q("SELECT verify_model, SUM(in_tok) i, SUM(out_tok) o FROM candidates WHERE verify_model IS NOT NULL"
                  " GROUP BY verify_model"):
        usd = cost(cfg, r["verify_model"], r["i"], r["o"])
        total_usd += usd
        print(f"Проверка [{r['verify_model']}]: ≈ ${usd:.2f}")
    for r in st.q("SELECT model, SUM(in_tok) i, SUM(out_tok) o FROM transcripts GROUP BY model"):
        usd = cost(cfg, r["model"], r["i"], r["o"])
        total_usd += usd
        print(f"Расшифровка [{r['model']}]: ≈ ${usd:.2f}")
    pending = st.q("SELECT id, status, n FROM batches WHERE status != 'collected'")
    for b in pending:
        print(f"Пакет {b['id']} ({b['n']} плиток) ещё не забран — collect")
    print(f"Итого ≈ ${total_usd:.2f}")


# ---------- main ----------

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Поиск упоминаний в сканах газет")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="первый проход: поиск по плиткам")
    s.add_argument("pdf", nargs="+")
    s.add_argument("--pages", help="например 1-70 или 5,8,10-")
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--batch", action="store_true", help="через Batch API (−50%%, ответ до 24 ч)")
    s.add_argument("--dry-run", action="store_true", help="только оценка стоимости и превью плиток")

    c = sub.add_parser("collect", help="забрать результаты Batch API")
    c.add_argument("--wait", action="store_true", help="ждать, пока все пакеты будут готовы")

    v = sub.add_parser("verify", help="проверить кандидатов сильной моделью")
    v.add_argument("--model")
    v.add_argument("--scan-model")
    v.add_argument("--doc")
    v.add_argument("--min-certainty", choices=["low", "medium", "high"], default="low")
    v.add_argument("--again", action="store_true", help="перепроверить и уже подтверждённых (например, чтобы заполнить новые колонки)")

    r = sub.add_parser("report", help="находки.xlsx, находки.csv и находки.md")
    r.add_argument("--model", help="модель поиска (по умолчанию из config)")
    r.add_argument("--all", action="store_true", help="включить отклонённые (колонка «статус»)")

    e = sub.add_parser("eval", help="сравнить с эталонной таблицей")
    e.add_argument("pdf")
    e.add_argument("--truth", required=True)
    e.add_argument("--pages")
    e.add_argument("--model", help="модель поиска")

    t = sub.add_parser("transcribe", help="полная расшифровка страниц")
    t.add_argument("pdf")
    t.add_argument("--pages", required=True)
    t.add_argument("--model")

    sub.add_parser("status", help="прогресс и расходы")

    args = ap.parse_args()
    cfg = load_config(args.config)
    st = Store(cfg["work"] / "poisk.db")
    {"scan": cmd_scan, "collect": cmd_collect, "verify": cmd_verify, "report": cmd_report,
     "eval": cmd_eval, "transcribe": cmd_transcribe, "status": cmd_status}[args.cmd](cfg, st, args)


if __name__ == "__main__":
    main()
