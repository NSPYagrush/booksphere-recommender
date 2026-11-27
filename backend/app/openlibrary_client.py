"""Utilidades compartidas de sanitización textual, deduplicación y cliente Open Library.

Características clave:
  1. Limpieza Unicode NFC + sanitización estricta de puntuación (sin ``::``, sin símbolos
     sueltos ``>``/``~``/``@``, sin ruido de edición) y filtro de formatos no-libro
     (``Audio CD``, ``Box Set``, ``Coloring Book``, ``Workbook``, multi-libros ``A / B``).
  2. Deduplicación canónica estricta por primer autor y obra:
       * Normaliza iniciales/orden en autores (``J.K. Rowling`` == ``J. K. Rowling`` == ``Rowling J K``).
       * Elimina variantes editoriales (``Movie Tie-in``, ``Gift Edition``, ``Illustrated Edition``,
         ``Collector's Edition``, ``Deluxe Edition``, ``Large Print``, ``Volume X``, etc.).
       * NUNCA trunca artículos iniciales (``The``, ``A``) del título real ni de la clave.
  3. Soporte bilingüe limpio (Inglés o Español original), localización de obras de autores
     hispanohablantes al español y bloqueo estricto de mojibake/no-latino y otros idiomas.
"""
from __future__ import annotations

import html
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import httpx

OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_HEADERS = {
    "User-Agent": "BookSphereAcademic/1.0 (Universidad del Cauca; recommender-enrichment)"
}
OPENLIBRARY_FIELDS = (
    "key,title,author_name,first_publish_year,subject,"
    "number_of_pages_median,isbn,ratings_average,ratings_count,"
    "want_to_read_count,first_sentence,language"
)

GENERIC_GENRES = {"Fiction", "Nonfiction"}

CANONICAL_GENRES: tuple[str, ...] = (
    "Fiction",
    "Thriller",
    "Children's",
    "Mystery",
    "Fantasy",
    "Romance",
    "Religion & Spirituality",
    "Adventure",
    "Biography & Memoir",
    "Science Fiction",
    "Young Adult",
    "Politics",
    "History",
    "Self Help",
    "Humor",
    "Science",
    "Art",
    "Cooking",
    "Classics",
    "Business",
    "Lifestyle & Hobbies",
    "Horror",
    "Comics & Graphic Novels",
    "Education",
    "Historical Fiction",
    "Travel",
    "Psychology",
    "Sports & Outdoors",
    "Poetry",
    "Philosophy",
)

# --------------------------------------------------------------------------- #
# Reglas de limpieza de texto y rechazo de formatos no-libro
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_AUTHOR_NOISE_RE = re.compile(
    r"\bFormat:\s*.*$"
    r"|Visit Amazon(?:'s)?\b.*?\bPage\b"
    r"|\((?:Author|Illustrator|Editor|Translator|Narrator|Contributor|"
    r"Introduction|Foreword|Afterword|Adapter|Compiler|Creator|Series Editor|Reader)[^)]*\)",
    re.IGNORECASE,
)
_EDITION_BRACKET_RE = re.compile(
    r"\s*\[(?:"
    r"Mass Market Paperback|Paperback|Hardcover|Board Book|Spiral[- ]?bound|"
    r"Audio CD|Deckled Edge|Large Print|Illustrated|Kindle Edition|"
    r"Library Binding|Unabridged|Abridged|Box Set|Special Edition|A Cookbook|"
    r"Movie Tie[- ]?in|TV Tie[- ]?in|Book\s+\d+"
    r")[^\]]*\]",
    re.IGNORECASE,
)
_SCRAPER_UPPER_BRACKET_RE = re.compile(r"\s*\[[A-Z0-9 /:-]{6,}\]\s*")
_LEADING_STRAY_RE = re.compile(r"^[><~*•·|=+_#\\/\-–—:;,\s]+")
_TRAILING_STRAY_RE = re.compile(r"[:;\-–—><~*•·|=+_#/\\\s]+$")
_AUTHOR_LEADING_STRAY_RE = re.compile(r"^[><~*•·|=+_#@\\/\-–—:;,\s]+")
_AUTHOR_TRAILING_STRAY_RE = re.compile(r"[><~*•·|=+_#@\\/\-–—:;,\s]+$")

_CTRL_OR_MOJIBAKE_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|Ã[\x80-\xbf¡-ÿ]|Â[\x80-\xbf\xa0]|"
    r"ãƒ|ã‚|æœ|é»|å[\x80-\xbf]|â€|Ø[\x80-\xbf]|Ù[\x80-\xbf]|\ufffd"
)
_NON_LATIN_RE = re.compile(
    r"[\u0370-\u03ff\u0400-\u052f\u0590-\u06ff\u0900-\u097f\u0e00-\u0e7f"
    r"\u3000-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]"
)
_BAD_SYMBOL_EDGE_RE = re.compile(r"^[><~*•·|=+_#@]|[><~*•·|=+_#@]$")
_AUTHOR_BAD_CHARS_RE = re.compile(r"[@<>\[\]{}]|https?://|www\.", re.IGNORECASE)
_BY_PREFIX_RE = re.compile(r"^By\s+[A-Z][a-z]+\s+[A-Z][a-z]+\s+")

# Formatos no-libro / paquetes múltiples / tarjetas y cuadernos que generan ruido
_NON_BOOK_FORMAT_RE = re.compile(
    r"\b(?:"
    r"audio\s*cd|audiobook|audio\s+collection|mp3(?:\s*cd)?|compact\s+disc|unabridged\s+cd|"
    r"box(?:ed)?\s*set|boxset|collection\s+set|boxed\s+collection|trilogy\s+(?:set|box)|"
    r"(?:books?|series)\s*\)?\s*\d+\s*[-–to]+\s*\d+|all\s+\d+\s+books|"
    r"\d+\s+books?\s+(?:collection|set|bundle)|complete\s+(?:book\s+)?series|"
    r"complete\s+(?:trilogy|series\s+collection)|3\s+volumes?\b|"
    r"coloring\s+book|activity\s+book|sticker\s+book|companion\s+workbook|"
    r"paint\s+by\s+sticker|color\s+by\s+number|sticker\s+by\s+number|word\s+searches?|"
    r"summer\s+bridge\s+activities|summer\s+brain\s+quest|"
    r"flash\s*cards?|game\s*cards?|card\s+game|board\s+book\s+block|"
    r"sound\s+book|play[- ]?a[- ]?sound|me\s+reader|electronic\s+reader|"
    r"wedding\s+planner\s*(?:&|and)\s*organizer|blank\s+journal|wall\s+calendar|"
    r"study\s+guide|trivia\s+book|unofficial\s+.*(?:cookbook|reference|spellbook|guide)|"
    r"origami\b|crochet\s+(?:patterns|wizardry)|baking\s+book|"
    r"complete\s+reference\s+guide"
    r")\b|\bCD\b(?:\s*\(|$)|(?:^|\s)[-–]\s*Audio\b|\bWorkbook\b|\bSummary\s+of\b|\bOmnibus\b",
    re.IGNORECASE,
)

# Vocabulario funcional para validación bilingüe estricta (EN vs ES vs otros idiomas)
_EN_FUNCTION_WORDS = {
    "the", "and", "of", "to", "in", "is", "a", "that", "for", "with", "as",
    "on", "his", "her", "by", "from", "at", "it", "this", "an", "be", "are",
    "was", "but", "not", "have", "has", "who", "their", "they", "he", "she",
    "into", "about", "one", "all", "will", "when", "out", "up", "more", "than",
    "life", "world", "story", "new", "book", "you", "your", "can", "novel",
}
_ES_FUNCTION_WORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "en", "y", "e", "o", "que", "por", "para", "con", "sin", "sobre", "su", "sus",
    "es", "son", "fue", "era", "entre", "como", "pero", "desde", "hasta", "obra",
    "novela", "libro", "historia", "vida", "mundo", "autor", "autora", "publicada",
    "escrita", "escrito", "esta", "este", "estos", "estas", "dos", "sus", "más", "mas",
}
# Otros idiomas no permitidos (italiano, portugués, francés, alemán, polaco, sueco)
_OTHER_FOREIGN_WORDS = {
    # francés
    "les", "des", "une", "pour", "dans", "avec", "est", "sont", "sur", "qui", "pas", "cette", "monde",
    # alemán
    "der", "die", "das", "und", "ein", "eine", "mit", "von", "auf", "den", "dem", "nicht", "ich", "ist",
    # italiano
    "che", "della", "delle", "degli", "gli", "nel", "nella", "sono", "questo", "questa",
    "molti", "anni", "dopo", "davanti", "plotone", "esecuzione", "venuta", "tempesta",
    # portugués
    "não", "nao", "uma", "mais", "gente", "disse", "são", "sao", "isso", "pelo", "pela", "você", "voce",
    # polaco / sueco / neerlandés
    "nie", "się", "jest", "oraz", "przez", "jako", "jego", "jej", "och", "att", "för", "van", "het", "een",
}
_SPAM_SNIPPETS = ("double check isbn", "exact same isbn", "brand new, exactly same isbn")


def _ascii_fold(text: object) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()


def clean_text(raw: object) -> str:
    """Normaliza Unicode NFC, entidades HTML, etiquetas y espacios múltiples."""
    s = unicodedata.normalize("NFC", html.unescape(_TAG_RE.sub(" ", str(raw or ""))))
    return re.sub(r"\s+", " ", s).strip()


