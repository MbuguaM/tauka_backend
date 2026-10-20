"""Fidel -> a learner-friendly Latin transliteration. Deterministic, no AI.

Features addendum B4 and X-16: Amharic in a summary is always the lesson's
own Fidel with a transliteration beside it. The lessons store Fidel and an
English gloss but no transliteration, and an AI-written one could be wrong in
ways a non-Amharic reader can't see, so this is a table lookup.

The Ethiopic block lays each consonant family out as 8 code points: the seven
orders (ä u i a e ə o) and a labialised -wa. Labialised families (ቈ ኈ ኰ ዀ ጐ)
use orders 0, 2-5 only. The 6th order (ə) loses its vowel at the end of a word
and before a full vowel, which is how a learner hears it. Gemination marks are dropped.

    >>> latin("ኢትዮጵያ"), latin("እንደምን")
    ("ityop'ya", "əndemən")
"""

_VOWELS = ("e", "u", "i", "a", "ē", "ə", "o", "wa")
_LAB_VOWELS = {0: "we", 2: "wi", 3: "wa", 4: "wē", 5: "wə"}

# Family base code point -> consonant ("" for the vowel carriers አ and ዐ).
_FAMILIES = {
    0x1200: "h", 0x1208: "l", 0x1210: "h", 0x1218: "m", 0x1220: "s", 0x1228: "r", 0x1230: "s", 0x1238: "sh",
    0x1240: "q", 0x1250: "qh", 0x1260: "b", 0x1268: "v", 0x1270: "t", 0x1278: "ch", 0x1280: "h", 0x1290: "n",
    0x1298: "ny", 0x12A0: "", 0x12A8: "k", 0x12B8: "kh", 0x12C8: "w", 0x12D0: "", 0x12D8: "z", 0x12E0: "zh",
    0x12E8: "y", 0x12F0: "d", 0x12F8: "dd", 0x1300: "j", 0x1308: "g", 0x1318: "ng", 0x1320: "t'", 0x1328: "ch'",
    0x1330: "p'", 0x1338: "ts'", 0x1340: "ts'", 0x1348: "f", 0x1350: "p",
}
_LABIALISED = {0x1248: "q", 0x1258: "qh", 0x1288: "h", 0x12B0: "k", 0x12C0: "kh", 0x1310: "g"}
_PUNCT = {"፡": " ", "።": ".", "፣": ",", "፤": ";", "፥": ":", "፦": ":", "፧": "?", "፨": "¶"}
_GEMINATION = {"፟", "፝", "፞"}


def is_fidel(ch: str) -> bool:
    return "ሀ" <= ch <= "᎟" or "ⶀ" <= ch <= "⷟" or "꬀" <= ch <= "꬯"


def has_fidel(text: str) -> bool:
    return any(is_fidel(c) for c in text or "")


def _syllable(ch: str) -> str | None:
    cp = ord(ch)
    base = cp - (cp - 0x1200) % 8
    order = (cp - 0x1200) % 8
    if base in _LABIALISED:
        v = _LAB_VOWELS.get(order)
        return _LABIALISED[base] + v if v else None
    if base in _FAMILIES:
        cons = _FAMILIES[base]
        if cons == "":
            return ("a", "u", "i", "a", "e", "ə", "o", "wa")[order]
        return cons + _VOWELS[order]
    return None


def latin(text: str) -> str:
    """6th-order (ə) syllables lose the vowel at the end of a word and, except
    at the start of one, before a syllable with a full vowel (ኢትዮጵያ ->
    ityop'ya, ይመስገን -> yəmesgen), which is how they sound."""
    parts: list[tuple[str, bool]] = []          # (text, is a 6th-order consonant syllable)
    for ch in text or "":
        if ch in _GEMINATION:
            continue
        if ch in _PUNCT:
            parts.append((_PUNCT[ch], False))
            continue
        syl = _syllable(ch) if is_fidel(ch) else None
        if syl is None:
            parts.append((ch, False))
        else:
            parts.append((syl, syl.endswith("ə") and len(syl) > 1))
    out = []
    for i, (txt, sixth) in enumerate(parts):
        if sixth:
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            final = nxt is None or not nxt[0] or not nxt[0][0].isalpha() and nxt[0][0] != "'"
            full_next = nxt is not None and not nxt[1] and nxt[0][:1].isalpha() and not final
            word_start = i == 0 or not parts[i - 1][0][-1:].isalpha()
            if final or (full_next and not word_start):
                txt = txt[:-1]
        out.append(txt)
    return "".join(out).strip()
