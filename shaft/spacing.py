"""Put back the space a model drops between a word and a number ("alle09:30", "13nov", "riga571", "24mesi").

A prose word glued to a number, and a number glued to a prose word, get their space back. Names are left alone:
code spans, fenced blocks, URLs, paths and hex ids are skipped; all-caps and camel-case names ("IL6", "MG4",
"UniCredit16"), stems of three letters or fewer ("sha256", "md5", "Gen4", "v0"), a stem glued to a year
("user1984"), units ("30min", "72h") and a few names measured in instances' own prose before the glue began
("python3", "base64", "Sheet1") keep their form.
"""
import re

MONTHS = ("gen|gennaio|feb|febbraio|mar|marzo|apr|aprile|mag|maggio|giu|giugno|lug|luglio|ago|agosto|set|sett|"
          "settembre|ott|ottobre|nov|novembre|dic|dicembre|jan|january|february|march|april|may|jun|june|jul|july|"
          "aug|august|sep|sept|september|oct|october|november|dec|december")
MONTHS_UNAMBIGUOUS = "|".join(m for m in MONTHS.split("|") if m not in ("gen", "mar", "set", "sett", "sep", "sept", "dec", "may", "ago"))
# Words that are never glued to a number in a name: articles, conjunctions, prepositions, time words (it, en).
STOPWORDS = ("il|lo|la|le|gli|un|una|del|dello|della|dei|degli|delle|dal|dallo|dalla|dai|dagli|dalle|al|allo|alla|"
             "ai|agli|alle|nel|nello|nella|nei|negli|nelle|sul|sullo|sulla|sui|sugli|sulle|col|con|per|tra|fra|da|di|"
             "fino|entro|dopo|prima|ore|ora|circa|oltre|sotto|sopra|verso|contro|ogni|tutti|tutte|resta|restano|"
             "e|ed|o|ad|in|"
             "lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica|lun|mer|gio|ven|sab|dom|"
             "at|on|by|the|of|to|from|until|since")
# Stems of four letters or more that name a thing with its number, seen glued in instances' prose before the glue.
NAMES = ("python", "base", "latin", "sheet", "knowbe", "kali", "auth")
# Letters right after a number that stay glued to it: a unit or currency ("30min", "72h", "7.6h", "30d", "12,08EUR").
UNITS = ("min", "sec", "mln", "mld", "bps", "kbps", "mbps", "gbps", "khz", "mhz", "ghz", "kwh", "mah", "fps", "dpi",
         "ppi", "rpm", "rpc")
CURRENCIES = ("EUR", "USD", "GBP", "CHF")
_STOP = frozenset(STOPWORDS.split("|")) | frozenset(MONTHS_UNAMBIGUOUS.split("|"))
_W = r"[A-Za-zÀ-ÖØ-öø-ÿ]"
_START = rf"(?<![\w/\\.@#:+%$\-])"          # not inside a path, handle, version or identifier
_END = r"(?![\w/\\\-]|\.\w|:\d)"           # not followed by more of a name
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
_TIME_END = r"(?![\w\\\-]|/(?!\d)|\.\w|:\d)"     # "12:11/12:22" is a pair of times, not a path
_RULES = [
    # a word glued to a date: "autonomo13nov", "controllo12novembre"
    re.compile(rf"{_START}({_W}{{2,}})(\d{{1,2}}(?:{MONTHS}))(?={_TIME}|\b)(?![\w/\\\-]*\.\w)"),
    # a word glued to a clock time: "alle09:30", "ciclo10:20", "novembre10:20"
    re.compile(rf"{_START}({_W}{{2,}})({_TIME}){_TIME_END}"),
    # a day glued to a month and the month glued to what follows: "13nov", "13novembre10:22"
    re.compile(rf"{_START}(\d{{1,2}})((?:{MONTHS}))(?={_TIME}|\b)(?![\w/\\\-]*\.\w)"),
    # a month glued to a day or year: "novembre12", "ottobre2026" (not gen/mar/set/sep/dec/may/ago: "Gen4", "set1")
    re.compile(rf"{_START}((?:{MONTHS_UNAMBIGUOUS}))(\d{{1,4}}){_END}"),
    # an article or preposition glued to a number, lowercase only ("IL6" is a name): "del12", "fino16"
    re.compile(rf"{_START}((?:{STOPWORDS}))(\d[\d.,]*){_END}"),
    # an HTTP status: "HTTP200"
    re.compile(rf"{_START}(HTTP)(\d{{3}}){_END}"),
]
# A run of letters and numbers standing alone in prose: "riga571", "per24mesi", "creato585entro3giorni".
_TOKEN = re.compile(r"(?<![\w/\\.@#:+%$\-])(?:[^\W_]|(?<=\d)[.,](?=\d))+(?![\w\\@\-]|/(?!\d)|\.\w)")
_RUN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")
_WORD = re.compile(r"[A-ZÀ-ÖØ-Þ]?[a-zß-öø-ÿ]+")
_VOWEL = re.compile(r"[aeiouyàáèéìíòóùú]", re.I)