def clean_title(raw: object) -> str:
    """Limpia artefactos de títulos (doble '::', símbolos '>', corchetes de formato)."""
    s = clean_text(raw)
    if not s:
        return ""
    s = _EDITION_BRACKET_RE.sub("", s)
    s = _SCRAPER_UPPER_BRACKET_RE.sub(" ", s)
    s = _LEADING_STRAY_RE.sub("", s)
    s = re.sub(r"\s*:{2,}\s*", ": ", s)
    s = re.sub(r"\s*;\s*;+\s*", "; ", s)
    s = re.sub(r"\s+:\s*", ": ", s)
    s = _TRAILING_STRAY_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_AUTHOR_CREDENTIALS_RE = re.compile(
    r"(?:,?\s+\b(?:Ph\.?\s*D\.?|M\.?\s*D\.?|L\.?\s*M\.?\s*S\.?\s*W\.?|"
    r"Ed\.?\s*D\.?|Psy\.?\s*D\.?|D\.?\s*D\.?S\.?|M\.?\s*B\.?\s*A\.?|"
    r"L\.?C\.?S\.?W\.?|R\.?\s*N\.?|J\.?\s*D\.?|D\.?\s*O\.?))+\.?\s*$",
    re.IGNORECASE,
)
_AUTHOR_LEADING_CRED_RE = re.compile(
    r"^(?:(?:Ph\.?\s*D\.?|M\.?\s*D\.?|L\.?\s*M\.?\s*S\.?\s*W\.?|Ed\.?\s*D\.?|Psy\.?\s*D\.?|LCSW)\b[\s,\.]*)+",
    re.IGNORECASE,
)
_AUTHOR_EMBEDDED_CRED_RE = re.compile(
    r"\s+\b(?:Ph\.?\s*D\.?|M\.?\s*D\.?|L\.?\s*M\.?\s*S\.?\s*W\.?|Ed\.?\s*D\.?|Psy\.?\s*D\.?|LCSW)\b(?=\s|$|,)",
    re.IGNORECASE,
)
_AUTHOR_ONLY_CRED_RE = re.compile(
    r"^(?:Ph\.?\s*D\.?|M\.?\s*D\.?|L\.?\s*M\.?\s*S\.?\s*W\.?|Ed\.?\s*D\.?|Psy\.?\s*D\.?|LCSW)\.?$",
    re.IGNORECASE,
)
_AUTHOR_PREFIX_TITLE_RE = re.compile(
    r"^(?:(?:Mr|Mrs|Ms|Prof)\.?\s+|Dr\.?\s+(?!Seuss\b))",
    re.IGNORECASE,
)
_NAME_LOWER_PARTICLES = {"de", "del", "la", "las", "los", "van", "von", "der", "da", "di", "do", "dos", "y", "e", "o"}
_NON_INVERTIBLE_FIRST_PARTS = {
    "one", "flea", "raffi", "miryam", "attar", "epictetus", "plato",
    "homer", "aristotle", "voltaire", "rumi", "banksy", "anonymous", "various",
}
_KNOWN_REORDERED_AUTHORS: dict[str, str] = {
    "rowling j k": "J. K. Rowling",
    "rowling j": "J. K. Rowling",
    "martin george r r": "George R. R. Martin",
    "lewis c s": "C. S. Lewis",
    "tolkien j r r": "J. R. R. Tolkien",
    "kuang r f": "R. F. Kuang",
    "harari yuval noah": "Yuval Noah Harari",
    "hosseini khaled": "Khaled Hosseini",
    "sparks nicholas": "Nicholas Sparks",
    "king stephen": "Stephen King",
}


def _smart_title_case_author(part: str) -> str:
    """Convierte autores en MAYÚSCULAS sostenidas (p. ej. 'JOY ELLIS') a formato editorial limpio."""
    tokens = part.split()
    out: list[str] = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if i > 0 and i < len(tokens) - 1 and low in _NAME_LOWER_PARTICLES:
            out.append(low)
        elif len(low) == 1:
            out.append(tok.upper())
        elif re.match(r"^(?:o'|d'|l')[a-z]+$", low):
            out.append(low[:2].upper() + low[2].upper() + low[3:])
        elif low.startswith("mc") and len(low) >= 4:
            out.append("Mc" + low[2].upper() + low[3:])
        else:
            out.append(tok.capitalize())
    return " ".join(out)


def _collapse_repeated_author(part: str) -> str:
    """Colapsa nombres duplicados por error de metadatos ('Julia Quinn Julia Quinn' -> 'Julia Quinn')."""
    toks = part.split()
    n = len(toks)
    if n >= 4 and n % 2 == 0:
        half = n // 2
        if [t.lower() for t in toks[:half]] == [t.lower() for t in toks[half:]]:
            return " ".join(toks[:half])
    return part


def _format_author_initials(part: str) -> str:
    """Estandariza iniciales de autor ('J.K.', 'C S', 'R F', 'George R R') a 'X. Y. Apellido'."""
    s = part.strip()
    # 1. Separar iniciales pegadas (J.K. -> J. K., R.F.C. -> R. F. C.)
    for _ in range(3):
        s = re.sub(r"\b([A-Za-z])\.([A-Za-z])(?:\.|\b)", r"\1. \2.", s)
    s = re.sub(r"\b([A-Za-z])\.([A-Za-z]{2,})", r"\1. \2", s)

    tokens = s.split()
    if not tokens:
        return ""
    out: list[str] = []
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        # Inicial ya punteada de 1 letra ("j." -> "J.")
        if re.fullmatch(r"[A-Za-z]\.", tok):
            out.append(tok[0].upper() + ".")
            continue
        # Inicial suelta de 1 letra sin punto ("C S Lewis", "George R R Martin", "R F Kuang", "K a Tucker")
        if len(tok) == 1 and tok.isalpha() and nxt:
            # Proteger letras finales ("Malcolm X") y acrónimos en mayúsculas ("I SPY")
            nxt_is_acronym = len(nxt.rstrip(".")) >= 2 and nxt.rstrip(".").isupper()
            if nxt_is_acronym:
                out.append(tok)
                continue
            prev_is_initial = bool(out and re.fullmatch(r"[A-Za-z]\.", out[-1]))
            # Conjunción española en minúscula ('y', 'e', 'o') -> mantener salvo si es parte de iniciales
            if tok.islower() and tok in {"y", "e", "o"} and not prev_is_initial:
                out.append(tok)
                continue
            # 'a' minúscula solo es inicial si viene tras otra inicial ('K a Tucker' -> 'K. A. Tucker')
            if tok.islower() and tok not in {"a"} and not prev_is_initial:
                out.append(tok)
                continue
            if tok == "a" and not prev_is_initial:
                out.append(tok)
                continue
            if re.fullmatch(r"[A-Za-z]\.?", nxt) or nxt[0].isupper():
                out.append(tok.upper() + ".")
                continue
        out.append(tok)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def clean_author(raw: object) -> str:
    """Limpia y estandariza nombres e iniciales de autor ('C. S. Lewis', 'R. F. Kuang', etc.)."""
    s = clean_text(raw)
    if not s:
        return ""
    s = _AUTHOR_NOISE_RE.sub("", s)
    if _NON_LATIN_RE.search(s):
        latin_parens = re.findall(r"\(([A-Za-z][A-Za-z .'\-]{2,})\)", s)
        if latin_parens:
            s = latin_parens[0]
    s = re.sub(r"\s*:{2,}\s*", ": ", s)
    s = _AUTHOR_LEADING_STRAY_RE.sub("", s)
    s = _AUTHOR_TRAILING_STRAY_RE.sub("", s)

    raw_parts = [p.strip(" ,;-:") for p in s.split(",") if p.strip(" ,;-:")]
    # Limpiar credenciales (al final, al inicio o incrustadas) y tratamientos por segmento
    cleaned_segs: list[str] = []
    for seg in raw_parts:
        seg = _AUTHOR_LEADING_CRED_RE.sub("", seg).strip(" ,;-:")
        seg = _AUTHOR_CREDENTIALS_RE.sub("", seg).strip(" ,;-:")
        seg = _AUTHOR_EMBEDDED_CRED_RE.sub("", seg).strip(" ,;-:")
        seg = _AUTHOR_LEADING_CRED_RE.sub("", seg).strip(" ,;-:")
        seg = _AUTHOR_PREFIX_TITLE_RE.sub("", seg).strip(" ,;-:")
        seg = re.sub(r"\s+", " ", seg).strip(" ,;-:")
        if seg and not _AUTHOR_ONLY_CRED_RE.fullmatch(seg):
            cleaned_segs.append(seg)

    if not cleaned_segs:
        return ""

    # Detectar formato invertido único "Apellido, Nombre" (p. ej. "Follett, Ken", "Smith, Patrick D.")
    if len(cleaned_segs) == 2:
        p1, p2 = cleaned_segs
        p1_words = p1.split()
        if (
            len(p1_words) == 1
            and len(p1) >= 3
            and p1[0].isupper()
            and p1[1:].islower()
            and p1.lower() not in _NON_INVERTIBLE_FIRST_PARTS
            and (
                re.fullmatch(r"[A-Za-zÀ-ÿ'’-]{2,}", p2)
                or re.fullmatch(r"[A-Za-zÀ-ÿ'’-]{2,}\s+[A-Za-z]\.?", p2)
                or re.fullmatch(r"(?:[A-Za-z]\.?\s*){1,3}", p2)
            )
        ):
            cleaned_segs = [f"{p2.strip()} {p1.strip()}"]

    final_parts: list[str] = []
    for seg in cleaned_segs:
        seg = _collapse_repeated_author(seg)
        seg = _AUTHOR_CREDENTIALS_RE.sub("", seg).strip(" ,;-:")
        if len(seg) >= 5 and seg == seg.upper() and any(ch.isalpha() for ch in seg):
            seg = _smart_title_case_author(seg)

        # Corregir inversiones sin coma tipo "Rowling J K" / "Martin George R R" / "Harari Yuval Noah"
        canon_probe = re.sub(r"\s+", " ", re.sub(r"[.]", " ", _ascii_fold(seg))).strip()
        if canon_probe in _KNOWN_REORDERED_AUTHORS:
            seg = _KNOWN_REORDERED_AUTHORS[canon_probe]
        else:
            toks = seg.split()
            if (
                len(toks) in (3, 4)
                and len(toks[0].rstrip(".")) >= 3
                and all(len(t.rstrip(".")) == 1 and t[0].isalpha() for t in toks[1:])
            ):
                seg = " ".join(toks[1:] + [toks[0]])

        seg = _format_author_initials(seg)
        if seg:
            final_parts.append(seg)

    return ", ".join(dict.fromkeys(final_parts))


def is_non_book_format(raw_title: str, raw_author: str = "") -> bool:
    """Detecta CDs, audiolibros, Box Sets, libros para colorear, workbooks, guías y paquetes multi-obra."""
    s = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if _NON_BOOK_FORMAT_RE.search(s):
        return True
    if "Reader)" in raw_author and "CD" in s:
        return True
    # Guías o resúmenes escritos por terceros sobre otro autor ("... by J.K. Rowling" por otro autor)
    if re.search(r".{10,}\s+by\s+[A-Z]\.?\s*[A-Z]\.?\s*[A-Za-z]+$", s):
        return True
    # Paquetes múltiples con "/": "House of Earth and Blood / House of Sky and Breath / ..."
    if "/" in s and len(s) >= 28:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if len(parts) >= 2 and all(len(p.split()) >= 2 for p in parts[:2]):
            # Evitar falsos positivos en títulos legítimos como "62/Modelo para armar"
            if len(parts) >= 3 or " / " in s:
                return True
    return False


