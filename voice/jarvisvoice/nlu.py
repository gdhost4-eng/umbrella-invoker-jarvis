"""Нормализация распознанного текста и разбор фразы в команды."""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Generic, TypeVar

from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

from .commands import ComboCmd, Command, ItemCmd, PressCmd, SpellCmd, StanceCmd, SystemCmd
from .config import Config
from .text import normalize_tokens as normalize_tokens
from .text import sound_key

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
T = TypeVar("T")

# Разбор ищет разбивку фразы, которая лучше всего объясняет её буквы командами.
_SPAN_COST = 0.5  # одна длинная команда лучше двух коротких на тех же буквах: «колд снэп» ≠ «колд» + «снэп»
_SHIFT_COST = 0.75  # граница команды внутри слова Whisper: «бласта тос» → «бласт» + «атос»
_SOUND_SCORE = 95.0  # совпадение только по звучанию чуть хуже совпадения по буквам
_NEAR_SOUND_SCORE = 85.0  # звучание отличается одним звуком: «балинг» → «блинк»
_NEAR_SOUND_MIN_RATIO = 70.0  # и буквы всё же похожи: «сколько» звучит почти как «салька», но это не Salve
_SOUND_VOWELS = frozenset("аиуэ")  # гласные звукового ключа (о→а, е→и, ю→у, …)
_SOUND_MIN_LENGTH = 5  # «весь» и «фейз» звучат одинаково — у коротких слов звучание не различает
_JOINED_SOUND_MIN_LENGTH = 4  # внутри склеенного слова рядом с командой: «блин катас» → «блинк» + «атос»
_JOINED_MIN_LENGTH = 4  # кусок слова короче сравнивается только точно
_PREFIX_MAX = 3  # обрывок в начале слова перед командой: «и тербласт» → «бласт»
_PREFIXED_MIN = 5
_CACHE_LIMIT = 16384

# Позиция в склеенной фразе и что было перед ней.
_NORMAL, _SHARED, _PREFIXED = 0, 1, 2  # обычно; общий звук с предыдущей командой; обрывок начала слова
State = tuple[int, int]


def _vowel_apart(heard: str, alias: str) -> bool:
    """Звуковые ключи отличаются одной вставленной или пропущенной гласной."""
    ops = Levenshtein.editops(heard, alias)
    if len(ops) != 1 or ops[0].tag == "replace":
        return False
    op = ops[0]
    letter = heard[op.src_pos] if op.tag == "delete" else alias[op.dest_pos]
    return letter in _SOUND_VOWELS


def _max_edits(length: int) -> int:
    """Сколько опечаток прощать алиасу такой длины: «пласт» → «бласт», но не «о тут» → «бот мут»."""
    return 1 if length <= 7 else 2 if length <= 11 else 3


@dataclass(frozen=True)
class _Alias(Generic[T]):
    text: str
    compact: str
    target: T
    fuzzy: bool  # False — только точное совпадение: короткие алиасы и команды самому боту
    extendable: bool = False  # есть длиннее у другой команды: «алакрити» → «алакрити на союзника»


@dataclass(frozen=True)
class _Hit(Generic[T]):
    alias: _Alias[T]
    score: float