def _is_word(run: str, least: int, first: bool = True) -> bool:
    """A prose word: lowercase, or capitalised when it opens the token (not "SGIC", "CdA", the "Dec" of "3Dec25"),
    with a vowel ("frmq" is a code)."""
    return (run in _STOP or
            (len(run) >= least and bool(_WORD.fullmatch(run)) and (first or run.islower())
             and bool(_VOWEL.search(run))))


def _split_glued(token: str) -> str:
    """The token with a space at each word/number boundary, when every piece of it is prose; otherwise unchanged.

    Prose pieces: a word (four letters or more first, three after a number: "riga571", "585entro"), an article,
    conjunction, preposition or month ("e2nov", "per24mesi"), and a number of up to six digits. A unit or a one-
    or two-letter suffix stays on its number ("30min", "7.6h"). Anything else (an id like "7dcndrb42vh3", "msg4",
    "SGIC18", "python3", "user1984") keeps its form."""
    runs = _RUN.findall(token)
    if len(runs) < 2 or "".join(runs) != token:
        return token
    pieces = []
    for i, run in enumerate(runs):
        if run[0].isdigit():
            if len(run.split(".")[0].split(",")[0]) > 6:
                return token
            pieces.append(run)
        elif i and runs[i - 1][0].isdigit() and (len(run) <= 2 or run.lower() in UNITS or run in CURRENCIES):
            if i == 1 or i < len(runs) - 1 or not _is_word(runs[i - 2], 4) or runs[i - 2] in _STOP:
                return token                 # "e2e", "4th", "x2028q4": a name, not a word glued to "30d"
            pieces[-1] += run
        elif _is_word(run, 4 if i == 0 else 3, first=i == 0):
            pieces.append(run)
        else:
            return token
    first = runs[0]
    if first.lower() in NAMES or (len(runs) == 2 and not first[0].isdigit() and first not in _STOP
                                  and re.fullmatch(r"(?:19|20)\d\d", runs[1])):
        return token
    return " ".join(pieces)


# Code, URLs, paths (a "/" with a letter or extension after it; "righe553/557." is prose) and hex ids ("dfae5750").
_SKIP = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|\b[\w.\-]+/(?=[\w./\-]*(?:[A-Za-z_]|\.\w))[\w./\-]*"
                   r"|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.S)


def _fix(text: str) -> str:
    for _ in range(3):                       # "13nov10:22" needs two passes
        before = text
        for rule in _RULES:
            text = rule.sub(lambda m: m.group(1) + " " + m.group(2), text)
        text = _TOKEN.sub(lambda m: _split_glued(m.group()), text)
        if text == before:
            break
    return text


def respace(text: str) -> str:
    """The text with a space put back at each glued word/number boundary; code, URLs, paths and names untouched."""
    if not text or not any(c.isdigit() for c in text):
        return text
    out, last = [], 0
    for m in _SKIP.finditer(text):
        out += [_fix(text[last:m.start()]), m.group()]
        last = m.end()
    out.append(_fix(text[last:]))
    return "".join(out)
