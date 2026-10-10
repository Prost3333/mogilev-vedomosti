"""Запросы к Claude: поиск на плитке, проверка кандидата, расшифровка полосы."""
import base64
import json

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5")

ABOUT = ("Это фрагмент скана «Могилевских губернских ведомостей» (конец XIX — начало XX в.). "
         "Орфография дореформенная: ѣ, і, ѳ, ѵ, ъ на конце слов. Скан — фотография подшивки: "
         "текст бывает мелким, бледным, смазанным или наклонённым.")


def client():
    return anthropic.Anthropic(max_retries=6, timeout=300)


def _targets_text(targets):
    lines = []
    for t in targets:
        forms = ", ".join(t.get("forms", []))
        lines.append(f"- «{t['id']}»: {t['description']}. Примеры написания: {forms}.")
    return "\n".join(lines)


def scan_system(targets):
    return f"""{ABOUT} На изображении — прямоугольный кусок страницы; края режут строки и колонки.

Задача — найти упоминания только этих объектов:
{_targets_text(targets)}

Правила:
- Просмотри весь фрагмент строка за строкой: заголовки, объявления, таблицы, списки, мелкий шрифт.
- Сообщай о каждом слове, которое является объектом в любой падежной форме или производным от него (прилагательное, название волости, общества и т. п.).
- Если слово похоже на объект, но прочитано неуверенно (смазано, обрезано краем, одна-две буквы под вопросом), всё равно сообщи с certainty "low". Пропустить настоящее упоминание хуже, чем сообщить лишнее: всё найденное потом проверяется отдельно.
- Фрагменты перекрываются, поэтому слово, обрезанное краем, целиком видно на соседнем фрагменте. Обрывок сообщай, только если в нём виден корень объекта (Вѣтк…, Грошик…, Рубан…); обрывки вроде «…ковъ», «…тка», «…анов» не сообщай.
- Бледный зеркальный текст, просвечивающий с обратной стороны листа, не читай.
- Нарицательное «вѣтка» (ветвь, желѣзнодорожная вѣтка) не сообщай, если из строки ясно, что это не местечко.
- quote — дословно 5–20 слов строки с найденным словом, как напечатано, без исправлений и без перевода в современную орфографию.
- Остальной текст не переписывай. Если ничего нет — верни пустой список hits."""


def scan_schema(targets):
    return {
        "type": "object",
        "properties": {
            "hits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": [t["id"] for t in targets]},
                        "word": {"type": "string"},
                        "quote": {"type": "string"},
                        "certainty": {"type": "string", "enum": ["high", "medium", "low"]},
                        "note": {"type": "string"},
                    },
                    "required": ["target", "word", "quote", "certainty", "note"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["hits"],
        "additionalProperties": False,
    }


def object_names(targets):
    return [n for t in targets for n in [t["id"], *t.get("aliases", [])]]


def verify_schema(targets):
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "object": {"type": "string", "enum": object_names(targets)},
            "word": text, "quote": text, "context": text, "topic": text, "rubric": text,
            "publication": {"type": "string", "enum": ["", "1-я", "2-я", "3-я"]},
            "persons": text, "summary": text,
            "issue": text, "date": text, "paper_page": text,
            "certainty": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": text,
            "bbox": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["found", "object", "word", "quote", "context", "topic", "rubric", "publication", "persons",
                     "summary", "issue", "date", "paper_page", "certainty", "reason", "bbox"],
        "additionalProperties": False,
    }


