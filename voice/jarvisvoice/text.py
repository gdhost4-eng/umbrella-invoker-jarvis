"""Нормализация русских, латинских и числовых вариантов голосовых команд."""

import re

_ORDINAL_DIGITS = {
    "1": "первый",
    "2": "второй",
    "3": "третий",
    "4": "четвертый",
    "5": "пятый",
    "6": "шестой",
}
# «первая», «первого» → «первый» и т.д.
_ORDINAL_STEMS = (
    ("перв", "первый"),
    ("втор", "второй"),
    ("трет", "третий"),
    ("четверт", "четвертый"),
    ("пят", "пятый"),
    ("шест", "шестой"),
)

# Whisper иногда пишет названия латиницей: «Sunstrike» → «санстрайк».
_LATIN_WORDS = {
    "sun": "сан",
    "strike": "страйк",
    "cold": "колд",
    "snap": "снэп",
    "ghost": "гост",
    "walk": "волк",
    "ice": "айс",
    "wall": "волл",
    "emp": "эмп",
    "tornado": "торнадо",
    "alacrity": "алакрити",
    "chaos": "хаос",
    "meteor": "метеор",
    "forge": "форж",
    "spirit": "спирит",
    "spirits": "спиритс",
    "deafening": "дифнинг",
    "blast": "бласт",
    "cataclysm": "катаклизм",
    "quas": "квас",
    "wex": "векс",
    "exort": "экзорт",
    "invoke": "инвок",
    "bkb": "бкб",
    "blink": "блинк",
    "tp": "тп",
    "stop": "стоп",
    "hold": "холд",
    "eul": "еул",
    "euls": "еулс",
    "hex": "хекс",
    "refresher": "рефрешер",
    "midas": "мидас",
    "bot": "бот",
    "mute": "мут",
    "slot": "слот",
    "cast": "каст",
}
_LATIN_MULTI = {
    "sch": "ш",
    "sh": "ш",
    "ch": "ч",
    "zh": "ж",
    "kh": "х",
    "ts": "ц",
    "th": "т",
    "ph": "ф",
    "ck": "к",
    "qu": "кв",
    "ee": "и",
    "oo": "у",
    "ya": "я",
    "yu": "ю",
    "yo": "е",
}
_LATIN_SINGLE = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "дж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "й",
    "z": "з",
}
_LATIN_RE = re.compile("|".join(sorted(_LATIN_MULTI, key=len, reverse=True)) + "|[a-z]")

_NUM_SUFFIX_RE = re.compile(r"(\d+)\s*-\s*[а-я]{1,3}\b")
_TOKEN_RE = re.compile(r"[a-zа-я0-9]+")
_ORDINAL_TOKEN_RE = re.compile(r"(\d+)[а-я]{0,3}")
_REPEAT_RE = re.compile(r"(.)\1+")
# Звонкие → глухие, безударные гласные → «а/и/у»: так слово звучит, а не пишется.
_SOUNDS = str.maketrans(
    {
        "б": "п",
        "в": "ф",
        "г": "к",
        "д": "т",
        "ж": "ш",
        "з": "с",
        "щ": "ш",
        "о": "а",
        "я": "а",
        "е": "и",
        "ы": "и",
        "й": "и",
        "ю": "у",
        "ь": None,
        "ъ": None,
    }
)


def _translit_letters(word: str) -> str:
    return _LATIN_RE.sub(lambda m: _LATIN_MULTI.get(m.group(0)) or _LATIN_SINGLE.get(m.group(0), ""), word)


def _latin_to_cyrillic(word: str) -> str:
    if word in _LATIN_WORDS:
        return _LATIN_WORDS[word]
    # Разбиение на известные слова: sunstrike → sun + strike.
    best: list[list[str] | None] = [None] * (len(word) + 1)
    best[0] = []
    for i in range(len(word)):
        if best[i] is None:
            continue
        for j in range(i + 2, len(word) + 1):
            piece = _LATIN_WORDS.get(word[i:j])
            if piece is not None and (best[j] is None or len(best[i]) + 1 < len(best[j])):
                best[j] = best[i] + [piece]
    if best[-1]:
        return "".join(best[-1])
    return _translit_letters(word)


def normalize_tokens(text: str) -> list[str]:
    """Приводит фразу к словам для сравнения: регистр, ё/э, латиница, цифры, двойные буквы."""
    text = _NUM_SUFFIX_RE.sub(r"\1", text.lower().replace("ё", "е"))
    tokens = []
    for token in _TOKEN_RE.findall(text):
        ordinal = _ORDINAL_TOKEN_RE.fullmatch(token)
        if ordinal:
            token = _ORDINAL_DIGITS.get(ordinal.group(1), ordinal.group(1))
        elif token.isascii():
            token = _latin_to_cyrillic(token)
        elif re.search(r"[a-z]", token):
            token = _translit_letters(token)
        token = token.replace("э", "е")
        for stem, word in _ORDINAL_STEMS:
            if token.startswith(stem):
                token = word
                break
        tokens.append(_REPEAT_RE.sub(r"\1", token))
    return tokens


def sound_key(word: str) -> str:
    """Грубый звуковой ключ нормализованного слова: «тарнада» и «торнадо», «пласт» и «бласт» совпадают."""
    return _REPEAT_RE.sub(r"\1", word.translate(_SOUNDS))
