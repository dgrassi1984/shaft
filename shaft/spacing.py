"""Put back the space a model drops between a word and a number ("alle09:30", "13nov", "HTTP200").

Only boundaries that are never an identifier in prose are touched; code spans, fenced blocks, URLs and paths
are left as they are.
"""
import re

MONTHS = ("gen|gennaio|feb|febbraio|mar|marzo|apr|aprile|mag|maggio|giu|giugno|lug|luglio|ago|agosto|set|sett|"
          "settembre|ott|ottobre|nov|novembre|dic|dicembre|jan|january|february|march|april|may|jun|june|jul|july|"
          "aug|august|sep|sept|september|oct|october|november|dec|december")
MONTHS_UNAMBIGUOUS = "|".join(m for m in MONTHS.split("|") if m not in ("gen", "mar", "set", "sett", "sep", "sept", "dec", "may", "ago"))
# Words that are never glued to a number in a name: articles, prepositions, time words (it, en).
STOPWORDS = ("il|lo|la|le|gli|un|una|del|dello|della|dei|degli|delle|dal|dallo|dalla|dai|dagli|dalle|al|allo|alla|"
             "ai|agli|alle|nel|nello|nella|nei|negli|nelle|sul|sullo|sulla|sui|sugli|sulle|col|con|per|tra|fra|da|di|"
             "fino|entro|dopo|prima|ore|ora|circa|oltre|sotto|sopra|verso|contro|ogni|tutti|tutte|resta|restano|"
             "lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica|lun|mer|gio|ven|sab|dom|"
             "at|on|by|the|of|to|from|until|since")
_W = r"[A-Za-zÀ-ÖØ-öø-ÿ]"
_START = rf"(?<![\w/\\.@#:+%$\-])"          # not inside a path, handle, version or identifier
_END = r"(?![\w/\\\-]|\.\w|:\d)"           # not followed by more of a name
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
_RULES = [
    # a word glued to a date: "autonomo13nov", "controllo12novembre"
    re.compile(rf"{_START}({_W}{{2,}})(\d{{1,2}}(?:{MONTHS}))(?={_TIME}|\b)(?![\w/\\\-]*\.\w)"),
    # a word glued to a clock time: "alle09:30", "ciclo10:20", "novembre10:20"
    re.compile(rf"{_START}({_W}{{2,}})({_TIME}){_END}"),
    # a day glued to a month and the month glued to what follows: "13nov", "13novembre10:22"
    re.compile(rf"{_START}(\d{{1,2}})((?:{MONTHS}))(?={_TIME}|\b)(?![\w/\\\-]*\.\w)"),
    # a month glued to a day or year: "novembre12", "ottobre2026" (not gen/mar/set/sep/dec/may/ago: "Gen4", "set1")
    re.compile(rf"{_START}((?:{MONTHS_UNAMBIGUOUS}))(\d{{1,4}}){_END}"),
    # an article or preposition glued to a number, lowercase only ("IL6" is a name): "del12", "fino16"
    re.compile(rf"{_START}((?:{STOPWORDS}))(\d[\d.,]*){_END}"),
    # an HTTP status: "HTTP200"
    re.compile(rf"{_START}(HTTP)(\d{{3}}){_END}"),
]
_SKIP = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|\b[\w.\-]+/[\w./\-]*", re.S)


def _fix(text: str) -> str:
    for _ in range(3):                       # "13nov10:22" needs two passes
        before = text
        for rule in _RULES:
            text = rule.sub(lambda m: m.group(1) + " " + m.group(2), text)
        if text == before:
            break
    return text


def respace(text: str) -> str:
    """The text with a space put back at each glued word/number boundary; code, URLs and paths untouched."""
    if not text or not any(c.isdigit() for c in text):
        return text
    out, last = [], 0
    for m in _SKIP.finditer(text):
        out += [_fix(text[last:m.start()]), m.group()]
        last = m.end()
    out.append(_fix(text[last:]))
    return "".join(out)