def verify_prompt(target, targets, quote, width, height):
    objects = "; ".join(f"«{n}»" for n in object_names(targets))
    return f"""{ABOUT}

Первое изображение — фрагмент страницы. Второе — верхний край той же страницы (шапка с номером газеты, датой, номером страницы), если он есть.

Автоматический поиск предположил, что на фрагменте упоминается «{target['id']}» ({target['description']}).
Предполагаемая строка: «{quote}».

Внимательно прочитай фрагмент и проверь. Верни:
- found: true, только если объект действительно упомянут (слово прочитано уверенно и относится к объекту, а не похожее слово);
- object: к чему относится упоминание, одно из: {objects};
- word: найденное слово, как напечатано;
- quote: дословно фраза или строка таблицы с этим словом, в дореформенной орфографии, без исправлений; нечитаемое — [неразборчиво];
- context: дословно связный текст вокруг: абзац объявления или строка таблицы с шапкой столбцов, если она видна (до ~120 слов);
- topic: тема в одну строку по-русски в современной орфографии, например «Розыск ветковского мещанина Шепшелева» или «Торги Московского земельного банка: имение Ветка»;
- rubric: заголовок рубрики или раздела газеты над этим текстом («Розыски», «О публичной и аукционной продаже» и т. п.), если виден, иначе пусто;
- publication: «1-я», «2-я» или «3-я», если над объявлением напечатано «(по 1-му / 2-му / 3-му разу)», иначе пусто;
- persons: упомянутые в этом тексте люди в виде «Фамилия Имя Отчество, сословие» через «; », в современной орфографии, иначе пусто;
- summary: 1–2 предложения по-русски в современной орфографии — о чём сообщение;
- issue: номер газеты (только число) из шапки, если виден, иначе пусто;
- date: дата выпуска в виде ГГГГ-ММ-ДД, если напечатана в шапке, иначе пусто;
- paper_page: номер страницы, напечатанный в шапке, если виден, иначе пусто;
- certainty: high / medium / low;
- reason: если found=false — что там написано на самом деле, иначе пусто;
- bbox: [x0, y0, x1, y1] — прямоугольник в пикселях первого изображения ({width}×{height}), охватывающий context; [] если не найдено."""


def transcribe_prompt(first_band):
    marker = "" if first_band else (
        " Красная линия у верхнего края отмечает границу предыдущего фрагмента: строки, целиком лежащие выше неё, "
        "уже расшифрованы — пропусти их; строку, которую линия пересекает, перепиши.")
    return f"""{ABOUT} Это горизонтальная полоса страницы во всю ширину.

Перепиши текст полосы дословно, как напечатано: сохраняй дореформенную орфографию (ѣ, і, ѳ, ъ), не исправляй опечатки, не достраивай текст по смыслу, не переводи. Нечитаемое — [неразборчиво].
Колонки переписывай слева направо; перед каждой колонкой — строка «=== колонка N ===». Заголовок во всю ширину — перед колонками.
Строки, обрезанные нижним краем полосы, не переписывай — они будут в следующей полосе.{marker}
Верни только текст, без пояснений и без markdown."""


def _image_block(data, media="image/jpeg"):
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def scan_params(model, effort, targets, jpeg):
    return {
        "model": model,
        "max_tokens": 4000,
        "system": scan_system(targets),
        "messages": [{"role": "user", "content": [_image_block(jpeg), {"type": "text", "text": "Найди упоминания на этом фрагменте."}]}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": scan_schema(targets)}},
    }


class ModelError(Exception):
    pass


def text_of(msg):
    if msg.stop_reason == "refusal":
        raise ModelError(f"модель отказалась отвечать ({getattr(msg, 'stop_details', None)})")
    if msg.stop_reason == "max_tokens":
        raise ModelError("ответ обрезан по max_tokens")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise ModelError("пустой ответ")
    return text


def usage_of(msg):
    u = msg.usage
    return (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0), u.output_tokens or 0


def parse_scan(msg):
    text = text_of(msg)
    hits = json.loads(text)["hits"]
    return hits, text, *usage_of(msg)


def scan_tile(cl, model, effort, targets, jpeg):
    return parse_scan(cl.messages.create(**scan_params(model, effort, targets, jpeg)))


def _create(cl, params, use_fallbacks):
    if use_fallbacks and params["model"] in FALLBACK_MODELS:
        return cl.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
    return cl.messages.create(**params)


def verify(cl, model, effort, target, targets, quote, jpeg, size, header_jpeg=None, use_fallbacks=True):
    content = [_image_block(jpeg)]
    if header_jpeg:
        content.append(_image_block(header_jpeg))
    content.append({"type": "text", "text": verify_prompt(target, targets, quote, *size)})
    params = {
        "model": model,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": verify_schema(targets)}},
    }
    msg = _create(cl, params, use_fallbacks)
    text = text_of(msg)
    return json.loads(text), *usage_of(msg)


def transcribe(cl, model, effort, jpeg, first_band, use_fallbacks=True):
    params = {
        "model": model,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": [
            _image_block(jpeg),
            {"type": "text", "text": transcribe_prompt(first_band)}]}],
        "output_config": {"effort": effort},
    }
    msg = _create(cl, params, use_fallbacks)
    return text_of(msg), *usage_of(msg)