def has_clean_punctuation(title: str, author: str) -> bool:
    """Valida que título y autor no conserven artefactos tipográficos ni ruido de scraping."""
    if not title or not author:
        return False
    if len(title) < 2 or len(title) > 220 or len(author) < 2 or len(author) > 100:
        return False
    if "::" in title or "::" in author:
        return False
    if _BAD_SYMBOL_EDGE_RE.search(title) or _BAD_SYMBOL_EDGE_RE.search(author):
        return False
    if _AUTHOR_BAD_CHARS_RE.search(author) or ":" in author:
        return False
    if re.search(r"\b(?:curated\s+from|revocable\s+living\s+trust)\b", author, re.IGNORECASE):
        return False
    low_t = title.lower()
    if "http://" in low_t or "https://" in low_t or "www." in low_t:
        return False
    t_words = re.findall(r"[a-z0-9]+", _ascii_fold(title))
    a_words = set(re.findall(r"[a-z0-9]+", _ascii_fold(author)))
    if len(t_words) >= 3 and sum(1 for w in t_words if w in a_words) >= max(3, int(len(t_words) * 0.8)):
        return False
    first_author = author.split(",")[0].strip().lower()
    if _BY_PREFIX_RE.match(title) and first_author and first_author in low_t:
        return False
    return True


# --------------------------------------------------------------------------- #
# Deduplicación canónica estricta por autor + obra (sin truncar The/A en títulos)
# --------------------------------------------------------------------------- #
_SUBTITLE_EDITION_RE = re.compile(
    r":\s*(?:"
    r"a\s+novel|a\s+memoir|a\s+thriller|a\s+mystery|a\s+biography|a\s+story|"
    r"a\s+celebration|a\s+new\s+translation|introduction\s+by|with\s+an\s+introduction|"
    r"the\s+official\s+playscript|special\s+rehearsal|the\s+illustrated\s+edition|"
    r"illustrated\s+edition|expanded\s+edition|full[- ]color|movie\s+tie|tv\s+tie|collector|gift\s+edition|"
    r"special\s+edition|(?:\d+(?:st|nd|rd|th)\s+)?anniversary|unabridged|large\s+print|"
    r"deluxe|standard\s+edition|new\s+edition|author'?s\s+preferred\s+text|"
    r"(?:the\s+)?complete\s+and\s+uncut(?:\s+edition)?|"
    r"the\s+philosophy\s+classic|the\s+epic\s+novel|the\s+prequel\s+to|the\s+first\s+novel\s+of|"
    r"book\s+(?:one|two|three|four|five|six|seven|\d+)\s+of\s+the\b|parts?\s+(?:one|1)\b|"
    r"reese'?s\s+book\s+club|oprah'?s\s+book\s+club|read\s+with\s+jenna|"
    r"the\s+(?:record[- ]breaking|multimillion|million[- ]copy|sunday\s+times|new\s+york\s+times|"
    r"number\s+one|no\.?\s*1|#1|instant|international|global|genre[- ]defining|bestselling|"
    r"best[- ]selling|addictive|electrifying|hottest|classic|original|landmark|inspirational|"
    r"definitive|essential|official|perfect|brand\s+new|sequel\s+to|thriller\s+that|"
    r"most\s+heartwrenching|heartbreaking|unforgettable|first\s+book\s+of|first\s+hercule\s+poirot|"
    r"first\s+novel\s+of|epic\s+novel|prequel\s+to|sequel\s+to|"
    r"heirloom\s+edition|tragical\s+history|century\s+trilogy\s+\d+)\b|"
    r"an?\s+(?:captivating|gripping|addictive|unforgettable|heartbreaking|heartwrenching|"
    r"electrifying|bestselling|best[- ]selling|totally\s+gripping|epic|summer\s+beach\s+read|"
    r"richard\s+(?:&|and)\s+judy|holiday\s+book|christmas\s+holiday|easter\s+and\s+springtime|"
    r"world\s+war\s+ii\s+story|practical\s+guide|fable\s+about|soldier'?s\s+memoir|"
    r"novel\s+of|memoir\s+of|true\s+story\s+of|introduction\s+to|reese'?s|oprah'?s|read\s+with\s+jenna)\b|"
    r"an?\s+[a-z0-9][a-z0-9'\s\-]{1,36}\s+(?:novel|mystery|thriller|story|memoir|classic|fable)\b|"
    r"now\s+a\s+major|based\s+on\s+an?|"
    r"from\s+the\s+(?:#1|no\.?\s*1|number\s+one|bestselling|best[- ]selling|award[- ]winning|acclaimed|internationally)?[a-z0-9\s#\-]{0,36}?(?:author|creator)\s+of\b|"
    r"john\s+grisham'?s\s+no\.?\s*1|hercule\s+poirot'?s\s+first|hercule\s+poirot\s+investigates|"
    r"side\s+by\s+side|oxford\s+school\s+shakespeare|with\s+original\s+illustrations|"
    r"or,\s+how\s+toys\s+become\s+real|love\.\s+guilt\.|introducing\s+the\s+"
    r").*$",
    re.IGNORECASE,
)
_TRAILING_EDITION_RE = re.compile(
    r"(?:,|\s+-\s+|\s+―\s+|\s+)"
    r"(?:movie\s+tie[- ]?in|tv\s+tie[- ]?in|color\s+gift\s+edition|collector'?s?\s+edition|"
    r"gift\s+edition|special\s+edition|illustrated\s+edition|the\s+illustrated\s+edition|"
    r"expanded\s+edition|complete\s+and\s+uncut(?:\s+edition)?|"
    r"\d+(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?|anniversary\s+edition|tenth[- ]anniversary|"
    r"standard\s+edition|new\s+edition|deluxe\s+edition|library\s+edition|clean\s+edition|"
    r"original\s+edition|revised\s+edition|heirloom\s+edition|large\s+print|"
    r"complete\s+and\s+unabridged|unabridged|abridged|paperback|hardcover|board\s+book|"
    r"signature\s+classics?|parts?\s+one\s+and\s+two|parts?\s+1\s*&\s*2|adaptation|now\s+a\s+major|\bcd\b|audio).*$"
    r"|(?:\s+-\s+|\s+―\s+)(?:novel|a\s+memoir\s+of\s+the\s+craft|the\s+success\s+secrets).*$",
    re.IGNORECASE,
)
_GLUED_INITIALS_RE = re.compile(r"\b([a-z])\.([a-z])(?:\.([a-z]))?\.?\b")
_AUTHOR_HONORIFICS = {
    "mr", "mrs", "ms", "dr", "sir", "prof", "jr", "sr", "iii", "ii", "iv",
    "phd", "md", "lmsw", "edd", "psyd", "lcsw", "dds", "mba", "rn", "do", "jd", "don", "rev",
    "col", "colonel", "detective", "lieutenant", "lt", "general", "gen", "capt", "captain", "sgt", "sergeant", "maj", "major",
}
_UK_TO_US_WORDS: dict[str, str] = {
    "colours": "colors",
    "colour": "color",
    "honour": "honor",
    "honours": "honors",
    "favour": "favor",
    "favours": "favors",
    "traveller": "traveler",
    "travellers": "travelers",
    "centre": "center",
    "neighbour": "neighbor",
    "neighbours": "neighbors",
    "rumour": "rumor",
    "defence": "defense",
    "theatre": "theater",
}
_LEADING_ARTICLES = {"the", "a", "an", "el", "la", "los", "las"}
_ORDINAL_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
}