class _Matcher(Generic[T]):
    """Алиасы одного вида: точное совпадение, сходство по буквам и по звучанию."""

    def __init__(
        self,
        entries: list[tuple[str, T]],
        threshold: float,
        exact_only: Callable[[T], bool] = lambda target: False,
    ) -> None:
        self.threshold = threshold
        self.conflicts: list[str] = []
        aliases: dict[str, _Alias[T]] = {}
        for text, target in entries:
            compact = "".join(normalize_tokens(text))
            if not compact:
                continue
            existing = aliases.get(compact)
            if existing is not None:
                if existing.target != target:
                    self.conflicts.append(f"«{text}» совпадает с «{existing.text}» у другой команды")
                continue
            # Короткие слова («тп», «бкб», «эмп») — только точное совпадение.
            aliases[compact] = _Alias(text, compact, target, len(compact) > 3 and not exact_only(target))
        self._exact = {
            compact: replace(alias, extendable=self._extendable(alias, aliases)) for compact, alias in aliases.items()
        }
        self._fuzzy = [alias for alias in self._exact.values() if alias.fuzzy]
        self._sounds: dict[str, _Alias[T] | None] = {}  # None — звучит как разные команды
        for alias in self._fuzzy:
            key = sound_key(alias.compact)
            if key not in self._sounds:
                self._sounds[key] = alias
            elif (other := self._sounds[key]) is not None and other.target != alias.target:
                self._sounds[key] = None
        self.max_length = max(map(len, self._exact), default=0)
        self._cache: dict[tuple[str, bool], _Hit[T] | None] = {}
        # Кандидаты по длине: при ≤3 опечатках длины отличаются не больше чем на 3.
        self._by_length: dict[int, tuple[list[str], list[_Alias[T]]]] = {}
        self._by_sound_length: dict[int, tuple[list[str], list[_Alias[T]]]] = {}

    def _candidates(self, length: int) -> tuple[list[str], list[_Alias[T]]]:
        if length not in self._by_length:
            aliases = [alias for alias in self._fuzzy if abs(len(alias.compact) - length) <= 3]
            self._by_length[length] = ([alias.compact for alias in aliases], aliases)
        return self._by_length[length]

    def _sound_candidates(self, length: int) -> tuple[list[str], list[_Alias[T]]]:
        if length not in self._by_sound_length:
            long_aliases = [alias for alias in self._fuzzy if len(alias.compact) >= _SOUND_MIN_LENGTH]
            pairs = [(sound_key(alias.compact), alias) for alias in long_aliases]
            pairs = [(key, alias) for key, alias in pairs if abs(len(key) - length) <= 1]
            self._by_sound_length[length] = ([key for key, _ in pairs], [alias for _, alias in pairs])
        return self._by_sound_length[length]

    @staticmethod
    def _extendable(alias: _Alias[T], aliases: dict[str, _Alias[T]]) -> bool:
        return any(
            other.target != alias.target and len(key) > len(alias.compact) and key.startswith(alias.compact)
            for key, other in aliases.items()
        )

    def exact(self, compact: str) -> _Hit[T] | None:
        alias = self._exact.get(compact)
        return _Hit(alias, 100.0) if alias is not None else None

    def lookup(self, compact: str, joined: bool = False) -> _Hit[T] | None:
        """joined — кусок склеенного слова рядом с другой командой: звучание сравнивается и у коротких."""
        hit = self.exact(compact)
        if hit is not None or len(compact) <= 3 or not self._fuzzy:
            return hit
        key = (compact, joined)
        if key not in self._cache:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = self._similar(compact, joined)
        return self._cache[key]

    def _similar(self, compact: str, joined: bool) -> _Hit[T] | None:
        best: _Hit[T] | None = None
        keys, aliases = self._candidates(len(compact))
        found = process.extract(compact, keys, scorer=fuzz.ratio, score_cutoff=self.threshold, limit=None)
        for key, score, index in found:
            alias = aliases[index]
            if Levenshtein.distance(compact, key) <= _max_edits(len(key)) and (best is None or score > best.score):
                best = _Hit(alias, score)
        sound = sound_key(compact)
        min_length = _JOINED_SOUND_MIN_LENGTH if joined else _SOUND_MIN_LENGTH
        sounds_like = self._sounds.get(sound) if len(compact) >= min_length else None
        if sounds_like is not None and len(sounds_like.compact) >= min_length:
            if best is None or best.score < _SOUND_SCORE:
                best = _Hit(sounds_like, _SOUND_SCORE)
        if best is None and len(compact) >= _SOUND_MIN_LENGTH:
            best = self._near_sound(compact, sound)
        return best

    def _near_sound(self, compact: str, sound: str) -> _Hit[T] | None:
        """Звучание отличается лишней или пропавшей гласной между согласными: «балинг» → «блинк».

        Другие отличия в один звук слишком часты в обычной речи: «огонь» — не «догон», «сулка» — не «салька».
        """
        best: _Alias[T] | None = None
        best_ratio = _NEAR_SOUND_MIN_RATIO
        ambiguous = False
        keys, aliases = self._sound_candidates(len(sound))
        for key, _, index in process.extract(sound, keys, scorer=Levenshtein.distance, score_cutoff=1, limit=None):
            alias = aliases[index]
            if not _vowel_apart(sound, key):
                continue
            ratio = fuzz.ratio(compact, alias.compact)
            if ratio > best_ratio:
                best, best_ratio, ambiguous = alias, ratio, False
            elif ratio == best_ratio and best is not None and alias.target != best.target:
                ambiguous = True
        return _Hit(best, _NEAR_SOUND_SCORE) if best is not None and not ambiguous else None


