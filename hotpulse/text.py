"""Text helpers: URL canonicalisation, HTML cleaning, tokenising, similarity and date parsing."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|igshid|si$|cmpid|ocid|taid|source$)", re.I)

STOPWORDS = set("""a an the and or but if of to in on at by for with from as is are was were be been being this that these
those it its into over under than then there their they them he she his her we our you your i me my not no yes do does did
done will would can could should may might must has have had new now more most less very just also about after before
up down out off via how what why when where who which while all any each few other some such only own same so too s t
says said say according report reports update today week year years per vs amid""".split())

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w][\w\-\.']*", re.UNICODE)


def canonical_url(url: str) -> str:
    """Normalise a URL so the same article from different links dedupes."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    path = re.sub(r"/amp$", "", path)
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def squash(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def truncate(text: str | None, limit: int) -> str:
    text = squash(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:-") + "…"


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokens(text: str | None, keep_stop: bool = False) -> list[str]:
    words = [w.strip(".-'").lower() for w in _WORD_RE.findall(strip_accents(text or ""))]
    return [w for w in words if w and (keep_stop or (w not in STOPWORDS and len(w) > 1))]


def token_set(text: str | None) -> set[str]:
    return set(tokens(text))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: set[str], b: set[str]) -> float:
    """Share of the smaller set that appears in the larger (good for short headlines)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def title_hash(title: str) -> str:
    key = " ".join(sorted(token_set(title)))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def sentences(text: str) -> list[str]:
    text = squash(text)
    parts = re.split(r"(?<=[.!?۔])\s+(?=[A-Z0-9\"'“؀-ۿ])", text)
    return [p.strip() for p in parts if len(p.strip()) > 20]


def keyword_hits(text: str, keywords: list[str]) -> int:
    """Count keywords present (multi-word keywords matched as phrases)."""
    low = " " + " ".join(tokens(text, keep_stop=True)) + " "
    hits = 0
    for kw in keywords:
        kw_norm = " ".join(tokens(kw, keep_stop=True))
        if kw_norm and f" {kw_norm} " in low:
            hits += 1
    return hits


def detect_script_lang(text: str) -> str:
    """Very rough language guess from the writing system (enough to know if translation is needed)."""
    sample = text[:400]
    counts = {"ur": len(re.findall(r"[؀-ۿ]", sample)),
              "hi": len(re.findall(r"[ऀ-ॿ]", sample)),
              "zh": len(re.findall(r"[一-鿿]", sample)),
              "bn": len(re.findall(r"[ঀ-৿]", sample))}
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n > 15 else "en"


def to_iso(value) -> str | None:
    """Accept feedparser struct_time, RFC 2822 strings, ISO strings or epoch numbers."""
    if value is None or value == "":
        return None
    try:
        if hasattr(value, "tm_year"):
            dt = datetime(*value[:6], tzinfo=timezone.utc)
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            s = str(value).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

_DEADLINE_CTX = re.compile(r"(deadline|apply by|applications? (?:close|due)|closing date|last date|due date|until)"
                           r"[^.\n]{0,60}", re.I)
_D_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_D_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_D_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str) -> list[date]:
    out: list[date] = []
    for y, m, d in _D_ISO.findall(text):
        dt = _mk_date(int(y), int(m), int(d))
        if dt:
            out.append(dt)
    for mon, d, y in _D_MDY.findall(text):
        m = MONTHS.get(mon.lower())
        if m:
            dt = _mk_date(int(y), m, int(d))
            if dt:
                out.append(dt)
    for d, mon, y in _D_DMY.findall(text):
        m = MONTHS.get(mon.lower())
        if m:
            dt = _mk_date(int(y), m, int(d))
            if dt:
                out.append(dt)
    return out


def guess_deadline(text: str, today: date | None = None) -> str | None:
    """Find an application deadline near words like 'deadline' / 'apply by'."""
    today = today or date.today()
    for m in _DEADLINE_CTX.finditer(text or ""):
        window = text[m.start(): m.end() + 40]
        dates = [d for d in find_dates(window) if d >= today.replace(year=today.year - 1)]
        if dates:
            return dates[0].isoformat()
    return None


def normalise_deadline(value) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    m = _D_ISO.search(s)
    if m:
        dt = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return dt.isoformat() if dt else None
    found = find_dates(s)
    return found[0].isoformat() if found else None
