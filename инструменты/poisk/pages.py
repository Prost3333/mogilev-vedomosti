"""Страницы PDF → изображения в исходном разрешении, плитки, вырезки."""
import io
import math

import pymupdf
from PIL import Image, ImageDraw, ImageOps

MAX_SIDE = 6000  # защита от PDF с огромными картинками


def page_zoom(page):
    """Масштаб, при котором рендер совпадает с разрешением вложенного скана.

    Сканы лежат в PDF как один JPEG на страницу; рендер в «300 DPI» лишь
    раздувает картинку, не добавляя деталей.
    """
    infos = [i for i in page.get_image_info() if i.get("width") and i.get("bbox")]
    if infos:
        info = max(infos, key=lambda i: pymupdf.Rect(i["bbox"]).get_area())
        box = pymupdf.Rect(info["bbox"])
        zoom = max(info["width"], info["height"]) / max(box.width, box.height)
    else:
        zoom = 200 / 72
    longest = max(page.rect.width, page.rect.height) * zoom
    if longest > MAX_SIDE:
        zoom *= MAX_SIDE / longest
    return zoom


def render_page(doc, pno, autocontrast=True):
    """pno — номер страницы с 1. Возвращает изображение в оттенках серого."""
    page = doc[pno - 1]
    zoom = page_zoom(page)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if autocontrast:
        img = ImageOps.autocontrast(img, cutoff=1)
    return img


def _starts(total, size, overlap):
    if total <= size:
        return [0], total
    n = math.ceil((total - overlap) / (size - overlap))
    step = (total - size) / (n - 1)
    return [round(i * step) for i in range(n)], size


def tile_grid(width, height, cfg):
    """Плитки с перекрытием, построчно сверху вниз: [(x, y, w, h), ...]."""
    xs, w = _starts(width, cfg["width"], cfg["overlap_x"])
    ys, h = _starts(height, cfg["height"], cfg["overlap_y"])
    return [(x, y, w, h) for y in ys for x in xs]


def band_grid(width, height, band_height, overlap):
    ys, h = _starts(height, band_height, overlap)
    return [(0, y, width, h) for y in ys]


def crop(img, rect, pad=0):
    x, y, w, h = rect
    box = (max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad))
    return img.crop(box), box


def to_jpeg(img, quality=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_png(img, path):
    img.save(path, "PNG", optimize=True)


def upscale(img, max_side, max_factor=2.0):
    factor = min(max_factor, max_side / max(img.width, img.height))
    if factor <= 1.05:
        return img, 1.0
    return img.resize((round(img.width * factor), round(img.height * factor)), Image.LANCZOS), factor


def mark_line(img, y):
    """Красная линия поперёк полосы: «выше — уже расшифровано»."""
    rgb = img.convert("RGB")
    ImageDraw.Draw(rgb).line([(0, y), (rgb.width, y)], fill=(230, 0, 0), width=3)
    return rgb


def parse_pages(spec, total):
    """'1-5,8,10-' → [1..5, 8, 10..total]. Пусто — все страницы."""
    if not spec:
        return list(range(1, total + 1))
    pages = set()
    for part in spec.replace("–", "-").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else total
            pages.update(range(a, b + 1))
        else:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)
