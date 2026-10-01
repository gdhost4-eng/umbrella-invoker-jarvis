"""Согласование промежуточных гипотез речи с уже выполненными командами."""

from dataclasses import dataclass, field

from .commands import Command
from .nlu import ParsedCommand


def too_many_commands(commands: list[ParsedCommand], duration_s: float) -> bool:
    return len(commands) > max(2, int(duration_s * 3) + 1)


@dataclass
class Finished:
    pending: list[ParsedCommand]  # ещё не выполнены — выполнить (сначала опоздавшие)
    skipped: list[ParsedCommand] = field(default_factory=list)  # заменили выполненную команду — не выполнять
    late: list[ParsedCommand] = field(default_factory=list)  # всплыли перед выполненными, но ничего не заменили


def _align(done: list[Command], commands: list[Command]) -> tuple[int, int, list[int]]:
    """Находит выполненные команды в новой гипотезе (наибольшая общая подпоследовательность).

    Возвращает: сколько выполненных нашлось; с какого места гипотезы начинаются новые команды;
    номера команд гипотезы до этого места, которые не выполнялись.
    """
    rows, cols = len(done), len(commands)
    lcs = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows):
        for j in range(cols):
            lcs[i + 1][j + 1] = lcs[i][j] + 1 if done[i] == commands[j] else max(lcs[i][j + 1], lcs[i + 1][j])
    found = lcs[rows][cols]
    # Самое короткое начало гипотезы, в котором уже нашлись все совпадения: дальше — новое.
    end = next(j for j in range(cols + 1) if lcs[rows][j] == found)
    matched: set[int] = set()
    i, j = rows, end
    while i and j:
        if done[i - 1] == commands[j - 1] and lcs[i][j] == lcs[i - 1][j - 1] + 1:
            matched.add(j - 1)
            i, j = i - 1, j - 1
        elif lcs[i - 1][j] >= lcs[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return found, end, [k for k in range(end) if k not in matched]


class StreamingCommands:
    """Сразу выпускает законченные команды и помнит их, чтобы не повторить.

    Whisper по ходу фразы может переписать уже выполненное начало: потерять команду («бласта тос»),
    заменить слово. Выполненные команды ищутся в новой гипотезе как подпоследовательность,
    и выполняется то, что идёт после последней найденной.

    Команда, которую надёжная гипотеза (на паузе или итоговая) дописала перед выполненными,
    выполняется с опозданием, если все выполненные нашлись: промежуточная гипотеза её просто
    не расслышала («блинк, а тос, катаклизм» → «блинк, а тос, хекс, катаклизм»). Если же
    выполненная команда пропала, новая могла её заменить — такая пропускается, иначе нажмутся обе.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.utterance_id: int | None = None
        self.emitted: list[Command] = []
        self.late: list[ParsedCommand] = []  # опоздавшие команды из последнего partial()
        self._diverged: list[Command] | None = None  # гипотеза, потерявшая выполненную команду

    def _select(self, utterance_id: int) -> None:
        if self.utterance_id != utterance_id:
            self.reset()
            self.utterance_id = utterance_id

    def partial(
        self, utterance_id: int, commands: list[ParsedCommand], closing: bool = False, open_end: bool = True
    ) -> list[ParsedCommand]:
        """closing — речь только что стихла; open_end — гипотеза кончается командой, слово могут ещё договаривать.

        Последняя команда выполняется, когда речь стихла или после неё уже звучит следующее слово
        («блинк, катакл…» — Blink можно жать, не дожидаясь, пока распознается «катаклизм»),
        если только её нельзя дополнить: «алакрити» → «алакрити на союзника».
        """
        self._select(utterance_id)
        self.late = []
        finished = (closing or not open_end) and commands and not commands[-1].extendable
        complete = commands if finished else commands[:-1]
        hypothesis = [parsed.command for parsed in complete]
        found, end, unmatched = _align(self.emitted, hypothesis)
        lost = len(self.emitted) - found
        # Недоговорённая гипотеза может потерять одну выполненную команду, если остальные на месте;
        # если расходится сильнее, ждём следующую гипотезу или конец фразы.
        if lost > 1 or (lost == 1 and found < 2):
            # Whisper переписал начало («блинк» → «балинг»): если следующая гипотеза повторила
            # ту же версию, продолжаем за найденной командой, а не ждём конца фразы.
            diverged, self._diverged = self._diverged, hypothesis
            if lost > 1 or not found or diverged is None or hypothesis[: len(diverged)] != diverged:
                return []
        self._diverged = None
        pending = complete[end:]
        if closing and unmatched and not lost:
            # Гипотеза на паузе дальше не перепишется: пропущенную раньше команду — сейчас.
            self.emitted = hypothesis
            self.late = [complete[k] for k in unmatched]
            return self.late + pending
        self.emitted.extend(parsed.command for parsed in pending)
        return pending

    def finish(self, utterance_id: int, commands: list[ParsedCommand]) -> Finished:
        self._select(utterance_id)
        found, end, unmatched = _align(self.emitted, [parsed.command for parsed in commands])
        lost = len(self.emitted) - found
        self.reset()
        before = [commands[k] for k in unmatched]
        if lost:
            return Finished(commands[end:], skipped=before)
        return Finished(before + commands[end:], late=before)
