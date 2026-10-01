"""Низкоуровневые WinAPI-вызовы."""

import ctypes
import sys

if sys.platform == "win32":
    from ctypes import wintypes as wt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = (("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG))

    _ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
    _PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
    _POWER_THROTTLING_EXECUTION_SPEED = 0x1
    _ERROR_INVALID_FUNCTION = 1
    _kernel32.GetCurrentProcess.restype = wt.HANDLE
    _kernel32.SetPriorityClass.argtypes = (wt.HANDLE, wt.DWORD)
    _kernel32.SetPriorityClass.restype = wt.BOOL
    _kernel32.SetProcessInformation.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
    _kernel32.SetProcessInformation.restype = wt.BOOL

    def keep_responsive() -> list[str]:
        """Пока Дота на переднем плане, бот — фоновый процесс: Windows понижает ему приоритет и частоту
        процессора (энергосбережение, EcoQoS), и распознавание в игре идёт медленнее. Возвращает, что не вышло."""
        process = _kernel32.GetCurrentProcess()
        problems = []
        if not _kernel32.SetPriorityClass(process, _ABOVE_NORMAL_PRIORITY_CLASS):
            problems.append(f"приоритет выше обычного: ошибка {ctypes.get_last_error()}")
        # ControlMask без StateMask — «не замедлять этот процесс».
        state = _POWER_THROTTLING_STATE(1, _POWER_THROTTLING_EXECUTION_SPEED, 0)
        if not _kernel32.SetProcessInformation(
            process, _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)
        ):
            error = ctypes.get_last_error()
            if error != _ERROR_INVALID_FUNCTION:  # энергосбережение процессов выключено в системе — и не нужно
                problems.append(f"отключить энергосбережение: ошибка {error}")
        return problems

else:

    def keep_responsive() -> list[str]:
        return []