@dataclass(frozen=True)
class ParsedCommand:
    command: Command
    alias: str
    heard: str
    score: float
    extendable: bool = False  # фразу могут дополнить до другой команды — ждать её конца

    @property
    def label(self) -> str:
        return self.command.label or self.alias


@dataclass
class ParseResult:
    commands: list[ParsedCommand]
    waiting_for_spell: bool  # прозвучало «скастуй», спелл ждём в следующей фразе
    unmatched: list[str]
    normalized: str
    pending_until: float = field(default=0.0, repr=False, compare=False)
    ends_with_command: bool = False  # последнее слово — часть команды, а не обрывок или посторонняя речь


_COMMAND = "command"
_INVOKE_ONLY = "invoke_only"
_LEARN = "learn"
_IGNORE = "ignore"


@dataclass(frozen=True)
class _Unit:
    """Кусок фразы, опознанный как команда, префикс или слово-исключение."""

    kind: str
    start: int
    end: int
    score: float
    shifts: int  # сколько концов куска не на границе слов Whisper
    command: Any = None
    alias: str = ""
    extendable: bool = False

    @property
    def gain(self) -> float:
        return (self.end - self.start) * (self.score / 100) ** 2 - _SPAN_COST - _SHIFT_COST * self.shifts


class _Phrase:
    """Фраза без пробелов и места, где может проходить граница команды.

    Граница команды может быть и внутри слова Whisper: он склеивает быстрые команды
    («метеорбласт», «бласкл снэп» = «бласт» + «колд снэп»).
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.text = "".join(tokens)
        self.bounds = [0]
        for token in tokens:
            self.bounds.append(self.bounds[-1] + len(token))
        self.boundaries = set(self.bounds)
        self.cuts = list(range(len(self.text) + 1))

    def next_boundary(self, position: int) -> int:
        return self.bounds[bisect.bisect_right(self.bounds, position)]

    def heard(self, start: int, end: int) -> str:
        if start in self.boundaries and end in self.boundaries:
            return " ".join(self.tokens[self.bounds.index(start) : self.bounds.index(end)])
        return self.text[start:end]


class CommandParser:
    def __init__(self, config: Config) -> None:
        threshold = config.recognition.match_threshold
        self.window_s = config.timing.invoke_only_window_ms / 1000
        self._commands = _Matcher(
            [(a, vc.command) for vc in config.commands for a in vc.say],
            threshold,
            _exact_only,
        )
        self._prefixes = _Matcher(
            [(a, _INVOKE_ONLY) for a in config.invoke_only_prefixes] + [(a, _LEARN) for a in config.learn_prefixes],
            threshold,
        )
        self._learn = _Matcher([(a, vc.command) for vc in config.learn_targets for a in vc.say], threshold)
        self._ignore = _Matcher([(a, _IGNORE) for a in config.ignore_words], threshold, lambda _: True)
        self._max_length = max(self._commands.max_length, self._prefixes.max_length + self._learn.max_length) + 3
        self._pending_until = 0.0
        self.conflicts = self._commands.conflicts + self._prefixes.conflicts + self._learn.conflicts
        for word in config.ignore_words:
            hit = self._commands.exact("".join(normalize_tokens(word)))
            if hit is not None:
                self.conflicts.append(f"«{word}» из ignore совпадает с командой «{hit.alias.text}»")

    def parse(self, text: str, now: float, commit: bool = True) -> ParseResult:
        """commit=False — пробный разбор недоговорённой фразы: состояние «скастуй» не меняется."""
        tokens = normalize_tokens(text)
        phrase = _Phrase(tokens)
        inherited_until = self._pending_until if now < self._pending_until else 0.0
        invoke_only = inherited_until > 0.0
        prefix_said = False
        commands: list[ParsedCommand] = []
        unmatched: list[str] = []
        items = self._segment(phrase)
        last_command = -1
        for index, item in enumerate(items):
            if isinstance(item, str):
                unmatched.append(item)
                continue
            if item.kind == _IGNORE:
                item = self._inside_combo(phrase, item, items[index + 1 : index + 2])
                if item is None:
                    continue
            if item.kind == _INVOKE_ONLY:
                invoke_only = prefix_said = True
                continue
            command = item.command
            if item.kind == _COMMAND:
                if invoke_only and isinstance(command, SpellCmd):
                    command = replace(command, invoke_only=True)
                invoke_only = False
            heard = phrase.heard(item.start, item.end)
            commands.append(ParsedCommand(command, item.alias, heard, item.score, item.extendable))
            last_command = index

        pending_until = (now + self.window_s if prefix_said else inherited_until) if invoke_only else 0.0
        ends_with_command = bool(items) and last_command == len(items) - 1
        result = ParseResult(commands, invoke_only, unmatched, " ".join(tokens), pending_until, ends_with_command)
        if commit:
            self.commit(result)
        return result

    def commit(self, result: ParseResult) -> None:
        """Принимает состояние уже проверенной фразы без повторного нечёткого поиска."""
        self._pending_until = result.pending_until

    def reset(self) -> None:
        self._pending_until = 0.0

    # --- разбивка фразы ------------------------------------------------------------

    def _segment(self, phrase: _Phrase) -> list[_Unit | str]:
        """Лучшая разбивка фразы на команды; строка — нераспознанный кусок."""
        length = len(phrase.text)
        # состояние → (вес, предыдущее состояние, кусок фразы между ними)
        best: dict[State, tuple[float, State | None, _Unit | str | None]] = {(0, _NORMAL): (0.0, None, None)}

        def relax(state: State, gain: float, previous: State, item: _Unit | str) -> None:
            known = best.get(state)
            if known is None or gain > known[0] + 1e-9:
                best[state] = (gain, previous, item)

        text = phrase.text
        for position in phrase.cuts:
            for mode in (_NORMAL, _SHARED, _PREFIXED):
                state = (position, mode)
                if state not in best or position >= length:
                    continue
                gain = best[state][0]
                # Сдвинутая граница допустима только между двумя командами: «сто пудов» — не «стоп».
                if mode == _NORMAL and position in phrase.boundaries:
                    following = phrase.next_boundary(position)
                    relax((following, _NORMAL), gain, state, text[position:following])
                    # Обрывок перед длинной командой в том же слове: «и тербласт» → «бласт».
                    for cut in range(position + 1, min(position + _PREFIX_MAX, following - _PREFIXED_MIN) + 1):
                        relax((cut, _PREFIXED), gain, state, text[position:cut])
                for unit in self._units_from(phrase, position):
                    if mode == _SHARED and unit.end <= position + 1:
                        continue
                    if mode == _PREFIXED and not _prefixed_ok(unit):
                        continue
                    relax((unit.end, _NORMAL), gain + unit.gain, state, unit)
                    # Одинаковый звук на стыке произносится один раз: «атос санстрайк» → «а то санстрайк».
                    if unit.end not in phrase.boundaries and unit.end - 1 in phrase.boundaries:
                        if unit.start < unit.end - 1:
                            relax((unit.end - 1, _SHARED), gain + unit.gain, state, unit)
                    # …или Whisper пишет его в обоих словах: «блинк, католс» → «блинк» + «атолс».
                    end = unit.end
                    if end in phrase.boundaries and 0 < end < length - 1 and text[end] == text[end - 1]:
                        if end + 1 < phrase.next_boundary(end):
                            relax((end + 1, _NORMAL), gain + unit.gain, state, unit)

        items: list[_Unit | str] = []
        cursor: State | None = (length, _NORMAL)
        while cursor is not None:
            _, previous, item = best[cursor]
            if item is not None:
                items.append(item)
            cursor = previous
        return items[::-1]

    def _units_from(self, phrase: _Phrase, start: int) -> Iterator[_Unit]:
        cuts = phrase.cuts
        for end in cuts[bisect.bisect_right(cuts, start) :]:
            if end - start > self._max_length:
                return
            shifts = (start not in phrase.boundaries) + (end not in phrase.boundaries)
            span = phrase.text[start:end]
            hit = self._find(self._commands, span, shifts, joined=True)
            if hit is not None:
                alias = hit.alias
                yield _Unit(_COMMAND, start, end, hit.score, shifts, alias.target, alias.text, alias.extendable)
            if self._ignore.exact(span) is not None:
                yield _Unit(_IGNORE, start, end, 100.0, shifts)
            prefix = self._find(self._prefixes, span, shifts)
            if prefix is None:
                continue
            if prefix.alias.target == _INVOKE_ONLY:
                yield _Unit(_INVOKE_ONLY, start, end, prefix.score, shifts)
            else:
                yield from self._learn_units(phrase, start, end, prefix, shifts)

    def _learn_units(self, phrase: _Phrase, start: int, middle: int, prefix: _Hit, shifts: int) -> Iterator[_Unit]:
        """«качай экзорт»: цель прокачки сразу после префикса."""
        cuts = phrase.cuts
        for end in cuts[bisect.bisect_right(cuts, middle) :]:
            if end - middle > self._learn.max_length + 3:
                return
            end_shifted = end not in phrase.boundaries
            target_shifts = (middle not in phrase.boundaries) + end_shifted
            target = self._find(self._learn, phrase.text[middle:end], target_shifts)
            if target is not None:
                alias = target.alias
                score = min(prefix.score, target.score)
                yield _Unit(_LEARN, start, end, score, shifts + end_shifted, alias.target, alias.text)

    def _inside_combo(self, phrase: _Phrase, item: _Unit, following: list[_Unit | str]) -> _Unit | None:
        """Слово-исключение прямо перед командой — сама команда: в комбо Whisper пишет «блин, хекс» вместо «блинк»."""
        if not following or isinstance(following[0], str) or following[0].kind == _IGNORE:
            return None
        hit = self._commands.lookup(phrase.text[item.start : item.end])
        if hit is None:
            return None
        alias = hit.alias
        return _Unit(_COMMAND, item.start, item.end, hit.score, item.shifts, alias.target, alias.text, alias.extendable)

    @staticmethod
    def _find(matcher: _Matcher, span: str, shifts: int, joined: bool = False) -> _Hit | None:
        """Кусок слова рядом с другой командой: для команд — нечётко («балинк|атос»), для остального — точно."""
        if not shifts:
            return matcher.lookup(span)
        if len(span) < _JOINED_MIN_LENGTH:
            return None  # «торкл снэп» — не «тор» + «колд снэп»
        return matcher.lookup(span, joined=True) if joined else matcher.exact(span)


def _exact_only(command: Command) -> bool:
    """Случайный мут или выход посреди драки хуже, чем переспросить. Номер комбо тоже нужен точный:
    «комбо 13» — не «комбо 1»."""
    return isinstance(command, SystemCmd) or (isinstance(command, ComboCmd) and command.slot > 0)


def _prefixed_ok(unit: _Unit) -> bool:
    """После обрывка в том же слове — только точный длинный спелл или предмет: «тербласт», но не «культа»."""
    return (
        unit.kind == _COMMAND
        and unit.score >= 100.0
        and unit.end - unit.start >= _PREFIXED_MIN
        and isinstance(unit.command, (SpellCmd, ItemCmd))
    )


class WakeWord:
    """Слово активации («Джарвис»). Короткие варианты сравниваются точно, длинные — по сходству.

    Слово, разбитое Whisper пополам («джар вис»), тоже засчитывается, но только при точном совпадении.
    """

    def __init__(self, aliases: tuple[str, ...], threshold: float) -> None:
        self._aliases = sorted({"".join(normalize_tokens(alias)) for alias in aliases} - {""})
        self._threshold = threshold

    def split(self, text: str, anywhere: bool) -> tuple[bool, str]:
        """Возвращает (было ли обращение, текст для разбора команд).

        anywhere=False — команды только после первого обращения, всё до него не адресовано боту;
        anywhere=True — бот уже слушает, обращения просто вырезаются из фразы.
        """
        tokens = normalize_tokens(text)
        rest: list[str] = []
        found = False
        i = 0
        while i < len(tokens):
            length = self._length_at(tokens, i)
            if not length:
                rest.append(tokens[i])
                i += 1
                continue
            if not found and not anywhere:
                rest.clear()
            found = True
            i += length
        return found, " ".join(rest)

    def _length_at(self, tokens: list[str], i: int) -> int:
        if i + 1 < len(tokens) and tokens[i] + tokens[i + 1] in self._aliases:
            return 2
        token = tokens[i]
        if token in self._aliases:
            return 1
        if len(token) > 3 and process.extractOne(token, self._aliases, scorer=fuzz.ratio, score_cutoff=self._threshold):
            return 1
        return 0


def build_prompt(config: Config, limit: int = 420) -> str:
    """Подсказка для Whisper со сленгом из алиасов.

    Whisper обрезает длинную подсказку с начала, поэтому слова идут от наименее важных
    к самым важным: предметы (из конца списка к началу) → команды → комбо → сферы → префиксы → спеллы
    → слово активации.
    """
    items: list[str] = []
    presses: list[str] = []
    combos: list[str] = []
    stances: list[str] = []
    spells: list[str] = []
    for vc in config.commands:
        command = vc.command
        if isinstance(command, SpellCmd):
            spells.extend(vc.say[:2])
        elif isinstance(command, ItemCmd):
            if not command.self_cast:
                items.append(vc.say[0])
        elif isinstance(command, StanceCmd):
            stances.append(vc.say[0])
        elif isinstance(command, PressCmd) and " " not in vc.say[0]:
            presses.append(vc.say[0])
        elif isinstance(command, ComboCmd) and command.slot == 0:
            combos.append(vc.say[0])  # номера Whisper пишет и сам, подсказать нужно слова
    prefixes = [*config.invoke_only_prefixes[:2], *config.learn_prefixes[:1]]
    wake = [config.wake.say[0].capitalize()] if config.wake.enabled else []

    words: list[str] = []
    seen: set[str] = set()
    for word in [*reversed(items), *presses, *combos, *stances, *prefixes, *spells, *wake]:
        key = "".join(normalize_tokens(word))
        if key not in seen and _CYRILLIC_RE.search(word):
            seen.add(key)
            words.append(word)
    while words and len(", ".join(words)) + 1 > limit:
        words.pop(0)
    return ", ".join(words) + "." if words else ""