def _author_tokens(part: str) -> list[str]:
    part = re.sub(r"\([^)]*\)", " ", str(part or ""))
    s = _ascii_fold(part)
    s = _GLUED_INITIALS_RE.sub(lambda m: " ".join(g for g in m.groups() if g), s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    tokens: list[str] = []
    for t in s.split():
        if t in _AUTHOR_HONORIFICS:
            continue
        tokens.append("william" if t == "wm" else t)
    n = len(tokens)
    if n >= 4 and n % 2 == 0 and tokens[: n // 2] == tokens[n // 2 :]:
        tokens = tokens[: n // 2]
    return tokens


def _single_author_key(part: str) -> str:
    tokens = _author_tokens(part)
    return " ".join(sorted(tokens)) if tokens else ""


_PRIORITY_AUTHOR_KEYS: set[str] | None = None


def norm_author_key(author: str) -> str:
    """Clave normalizada del autor principal: unifica iniciales, orden, tildes e ilustradores/coautores."""
    global _PRIORITY_AUTHOR_KEYS
    parts = [p.strip() for p in str(author or "").split(",") if p.strip()]
    if not parts:
        return ""
    keys = [_single_author_key(p) for p in parts]
    keys = [k for k in keys if k]
    if not keys:
        return ""
    if _PRIORITY_AUTHOR_KEYS is None and "HISPANIC_AUTHORS_CANON" in globals():
        _PRIORITY_AUTHOR_KEYS = set(HISPANIC_AUTHORS_CANON.keys()) | {
            "j k rowling", "c lewis s", "j r r tolkien", "king stephen", "agatha christie",
        }
    if _PRIORITY_AUTHOR_KEYS and len(keys) > 1:
        for k in keys:
            if k in _PRIORITY_AUTHOR_KEYS:
                return k
    return keys[0]


def _split_author_parts_with_aka(author: str) -> list[str]:
    """Divide coautores e incluye pseudónimos '(AKA X)' como partes candidatas para matching."""
    parts = [p.strip() for p in str(author or "").split(",") if p.strip()]
    akas = re.findall(r"\(\s*AKA\s+([^)]+)\)", str(author or ""), flags=re.IGNORECASE)
    for ak in akas:
        parts.extend(p.strip() for p in ak.split(",") if p.strip())
    return parts


def author_identity_keys(author: str) -> list[str]:
    """Devuelve claves de identidad de los autores principales (soporta coautores, iniciales y AKA)."""
    parts = _split_author_parts_with_aka(author)
    out: list[str] = []
    for idx, p in enumerate(parts[:4]):
        toks = _author_tokens(p)
        if not toks:
            continue
        full_k = " ".join(sorted(toks))
        if idx < 2 or (_PRIORITY_AUTHOR_KEYS and full_k in _PRIORITY_AUTHOR_KEYS) or len(parts) > 2:
            if full_k not in out:
                out.append(full_k)
            if len(toks) >= 3 and len(toks[0]) >= 2 and len(toks[-1]) >= 2:
                fl_k = " ".join(sorted((toks[0], toks[-1])))
                if fl_k not in out:
                    out.append(fl_k)
            if len(toks) >= 2 and len(toks[-1]) >= 3 and len(toks[0]) == 1:
                compact = f"{toks[-1]}_{toks[0]}"
                if compact not in out:
                    out.append(compact)
    return out


def _author_compact_last_initial(author: str) -> list[tuple[str, str, str]]:
    """Devuelve [(last, first_token, full_key)] por coautor (incluye solo-apellido >=5 chars y AKA)."""
    parts = _split_author_parts_with_aka(author)
    res: list[tuple[str, str, str]] = []
    for p in parts[:4]:
        toks = _author_tokens(p)
        if len(toks) >= 2 and len(toks[-1]) >= 3:
            res.append((toks[-1], toks[0], " ".join(sorted(toks))))
        elif len(toks) == 1 and len(toks[0]) >= 5:
            res.append((toks[0], "", toks[0]))
    return res


def _authors_compatible(author_a: str, author_b: str) -> bool:
    """True si dos cadenas de autor refieren razonablemente al mismo escritor."""
    keys_a = set(author_identity_keys(author_a))
    keys_b = set(author_identity_keys(author_b))
    if keys_a & keys_b:
        return True
    sig_a = _author_compact_last_initial(author_a)
    sig_b = _author_compact_last_initial(author_b)
    for last_a, first_a, _ in sig_a:
        for last_b, first_b, _ in sig_b:
            if last_a == last_b:
                if first_a == first_b or not first_a or not first_b:
                    return True
                if (len(first_a) == 1 or len(first_b) == 1) and first_a[0] == first_b[0]:
                    return True
    return False


def _normalize_title_surface(title: str) -> str:
    """Normaliza apóstrofes tipográficos y '&' antes de limpiar edición o puntuación."""
    t = str(title or "")
    t = (
        t.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u02bc", "'")
        .replace("\u2032", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    t = _ascii_fold(t)
    t = re.sub(r"\|[^|]+\|\d*", " ", t)
    t = re.sub(r",\s+(?:a|an|the)(?=\s*(?::|\(|$))", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*&\s*", " and ", t)
    return t


def norm_title_key(title: str, author_key: str = "") -> str:
    """Clave canónica de título para detectar ediciones duplicadas SIN mutilar 'The'/'A' en el título real."""
    t = _normalize_title_surface(title)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" :;-")
    for _ in range(2):
        t = _SUBTITLE_EDITION_RE.sub("", t).strip()
        t = _TRAILING_EDITION_RE.sub("", t).strip()
    t = re.sub(r"['\"]+", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [_UK_TO_US_WORDS.get(w, w) for w in t.split()]
    t = " ".join(words)
    if "rowling" in author_key:
        t = t.replace("philosophers stone", "sorcerers stone")
    return t


def _strip_leading_article(norm_t: str) -> str:
    words = norm_t.split()
    if len(words) >= 2 and words[0] in _LEADING_ARTICLES:
        return " ".join(words[1:])
    return norm_t


def _extract_volume_markers(title: str) -> tuple[str, ...]:
    """Extrae marcadores numéricos de volumen/libro/parte/grado para no fusionar entregas distintas."""
    t = _normalize_title_surface(title)
    markers: list[str] = []
    for m in re.finditer(
        r"\b(?:vol(?:ume)?|book|part|pt|episode|chapter|issue|no)\.?[\s#]*(\d+|one|two|three|four|five|six|seven|eight|nine|ten|i|ii|iii|iv|v)\b"
        r"|#\s*(\d+)\b(?!\s+(?:bestsell|best[- ]sell|new\s+york|sunday|amazon|international|national|audible|wall\s+street|instant|hit))"
        r"|\((\d{1,2})\)"
        r"|\b(\d+)(?:st|nd|rd|th)\s+to\s+(\d+)(?:st|nd|rd|th)\s+grade\b"
        r"|\bgrades?\s+([k\d]+)\s*(?:and|&|-)\s*(\d+)\b",
        t,
        re.IGNORECASE,
    ):
        vals = [g for g in m.groups() if g is not None]
        if vals:
            norm_vals = [_ORDINAL_WORDS.get(v.lower(), v.lower()) for v in vals]
            markers.append("-".join(norm_vals))
    return tuple(markers)


def _base_before_sep(title: str) -> str:
    """Devuelve el título base normalizado antes de ':' o ' - ' (sin artículo inicial para comparación)."""
    t = _normalize_title_surface(title)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.split(r":|\s+-\s+|\s+―\s+", t, maxsplit=1)[0]
    t = _TRAILING_EDITION_RE.sub("", t).strip()
    t = re.sub(r"['\"]+", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [_UK_TO_US_WORDS.get(w, w) for w in t.split()]
    return _strip_leading_article(" ".join(words))


def canonical_work_key(title: str, author: str) -> str:
    """Clave única (obra || primer autor) para eliminar copias de la misma obra."""
    a_key = norm_author_key(author)
    t_key = norm_title_key(title, a_key)
    if not t_key or not a_key:
        return ""
    return f"{t_key}||{a_key}"


def canonical_work_aliases(title: str, author: str) -> list[str]:
    """Devuelve claves de obra con alias de artículo inicial (en >=2 palabras) y variantes de autor."""
    a_key = norm_author_key(author)
    t_key = norm_title_key(title, a_key)
    if not t_key or not a_key:
        return []
    t_variants = [t_key]
    stripped = _strip_leading_article(t_key)
    if stripped != t_key and stripped:
        t_variants.append(stripped)

    a_keys = author_identity_keys(author) or [a_key]
    aliases: list[str] = []
    for tv in t_variants:
        if f"{tv}||{a_key}" not in aliases:
            aliases.append(f"{tv}||{a_key}")
        if len(tv) >= 3:
            for ak in a_keys:
                alias = f"{tv}||{ak}"
                if alias not in aliases:
                    aliases.append(alias)
    return aliases


def norm_key(text: str) -> str:
    """Compatibilidad con código existente: normalización ASCII alfanumérica."""
    t = _ascii_fold(text)
    t = re.sub(r"\s*\([^)]*\)\s*", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_cleaner_title(candidate: str, existing: str) -> bool:
    """Prefiere títulos limpios y con su artículo inicial íntegro sobre ediciones promocionales."""
    def noise_score(t: str) -> tuple[int, int, int, int]:
        has_sub = 1 if re.search(
            r"Tie[- ]?in|Collector|Gift Edition|Illustrated|Large Print|Edition|"
            r"Book\s+\d+|Parts?\s+(?:One|1)|Introduction by|Bestseller|Book Club|Read with Jenna",
            t, re.IGNORECASE,
        ) else 0
        has_parens = 1 if ("(" in t or "[" in t) else 0
        has_promo_colon = 1 if (":" in t and len(t.split(":", 1)[1].strip()) > 32) else 0
        # Si dos títulos solo difieren en artículo inicial ("The Help" vs "Help"), preferir el que lo conserva
        first_word = _ascii_fold(t).split()[0] if t.strip() else ""
        missing_art_penalty = 0 if first_word in _LEADING_ARTICLES else 1
        return (has_sub, has_parens, has_promo_colon, len(t) + missing_art_penalty * 8)

    # Si ambos son básicamente la misma frase sin artículo inicial, no penalizar al artículo por +4 chars
    nt_c = _strip_leading_article(norm_title_key(candidate))
    nt_e = _strip_leading_article(norm_title_key(existing))
    if nt_c == nt_e and ":" not in candidate and ":" not in existing and "(" not in candidate and "(" not in existing:
        wc = _ascii_fold(candidate).split()
        we = _ascii_fold(existing).split()
        if wc and we and (wc[0] in _LEADING_ARTICLES) != (we[0] in _LEADING_ARTICLES):
            return wc[0] in _LEADING_ARTICLES
    return noise_score(candidate) < noise_score(existing)


def is_better_author(candidate: str, existing: str) -> bool:
    """Prefiere autor único limpio y con nombre completo sobre iniciales solas, solo-apellido o coautores."""
    if not candidate:
        return False
    if not existing:
        return True
    c_has_aka = "AKA " in candidate.upper()
    e_has_aka = "AKA " in existing.upper()
    if c_has_aka != e_has_aka:
        return not c_has_aka
    c_comma = candidate.count(",")
    e_comma = existing.count(",")
    if c_comma != e_comma:
        return c_comma < e_comma
    c_words = candidate.split(",")[0].strip().split()
    e_words = existing.split(",")[0].strip().split()
    if (len(c_words) >= 2) != (len(e_words) >= 2):
        return len(c_words) >= 2
    c_first = c_words[0].rstrip(".")
    e_first = e_words[0].rstrip(".")
    if (len(c_first) == 1) != (len(e_first) == 1):
        return len(c_first) > len(e_first)
    return len(candidate) > len(existing) and len(candidate) <= 60


def _merge_book_entry(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Fusiona un duplicado ``incoming`` dentro de ``target`` preservando lo mejor de ambos."""
    if "merged_ids" in target:
        target["merged_ids"].extend(incoming.get("merged_ids") or ([incoming["book_id"]] if "book_id" in incoming else []))
    target["in_recommender"] = max(int(target.get("in_recommender", 1)), int(incoming.get("in_recommender", 1)))
    t_votes = int(target.get("ratings_count") or 0)
    i_votes = int(incoming.get("ratings_count") or 0)
    if i_votes > t_votes:
        target["ratings_count"] = i_votes
        target["avg_rating"] = incoming.get("avg_rating")
        if incoming.get("parent_asin"):
            target["parent_asin"] = incoming["parent_asin"]
        if len(str(incoming.get("synopsis") or "")) > len(str(target.get("synopsis") or "")):
            target["synopsis"] = incoming["synopsis"]
    if incoming.get("lang") == "es" or (
        target.get("lang") == incoming.get("lang") and is_cleaner_title(incoming["title"], target["title"])
    ):
        target["title"] = incoming["title"]
    if is_better_author(incoming["author"], target["author"]):
        target["author"] = incoming["author"]
    if target.get("publish_year") is None and incoming.get("publish_year") is not None:
        target["publish_year"] = incoming["publish_year"]
    if target.get("pages") is None and incoming.get("pages") is not None:
        target["pages"] = incoming["pages"]
    if target.get("isbn") is None and incoming.get("isbn") is not None:
        target["isbn"] = incoming["isbn"]


def _has_multi_work_colon_series(records: list[dict[str, Any]], base_no_art: str) -> bool:
    """Detecta si un prefijo antes de ':' corresponde a una saga con múltiples entregas distintas."""
    subtitles: set[str] = set()
    for b in records:
        if _base_before_sep(b["title"]) != base_no_art:
            continue
        t_no_p = re.sub(r"\([^)]*\)", "", b["title"])
        parts = re.split(r":|\s+-\s+", t_no_p, maxsplit=1)
        if len(parts) < 2:
            continue
        nt = _strip_leading_article(norm_title_key(b["title"]))
        # Si norm_title_key ya dejó el subtítulo intacto (no es marketing), contarlo como entrega propia
        if len(nt.split()) > len(base_no_art.split()):
            subtitles.add(nt)
    return len(subtitles) >= 3


def deduplicate_books_map(
    books_by_key: dict[str, dict[str, Any]],
    asin_to_key: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], int]:
    """Capa 2 (O(N)): fusiona variantes de subtítulo promocional, similitud >=0.94 y erratas de autor."""
    merged_map: dict[str, str] = {}

    def resolve(k: str) -> str:
        while k in merged_map:
            k = merged_map[k]
        return k

    # Precalcular rasgos de cada registro en una sola pasada O(N)
    pre: dict[str, tuple[str, str, tuple[str, ...], bool, frozenset[str], list[tuple[str, str, str]], str]] = {}
    by_base_last: dict[tuple[str, str], list[str]] = {}
    by_author_fi: dict[tuple[str, str], list[str]] = {}
    multi_series_bases: dict[tuple[str, str], set[str]] = {}

    for k, b in books_by_key.items():
        title = b["title"]
        author = b["author"]
        a_key = norm_author_key(author)
        nt = _strip_leading_article(norm_title_key(title, a_key))
        base = _base_before_sep(title)
        vols = _extract_volume_markers(title)
        has_sep = len(nt.split()) > len(base.split())
        id_keys = frozenset(author_identity_keys(author))
        sigs = _author_compact_last_initial(author)
        a_fold = _ascii_fold(author).lower()
        pre[k] = (nt, base, vols, has_sep, id_keys, sigs, a_fold)

        lasts = {s[0] for s in sigs} or ({a_key} if a_key else set())
        if base:
            for last in lasts:
                by_base_last.setdefault((base, last), []).append(k)
                if has_sep:
                    multi_series_bases.setdefault((base, last), set()).add(nt)
        for last, first, _ in sigs:
            if first:
                by_author_fi.setdefault((last, first[0]), []).append(k)

    def _sigs_compatible(k1: str, k2: str) -> bool:
        _, _, _, _, ids1, sigs1, _ = pre[k1]
        _, _, _, _, ids2, sigs2, _ = pre[k2]
        if ids1 & ids2:
            return True
        for last1, first1, _ in sigs1:
            for last2, first2, _ in sigs2:
                if last1 == last2:
                    if first1 == first2 or not first1 or not first2:
                        return True
                    if (len(first1) == 1 or len(first2) == 1) and first1[0] == first2[0]:
                        return True
        return False

    # Regla 2A: exacto n1==n2 y título base <-> base:subtítulo agrupados por (base, last) [O(N)]
    for (base_key, last_key), keys in by_base_last.items():
        if len(keys) < 2:
            continue
        is_multi_series = len(multi_series_bases.get((base_key, last_key), ())) >= 3
        distinct_nonempty_vols = {frozenset(pre[k][2]) for k in keys if pre[k][2]}
        single_vol_cluster = len(distinct_nonempty_vols) <= 1
        keys_sorted = sorted(
            keys,
            key=lambda k: (
                -int(books_by_key[k].get("in_recommender", 1)),
                -int(books_by_key[k].get("ratings_count") or 0),
                len(books_by_key[k]["title"]),
            ),
        )
        for idx, k1 in enumerate(keys_sorted):
            rk1 = resolve(k1)
            if rk1 != k1:
                continue
            b1 = books_by_key[k1]
            n1, _, vols1, has_sep1, _, _, _ = pre[k1]
            for k2 in keys_sorted[idx + 1:]:
                rk2 = resolve(k2)
                if rk2 != k2 or rk2 == rk1:
                    continue
                if not _sigs_compatible(k1, k2):
                    continue
                n2, _, vols2, has_sep2, _, _, _ = pre[k2]
                vols_ok = (vols1 == vols2) or (single_vol_cluster and not is_multi_series)
                if (n1 and n1 == n2) or ((has_sep1 != has_sep2) and vols_ok and not is_multi_series):
                    _merge_book_entry(b1, books_by_key[k2])
                    merged_map[k2] = rk1

    # Regla 2B: similitud difusa >= 0.94 dentro de buckets pequeños (apellido, inicial_nombre)
    for _afi, keys in by_author_fi.items():
        live = [k for k in keys if resolve(k) == k]
        if len(live) < 2 or len(live) > 250:
            continue
        # Ordenar por longitud de título normalizado para acotar la ventana de comparación
        live.sort(
            key=lambda k: (
                len(pre[k][0]),
                -int(books_by_key[k].get("in_recommender", 1)),
                -int(books_by_key[k].get("ratings_count") or 0),
            )
        )
        for idx, k1 in enumerate(live):
            rk1 = resolve(k1)
            if rk1 != k1:
                continue
            n1, _, vols1, _, _, _, _ = pre[k1]
            len1 = len(n1)
            if len1 < 8:
                continue
            max_len2 = len1 + max(2, int(len1 * 0.08))
            for k2 in live[idx + 1:]:
                rk2 = resolve(k2)
                if rk2 != k2 or rk2 == rk1:
                    continue
                n2, _, vols2, _, _, _, _ = pre[k2]
                len2 = len(n2)
                if len2 > max_len2:
                    break
                if vols1 != vols2:
                    continue
                if n1 != n2 and n1[:3] != n2[:3] and n1[-4:] != n2[-4:]:
                    continue
                if not _sigs_compatible(k1, k2):
                    continue
                if n1 == n2 or SequenceMatcher(None, n1, n2).ratio() >= 0.94:
                    # Conservar in_recommender=1 y mayor ratings_count como superviviente
                    b1, b2 = books_by_key[k1], books_by_key[k2]
                    score1 = (int(b1.get("in_recommender", 1)), int(b1.get("ratings_count") or 0))
                    score2 = (int(b2.get("in_recommender", 1)), int(b2.get("ratings_count") or 0))
                    if score2 > score1:
                        _merge_book_entry(b2, b1)
                        merged_map[k1] = rk2
                        break
                    _merge_book_entry(b1, b2)
                    merged_map[k2] = rk1

    # Regla 2C: errata menor de autor con exactamente el mismo título normalizado (>= 3 palabras)
    by_exact_title: dict[str, list[str]] = {}
    for k in books_by_key:
        if resolve(k) != k:
            continue
        nt = pre[k][0]
        if len(nt.split()) >= 3:
            by_exact_title.setdefault(nt, []).append(k)

    for _nt, keys in by_exact_title.items():
        if len(keys) < 2:
            continue
        keys_sorted = sorted(
            keys,
            key=lambda k: (-int(books_by_key[k].get("ratings_count") or 0), len(books_by_key[k]["title"])),
        )
        for idx, k1 in enumerate(keys_sorted):
            rk1 = resolve(k1)
            if rk1 != k1:
                continue
            b1 = books_by_key[k1]
            a1 = pre[k1][6]
            for k2 in keys_sorted[idx + 1:]:
                rk2 = resolve(k2)
                if rk2 != k2 or rk2 == rk1:
                    continue
                a2 = pre[k2][6]
                if SequenceMatcher(None, a1, a2).ratio() >= 0.88:
                    _merge_book_entry(b1, books_by_key[k2])
                    merged_map[k2] = rk1

    clean_books = {k: b for k, b in books_by_key.items() if resolve(k) == k}
    new_asin_map: dict[str, str] = {}
    if asin_to_key:
        for asin, old_k in asin_to_key.items():
            rk = resolve(old_k)
            if rk in clean_books:
                new_asin_map[asin] = rk
    return clean_books, new_asin_map, len(merged_map)


class CatalogDedupeIndex:
    """Índice de deduplicación de dos capas para bloquear duplicados antes de insertar en SQLite."""

    def __init__(self) -> None:
        self.seen_aliases: set[str] = set()
        self.seen_ta: set[tuple[str, str]] = set()
        self.seen_ext: set[str] = set()
        self.by_last: dict[str, list[tuple[str, str, str, str, tuple[str, ...], bool]]] = {}
        self.by_exact_title: dict[str, list[str]] = {}

    def add(self, title: str, author: str, external_key: str | None = None) -> None:
        a_key = norm_author_key(author)
        for al in canonical_work_aliases(title, author):
            self.seen_aliases.add(al)
        self.seen_ta.add((title.lower(), author.lower()))
        if external_key:
            self.seen_ext.add(external_key)
        nt = _strip_leading_article(norm_title_key(title, a_key))
        base = _base_before_sep(title)
        vols = _extract_volume_markers(title)
        has_sep = len(nt.split()) > len(base.split())
        entry = (title, author, nt, base, vols, has_sep)
        sigs = _author_compact_last_initial(author)
        lasts = {s[0] for s in sigs} or {a_key}
        for last in lasts:
            if last:
                self.by_last.setdefault(last, []).append(entry)
        if len(nt.split()) >= 3:
            self.by_exact_title.setdefault(nt, []).append(author)

    def is_duplicate(self, title: str, author: str, external_key: str | None = None) -> bool:
        if (title.lower(), author.lower()) in self.seen_ta:
            return True
        if external_key and external_key in self.seen_ext:
            return True
        for al in canonical_work_aliases(title, author):
            if al in self.seen_aliases:
                return True

        a_key = norm_author_key(author)
        nt = _strip_leading_article(norm_title_key(title, a_key))
        base = _base_before_sep(title)
        vols = _extract_volume_markers(title)
        has_sep = len(nt.split()) > len(base.split())
        sigs = _author_compact_last_initial(author)
        lasts = {s[0] for s in sigs} or {a_key}

        for last in lasts:
            bucket = self.by_last.get(last, [])
            for e_title, e_author, e_nt, e_base, e_vols, e_has_sep in bucket:
                if not _authors_compatible(author, e_author):
                    continue
                if nt and nt == e_nt:
                    return True
                if base and base == e_base and (has_sep != e_has_sep):
                    distinct_sub = {
                        en for _, _, en, eb, _, _ in bucket if eb == base and len(en.split()) > len(base.split())
                    }
                    if len(distinct_sub) < 3:
                        distinct_vols = {frozenset(ev) for _, _, _, eb, ev, _ in bucket if eb == base and ev}
                        if vols:
                            distinct_vols.add(frozenset(vols))
                        if vols == e_vols or len(distinct_vols) <= 1:
                            return True
                if len(nt) >= 8 and len(e_nt) >= 8 and vols == e_vols and SequenceMatcher(None, nt, e_nt).ratio() >= 0.94:
                    return True

        if len(nt.split()) >= 3 and nt in self.by_exact_title:
            a_low = _ascii_fold(author).lower()
            for e_auth in self.by_exact_title[nt]:
                if SequenceMatcher(None, a_low, _ascii_fold(e_auth).lower()).ratio() >= 0.88:
                    return True
        return False


# --------------------------------------------------------------------------- #
# Registro de autores hispanohablantes y localización a español original
# --------------------------------------------------------------------------- #
# Clave = norm_author_key(autor) -> Nombre canónico correctamente tildado
HISPANIC_AUTHORS_CANON: dict[str, str] = {
    "gabriel garcia marquez": "Gabriel García Márquez",
    "borges jorge luis": "Jorge Luis Borges",
    "cortazar julio": "Julio Cortázar",
    "allende isabel": "Isabel Allende",
    "cervantes de miguel saavedra": "Miguel de Cervantes",
    "cervantes de miguel": "Miguel de Cervantes",
    "carlos ruiz zafon": "Carlos Ruiz Zafón",
    "llosa mario vargas": "Mario Vargas Llosa",
    "bolano roberto": "Roberto Bolaño",
    "juan rulfo": "Juan Rulfo",
    "octavio paz": "Octavio Paz",
    "neruda pablo": "Pablo Neruda",
    "esquivel laura": "Laura Esquivel",
    "arturo perez reverte": "Arturo Pérez-Reverte",
    "federico garcia lorca": "Federico García Lorca",
    "eduardo galeano": "Eduardo Galeano",
    "enriquez mariana": "Mariana Enríquez",
    "samanta schweblin": "Samanta Schweblin",
    "javier marias": "Javier Marías",
    "duenas maria": "María Dueñas",
    "dolores redondo": "Dolores Redondo",
    "castillo javier": "Javier Castillo",
    "gabriel juan vasquez": "Juan Gabriel Vásquez",
    "abad faciolince hector": "Héctor Abad Faciolince",
    "carlos fuentes": "Carlos Fuentes",
    "ernesto sabato": "Ernesto Sabato",
    "horacio quiroga": "Horacio Quiroga",
    "adolfo bioy casares": "Adolfo Bioy Casares",
    "montero rosa": "Rosa Montero",
    "almudena grandes": "Almudena Grandes",
    "fernando vallejo": "Fernando Vallejo",
    "dario ruben": "Rubén Darío",
}

# Obras traducidas en Amazon US -> (título original español, sinopsis limpia en español)
EN_TO_ES_CANONICAL_WORKS: dict[tuple[str, str], tuple[str, str]] = {
    ("one hundred years of solitude", "gabriel garcia marquez"): (
        "Cien años de soledad",
        "Novela cumbre del realismo mágico escrita por Gabriel García Márquez que narra el auge y caída de siete generaciones de la familia Buendía en el pueblo mítico de Macondo.",
    ),
    ("love in the time of cholera", "gabriel garcia marquez"): (
        "El amor en los tiempos del cólera",
        "Novela de Gabriel García Márquez sobre el amor paciente y obsesivo de Florentino Ariza por Fermina Daza a lo largo de más de cincuenta años en el Caribe colombiano.",
    ),
    ("chronicle of a death foretold", "gabriel garcia marquez"): (
        "Crónica de una muerte anunciada",
        "Novela corta de Gabriel García Márquez que reconstruye paso a paso el asesinato inevitable de Santiago Nasar en un pueblo donde todos sabían que ocurriría.",
    ),
    ("no one writes to the colonel", "gabriel garcia marquez"): (
        "El coronel no tiene quien le escriba",
        "Novela de Gabriel García Márquez sobre un veterano coronel que espera semana tras semana en la pobreza la carta que reconozca su pensión.",
    ),
    ("of love and other demons", "gabriel garcia marquez"): (
        "Del amor y otros demonios",
        "Novela histórica de Gabriel García Márquez ambientada en la Cartagena colonial en torno a Sierva María de Todos los Ángeles y un sacerdote encargado de exorcizarla.",
    ),
    ("autumn of the patriarch", "gabriel garcia marquez"): (
        "El otoño del patriarca",
        "Novela de Gabriel García Márquez que retrata la soledad absoluta y la decadencia de un dictador caribeño eterno a lo largo de más de un siglo.",
    ),
    ("the autumn of the patriarch", "gabriel garcia marquez"): (
        "El otoño del patriarca",
        "Novela de Gabriel García Márquez que retrata la soledad absoluta y la decadencia de un dictador caribeño eterno a lo largo de más de un siglo.",
    ),
    ("memories of my melancholy whores", "gabriel garcia marquez"): (
        "Memoria de mis putas tristes",
        "Novela de Gabriel García Márquez protagonizada por un anciano periodista que descubre el amor verdadero al cumplir noventa años.",
    ),
    ("until august", "gabriel garcia marquez"): (
        "En agosto nos vemos",
        "Novela póstuma de Gabriel García Márquez sobre Ana Magdalena Bach, quien viaja cada agosto a una isla del Caribe para visitar la tumba de su madre.",
    ),
    ("the shadow of the wind", "carlos ruiz zafon"): (
        "La sombra del viento",
        "Novela de Carlos Ruiz Zafón ambientada en la Barcelona de posguerra en torno al Cementerio de los Libros Olvidados y la misteriosa búsqueda del autor Julián Carax.",
    ),
    ("the angels game", "carlos ruiz zafon"): (
        "El juego del ángel",
        "Novela de misterio e intriga gótica escrita por Carlos Ruiz Zafón en la que el joven escritor David Martín recibe un oscuro encargo literario en la Barcelona de los años veinte.",
    ),
    ("the prisoner of heaven", "carlos ruiz zafon"): (
        "El prisionero del cielo",
        "Tercera entrega de la saga del Cementerio de los Libros Olvidados escrita por Carlos Ruiz Zafón, centrada en los secretos ocultos de Fermín Romero de Torres y Daniel Sempere.",
    ),
    ("the labyrinth of the spirits", "carlos ruiz zafon"): (
        "El laberinto de los espíritus",
        "Desenlace monumental de la tetralogía del Cementerio de los Libros Olvidados de Carlos Ruiz Zafón, protagonizado por la investigadora Alicia Gris.",
    ),
    ("the prince of mist", "carlos ruiz zafon"): (
        "El príncipe de la niebla",
        "Novela juvenil de misterio y fantasía de Carlos Ruiz Zafón sobre una familia que se muda a la costa y descubre la oscura promesa del mago Caín.",
    ),
    ("the house of the spirits", "allende isabel"): (
        "La casa de los espíritus",
        "Primera novela de Isabel Allende que recorre cuatro generaciones de la familia Trueba a través del amor, el realismo mágico y las convulsiones políticas de Chile.",
    ),
    ("a long petal of the sea", "allende isabel"): (
        "Largo pétalo de mar",
        "Novela histórica de Isabel Allende sobre Víctor Dalmau y Roser Bruguera, exiliados de la Guerra Civil española que parten hacia Chile a bordo del barco Winnipeg.",
    ),
    ("the japanese lover", "allende isabel"): (
        "El amante japonés",
        "Novela de Isabel Allende que entrelaza la historia de amor secreta de casi setenta años entre Alma Belasco y el jardinero Ichimei Fukuda.",
    ),
    ("the wind knows my name", "allende isabel"): (
        "El viento conoce mi nombre",
        "Novela de Isabel Allende que conecta dos historias de desarraigo infantil separadas por décadas, desde la Viena de 1938 hasta la frontera actual.",
    ),
    ("daughter of fortune", "allende isabel"): (
        "Hija de la fortuna",
        "Novela de aventuras históricas de Isabel Allende en la que Eliza Sommers viaja de Valparaíso a la California de la fiebre del oro en busca de su gran amor.",
    ),
    ("portrait in sepia", "allende isabel"): (
        "Retrato en sepia",
        "Novela de Isabel Allende sobre Aurora del Valle y la reconstrucción de los secretos de familia entre San Francisco y el Chile del siglo XIX.",
    ),
    ("city of the beasts", "allende isabel"): (
        "La ciudad de las bestias",
        "Novela juvenil de aventuras de Isabel Allende protagonizada por Alexander Cold durante una expedición por las profundidades del Amazonas.",
    ),
    ("ines of my soul", "allende isabel"): (
        "Inés del alma mía",
        "Novela histórica de Isabel Allende basada en la vida de Inés Suárez y los primeros años de la conquista de Chile.",
    ),
    ("like water for chocolate", "esquivel laura"): (
        "Como agua para chocolate",
        "Novela de realismo mágico escrita por Laura Esquivel que mezcla recetas de cocina tradicional mexicana con la historia de amor prohibido entre Tita y Pedro.",
    ),
    ("don quixote", "cervantes de miguel saavedra"): (
        "Don Quijote de la Mancha",
        "Obra maestra universal de Miguel de Cervantes que relata las aventuras del hidalgo Alonso Quijano y su escudero Sancho Panza por los caminos de La Mancha.",
    ),
    ("don quixote", "cervantes de miguel"): (
        "Don Quijote de la Mancha",
        "Obra maestra universal de Miguel de Cervantes que relata las aventuras del hidalgo Alonso Quijano y su escudero Sancho Panza por los caminos de La Mancha.",
    ),
    ("the savage detectives", "bolano roberto"): (
        "Los detectives salvajes",
        "Novela emblemática de Roberto Bolaño sobre la búsqueda de la poeta Cesárea Tinajero por los fundadores del realvisceralismo, Arturo Belano y Ulises Lima.",
    ),
    ("hopscotch", "cortazar julio"): (
        "Rayuela",
        "Novela experimental de Julio Cortázar sobre Horacio Oliveira entre París y Buenos Aires, célebre por sus múltiples órdenes de lectura.",
    ),
    ("the feast of the goat", "llosa mario vargas"): (
        "La fiesta del chivo",
        "Novela histórica de Mario Vargas Llosa que retrata los últimos días de la dictadura de Rafael Leónidas Trujillo en la República Dominicana.",
    ),
    ("aunt julia and the scriptwriter", "llosa mario vargas"): (
        "La tía Julia y el escribidor",
        "Novela de Mario Vargas Llosa ambientada en la Lima de los años cincuenta en torno a un joven escritor y el excéntrico libretista de radionovelas Pedro Camacho.",
    ),
    ("the time in between", "duenas maria"): (
        "El tiempo entre costuras",
        "Novela de intriga histórica de María Dueñas sobre la joven modista Sira Quiroga en el Madrid de entreguerras, Tánger y Tetuán.",
    ),
    ("the queen of the south", "arturo perez reverte"): (
        "La reina del sur",
        "Novela de Arturo Pérez-Reverte que sigue la trayectoria de Teresa Mendoza desde Sinaloa hasta convertirse en una poderosa figura en el estrecho de Gibraltar.",
    ),
    ("the club dumas", "arturo perez reverte"): (
        "El club Dumas",
        "Thriller literario de Arturo Pérez-Reverte protagonizado por el buscador de libros antiguos Lucas Corso tras un manuscrito de Alejandro Dumas y un grimorio maldito.",
    ),
    ("our share of night", "enriquez mariana"): (
        "Nuestra parte de noche",
        "Novela de terror gótico contemporáneo escrita por Mariana Enríquez ambientada entre la dictadura argentina y los secretos sobrenaturales de una sociedad oculta.",
    ),
    ("fever dream", "samanta schweblin"): (
        "Distancia de rescate",
        "Novela corta de suspenso psicológico escrita por Samanta Schweblin sobre los temores de una madre en el campo argentino y la amenaza invisible que rodea a su hija.",
    ),
    ("a sunny place for shady people", "enriquez mariana"): (
        "Un lugar soleado para gente sombría",
        "Colección de doce relatos de terror y horror contemporáneo escrita por Mariana Enríquez.",
    ),
    ("the things we lost in the fire", "enriquez mariana"): (
        "Las cosas que perdimos en el fuego",
        "Libro de cuentos de Mariana Enríquez que explora lo macabro y la violencia cotidiana en la Argentina contemporánea.",
    ),
    ("open veins of latin america", "eduardo galeano"): (
        "Las venas abiertas de América Latina",
        "Ensayo histórico de Eduardo Galeano sobre el saqueo económico y político de América Latina desde la época colonial hasta el siglo XX.",
    ),
}


def localize_hispanic_amazon_book(
    title: str,
    author: str,
    genres: list[str],
    pub_year: int | None,
) -> tuple[str, str, str | None] | None:
    """Devuelve (título_es, autor_es, sinopsis_es) para obras de autores hispanohablantes.

    Retorna:
      * ``None`` si el autor no pertenece al registro hispanohablante (procesar en inglés).
      * ``("", "", None)`` si es traducción al inglés sin mapeo directo (omitir; Open Library
        aportará las obras originales en español limpio).
      * ``(es_title, es_author, es_synopsis_o_None)`` cuando debe conservarse en español.
    """
    a_key = norm_author_key(author)
    canon_author = HISPANIC_AUTHORS_CANON.get(a_key)
    if canon_author is None:
        return None

    t_key = norm_title_key(title, a_key)
    mapped = EN_TO_ES_CANONICAL_WORKS.get((t_key, a_key))
    if mapped is not None:
        return mapped[0], canon_author, mapped[1]

    # Si el título que viene de Amazon ya está en español (y no trae "[English Edition]" ni similar)
    if "[english" not in title.lower() and is_text_in_lang(title, "es", allow_short=True):
        genre_label = genres[0] if genres else "Fiction"
        yr = f" publicada en {pub_year}" if pub_year else ""
        es_syn = f"{title} es una obra de {genre_label} escrita por {canon_author}{yr}."
        return title, canon_author, es_syn

    # Traducción inglesa no mapeada de un autor hispanohablante -> descartar para evitar inglés
    return "", "", None


def is_text_in_lang(text: str, lang: str, allow_short: bool = False) -> bool:
    """Valida si un texto o frase inicial coincide con el idioma objetivo ('en' o 'es')."""
    if not text or _CTRL_OR_MOJIBAKE_RE.search(text) or _NON_LATIN_RE.search(text):
        return False
    low = text.lower()
    words = re.findall(r"[a-záéíóúüñ]+", low)
    if not words:
        return False
    if not allow_short and len(words) < 6:
        return False

    other_hits = sum(1 for w in words if w in _OTHER_FOREIGN_WORDS)
    en_hits = sum(1 for w in words if w in _EN_FUNCTION_WORDS)
    es_hits = sum(1 for w in words if w in _ES_FUNCTION_WORDS)

    # Cualquier señal clara de italiano/portugués/francés/alemán/polaco se rechaza siempre
    if other_hits >= 2 and other_hits >= max(en_hits, es_hits):
        return False

    if lang == "es":
        has_es_accent = bool(re.search(r"[áéíóúüñ¿¡]", low))
        if allow_short:
            if en_hits > es_hits and en_hits >= 2:
                return False
            return (es_hits >= 1 or has_es_accent) and other_hits == 0
        return (es_hits >= 2 or (es_hits >= 1 and has_es_accent)) and es_hits >= en_hits and other_hits <= 1

    # lang == "en"
    if allow_short:
        return en_hits >= 1 and es_hits <= en_hits and other_hits == 0
    return en_hits >= 2 and es_hits < en_hits and other_hits <= 1


def is_clean_en_or_es_book(
    title: str,
    author: str,
    synopsis: str,
    expected_lang: str = "en",
) -> bool:
    """Garantiza título, autor y sinopsis limpios en inglés o español, sin mojibake ni otros idiomas."""
    if not has_clean_punctuation(title, author):
        return False
    combined = f"{title} {author} {synopsis}"
    if _CTRL_OR_MOJIBAKE_RE.search(combined) or _NON_LATIN_RE.search(combined):
        return False
    syn_low = synopsis.lower()
    if any(sp in syn_low for sp in _SPAM_SNIPPETS):
        return False

    words = re.findall(r"[a-záéíóúüñ]+", syn_low)
    if len(words) < 8:
        return False

    other_hits = sum(1 for w in words if w in _OTHER_FOREIGN_WORDS)
    en_hits = sum(1 for w in words if w in _EN_FUNCTION_WORDS)
    es_hits = sum(1 for w in words if w in _ES_FUNCTION_WORDS)

    if expected_lang == "es":
        if en_hits > es_hits + 1:
            return False
        if (es_hits < 2 and not re.search(r"[áéíóúüñ]", syn_low)) or other_hits >= es_hits:
            return False
        # Evitar que un título traducido al inglés entre en expected_lang='es'
        t_words = re.findall(r"[a-z]+", _ascii_fold(title))
        t_en = sum(1 for w in t_words if w in {"the", "and", "of", "in", "for", "with", "who", "until", "sunny", "place", "labyrinths"})
        t_es = sum(1 for w in t_words if w in {"el", "la", "los", "las", "de", "del", "en", "y", "un", "una", "por", "para", "con"})
        if t_en >= 1 and t_es == 0:
            return False
        return True

    # expected_lang == "en"
    if en_hits < 2 or (en_hits / len(words)) < 0.12:
        return False
    if other_hits >= en_hits or es_hits >= en_hits:
        return False
    t_words = re.findall(r"[a-záéíóúüñ]+", title.lower())
    if t_words:
        t_foreign = sum(1 for w in t_words if w in _OTHER_FOREIGN_WORDS or w in _ES_FUNCTION_WORDS)
        t_en = sum(1 for w in t_words if w in _EN_FUNCTION_WORDS)
        if t_foreign >= 2 and t_foreign > t_en:
            return False
    return True


def is_clean_english_book(title: str, author: str, synopsis: str) -> bool:
    """Alias retrocompatible para validar libros estrictamente en inglés."""
    return is_clean_en_or_es_book(title, author, synopsis, expected_lang="en")


# --------------------------------------------------------------------------- #
# Mapeo estricto de subjects de Open Library -> géneros canónicos BookSphere
# --------------------------------------------------------------------------- #
_OL_SUBJECT_RULES: tuple[tuple[str, str], ...] = (
    ("science fiction", "Science Fiction"),
    ("ciencia ficcion", "Science Fiction"),
    ("sci-fi", "Science Fiction"),
    ("dystopia", "Science Fiction"),
    ("cyberpunk", "Science Fiction"),
    ("space opera", "Science Fiction"),
    ("fantasy", "Fantasy"),
    ("fantasia", "Fantasy"),
    ("realismo magico", "Fantasy"),
    ("magic", "Fantasy"),
    ("romantasy", "Fantasy"),
    ("thriller", "Thriller"),
    ("suspense", "Thriller"),
    ("espionage", "Thriller"),
    ("mystery", "Mystery"),
    ("misterio", "Mystery"),
    ("detective", "Mystery"),
    ("crime", "Mystery"),
    ("policiaca", "Mystery"),
    ("murder", "Mystery"),
    ("horror", "Horror"),
    ("terror", "Horror"),
    ("gothic", "Horror"),
    ("ghost stories", "Horror"),
    ("historical fiction", "Historical Fiction"),
    ("novela historica", "Historical Fiction"),
    ("romance", "Romance"),
    ("love stories", "Romance"),
    ("young adult", "Young Adult"),
    ("juvenil", "Young Adult"),
    ("teen", "Young Adult"),
    ("juvenile", "Children's"),
    ("children", "Children's"),
    ("infantil", "Children's"),
    ("picture books", "Children's"),
    ("classic", "Classics"),
    ("clasicos", "Classics"),
    ("biography", "Biography & Memoir"),
    ("biografia", "Biography & Memoir"),
    ("autobiography", "Biography & Memoir"),
    ("memoir", "Biography & Memoir"),
    ("memorias", "Biography & Memoir"),
    ("graphic novel", "Comics & Graphic Novels"),
    ("comics", "Comics & Graphic Novels"),
    ("cookbook", "Cooking"),
    ("cooking", "Cooking"),
    ("food & wine", "Cooking"),
    ("business", "Business"),
    ("economics", "Business"),
    ("management", "Business"),
    ("finance", "Business"),
    ("investing", "Business"),
    ("science", "Science"),
    ("physics", "Science"),
    ("biology", "Science"),
    ("mathematics", "Science"),
    ("computer", "Science"),
    ("technology", "Science"),
    ("astronomy", "Science"),
    ("medicine", "Science"),
    ("philosophy", "Philosophy"),
    ("filosofia", "Philosophy"),
    ("ethics", "Philosophy"),
    ("stoicism", "Philosophy"),
    ("psychology", "Psychology"),
    ("psicologia", "Psychology"),
    ("mental health", "Psychology"),
    ("cognitive", "Psychology"),
    ("self-help", "Self Help"),
    ("self-improvement", "Self Help"),
    ("personal growth", "Self Help"),
    ("habits", "Self Help"),
    ("productivity", "Self Help"),
    ("history", "History"),
    ("historia", "History"),
    ("world war", "History"),
    ("civilization", "History"),
    ("politics", "Politics"),
    ("politica", "Politics"),
    ("government", "Politics"),
    ("social science", "Politics"),
    ("sociology", "Politics"),
    ("religion", "Religion & Spirituality"),
    ("christian", "Religion & Spirituality"),
    ("spirituality", "Religion & Spirituality"),
    ("theology", "Religion & Spirituality"),
    ("humor", "Humor"),
    ("comedy", "Humor"),
    ("satire", "Humor"),
    ("art", "Art"),
    ("music", "Art"),
    ("photography", "Art"),
    ("architecture", "Art"),
    ("poetry", "Poetry"),
    ("poesia", "Poetry"),
    ("poems", "Poetry"),
    ("travel", "Travel"),
    ("viajes", "Travel"),
    ("sports", "Sports & Outdoors"),
    ("outdoor", "Sports & Outdoors"),
    ("crafts", "Lifestyle & Hobbies"),
    ("hobbies", "Lifestyle & Hobbies"),
    ("gardening", "Lifestyle & Hobbies"),
    ("education", "Education"),
    ("study guides", "Education"),
    ("teaching", "Education"),
    ("adventure", "Adventure"),
    ("aventuras", "Adventure"),
    ("fiction", "Fiction"),
    ("ficcion", "Fiction"),
    ("novela", "Fiction"),
)


def map_openlibrary_subjects(subjects: list[str], hint_genre: str | None = None) -> list[str]:
    """Mapea subjects de Open Library estrictamente a la taxonomía canónica BookSphere."""
    found: list[str] = []
    if hint_genre and hint_genre in CANONICAL_GENRES:
        found.append(hint_genre)

    joined_list = [_ascii_fold(s) for s in (subjects or [])[:35] if s]
    for subj in joined_list:
        for needle, canon in _OL_SUBJECT_RULES:
            if needle in subj and canon not in found:
                found.append(canon)
                if len(found) >= 5:
                    break
        if len(found) >= 5:
            break

    if not found:
        return ["Fiction"]
    specific = [g for g in found if g not in GENERIC_GENRES]
    generic = [g for g in found if g in GENERIC_GENRES]
    return (specific + generic)[:5]


def _pick_clean_isbn(raw_isbns: object) -> str | None:
    if not isinstance(raw_isbns, list):
        return None
    for val in raw_isbns[:10]:
        cleaned = re.sub(r"[^0-9xX]", "", str(val or "")).upper()
        if len(cleaned) == 13:
            return cleaned
    for val in raw_isbns[:10]:
        cleaned = re.sub(r"[^0-9xX]", "", str(val or "")).upper()
        if len(cleaned) == 10:
            return cleaned
    return None


def _build_ol_synopsis(
    doc: dict[str, Any],
    title: str,
    author: str,
    genres: list[str],
    pub_year: int | None,
    lang: str,
) -> str:
    """Construye una sinopsis limpia en inglés o español validando que first_sentence coincida."""
    fs_raw = doc.get("first_sentence")
    fs_text = ""
    if isinstance(fs_raw, list) and fs_raw:
        fs_text = clean_text(fs_raw[0])
    elif isinstance(fs_raw, str):
        fs_text = clean_text(fs_raw)

    # Descartar frases iniciales en otros idiomas (ej. italiano/portugués adjuntados por Open Library)
    if fs_text and not is_text_in_lang(fs_text, lang, allow_short=False):
        fs_text = ""

    raw_subjects = [
        clean_text(s)
        for s in (doc.get("subject") or [])[:12]
        if s
        and not _CTRL_OR_MOJIBAKE_RE.search(str(s))
        and not _NON_LATIN_RE.search(str(s))
        and 3 <= len(str(s)) <= 45
        and "nyt:" not in str(s).lower()
        and "accessible book" not in str(s).lower()
        and "protected daisy" not in str(s).lower()
    ]
    topic_str = ", ".join(raw_subjects[:4])
    genre_label = genres[0] if genres else "Fiction"

    if lang == "es":
        year_clause = f" publicada en {pub_year}" if pub_year else ""
        editorial = f"{title} es una obra de {genre_label} escrita por {author}{year_clause}."
        if topic_str and is_text_in_lang(topic_str, "es", allow_short=True):
            editorial += f" Temas principales: {topic_str}."
    else:
        year_clause = f" published in {pub_year}" if pub_year else ""
        editorial = f"{title} is a {genre_label} work by {author}{year_clause}."
        if topic_str:
            editorial += f" Key themes and subjects include {topic_str}."

    if fs_text and len(fs_text) >= 40:
        combined = f"{fs_text} {editorial}"
    else:
        combined = editorial
    return combined[:1200].strip()


def normalize_openlibrary_doc(
    doc: dict[str, Any],
    hint_genre: str | None = None,
    expected_lang: str = "en",
    catalog_source: str = "openlibrary_curated",
    in_recommender: int = 0,
) -> dict[str, Any] | None:
    """Convierte un documento de Open Library en registro limpio o ``None`` si no cumple calidad."""
    langs = [str(x).lower() for x in (doc.get("language") or [])]
    if expected_lang == "es":
        if langs and "spa" not in langs:
            return None
    else:
        if langs and "eng" not in langs:
            return None

    work_key = str(doc.get("key") or "").strip()
    if not work_key.startswith("/works/"):
        return None

    raw_title = str(doc.get("title") or "")
    authors_raw = doc.get("author_name") or []
    if not isinstance(authors_raw, list) or not authors_raw:
        return None
    raw_author_joined = " ".join(str(a) for a in authors_raw[:3])
    if is_non_book_format(raw_title, raw_author_joined):
        return None

    title = clean_title(raw_title)
    cleaned_authors = [clean_author(a) for a in authors_raw[:3]]
    cleaned_authors = [a for a in cleaned_authors if a]
    if not cleaned_authors:
        return None

    # Normalizar nombre tildado si es autor hispanohablante
    first_a_key = norm_author_key(cleaned_authors[0])
    if expected_lang == "en" and first_a_key in HISPANIC_AUTHORS_CANON:
        # En consultas en inglés jamás reintroducir traducciones de autores hispanohablantes
        return None
    if first_a_key in HISPANIC_AUTHORS_CANON:
        cleaned_authors[0] = HISPANIC_AUTHORS_CANON[first_a_key]
    author = ", ".join(dict.fromkeys(cleaned_authors))

    if is_non_book_format(title, author) or not has_clean_punctuation(title, author):
        return None

    # Descartar fichas genéricas sin soporte suficiente de una franquicia sola (ej. 'Harry Potter')
    if title.lower() in {"harry potter", "fantastic beasts"} and int(doc.get("ratings_count") or 0) < 25:
        return None

    c_key = canonical_work_key(title, author)
    if not c_key:
        return None

    pub_year_raw = doc.get("first_publish_year")
    pub_year: int | None = None
    if pub_year_raw is not None:
        try:
            y = int(pub_year_raw)
            if 1500 <= y <= 2026:
                pub_year = y
        except (TypeError, ValueError):
            pass

    subjects = doc.get("subject") or []
    genres = map_openlibrary_subjects(subjects, hint_genre=hint_genre)
    synopsis = _build_ol_synopsis(doc, title, author, genres, pub_year, lang=expected_lang)
    if not is_clean_en_or_es_book(title, author, synopsis, expected_lang=expected_lang):
        return None

    pages_raw = doc.get("number_of_pages_median")
    pages: int | None = None
    if pages_raw is not None:
        try:
            p = int(pages_raw)
            if 15 <= p <= 5000:
                pages = p
        except (TypeError, ValueError):
            pass

    isbn = _pick_clean_isbn(doc.get("isbn"))

    avg_raw = doc.get("ratings_average")
    cnt_raw = doc.get("ratings_count")
    avg_rating: float | None = None
    ratings_count = 0
    try:
        cnt_val = int(cnt_raw or 0)
        avg_val = float(avg_raw) if avg_raw is not None else 0.0
        if cnt_val >= 25 and 1.0 <= avg_val <= 5.0:
            avg_rating = round(avg_val, 2)
            ratings_count = cnt_val
    except (TypeError, ValueError):
        pass

    return {
        "canon_key": c_key,
        "title": title,
        "author": author,
        "genres": genres,
        "primary_genre": genres[0],
        "synopsis": synopsis,
        "publish_year": pub_year,
        "pages": pages,
        "isbn": isbn,
        "avg_rating": avg_rating,
        "ratings_count": ratings_count,
        "catalog_source": catalog_source,
        "in_recommender": int(in_recommender),
        "external_key": work_key,
    }


def query_openlibrary(
    params: dict[str, Any],
    client: httpx.Client,
    hint_genre: str | None = None,
    expected_lang: str = "en",
    catalog_source: str = "openlibrary_curated",
    in_recommender: int = 0,
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Ejecuta una consulta a Open Library con reintentos y devuelve documentos normalizados."""
    full_params = dict(params)
    full_params.setdefault("fields", OPENLIBRARY_FIELDS)

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.get(OPENLIBRARY_SEARCH_URL, params=full_params, headers=OPENLIBRARY_HEADERS)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.2 * (attempt + 1))
                continue
            resp.raise_for_status()
            docs = resp.json().get("docs") or []
            out: list[dict[str, Any]] = []
            for doc in docs:
                norm = normalize_openlibrary_doc(
                    doc,
                    hint_genre=hint_genre,
                    expected_lang=expected_lang,
                    catalog_source=catalog_source,
                    in_recommender=in_recommender,
                )
                if norm is not None:
                    out.append(norm)
            return out
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            time.sleep(1.0 * (attempt + 1))

    if last_exc is not None:
        print(f"  [Aviso Open Library] consulta omitida ({params.get('q') or params.get('author')}): {last_exc}")
    return []
