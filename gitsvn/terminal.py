"""Terminal colors, interactive pickers, and the read-only diff viewer."""

from contextlib import contextmanager
import os
import shutil
import sys
import unicodedata

from .models import GitSvnError


@contextmanager
def ansi_output():
    if not sys.stdout.isatty():
        yield False
        return
    if os.name != "nt":
        yield True
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_mode = kernel32.GetConsoleMode
    get_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_mode.restype = wintypes.BOOL
    set_mode = kernel32.SetConsoleMode
    set_mode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    set_mode.restype = wintypes.BOOL
    try:
        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
    except (OSError, ValueError):
        yield False
        return
    mode = wintypes.DWORD()
    # Enable ANSI output in cmd and restore its original setting afterward.
    if not get_mode(handle, ctypes.byref(mode)) or not set_mode(handle, mode.value | 0x0004):
        yield False
        return
    try:
        yield True
    finally:
        set_mode(handle, mode.value)


@contextmanager
def picker_input():
    if os.name != "nt" or not sys.stdin.isatty():
        yield None
        return
    import msvcrt

    with ansi_output() as enabled:
        yield msvcrt.getwch if enabled else None


def pick_item(labels, title, item, current=None, current_label="current"):
    labels = [terminal_text(" ".join(label.split())) for label in labels]
    with picker_input() as read_key:
        if read_key is None:
            print(title + (f" (* = {current_label}):" if current is not None else ":"))
            for index, label in enumerate(labels):
                print(f"{index + 1:3}. {'*' if index == current else ' '} {label}")
            try:
                choice = input(f"{item.capitalize()} number (q to cancel): ").strip()
            except EOFError:
                return None
            if choice.lower() in ("", "q", "quit"):
                return None
            if not choice.isdigit() or not 1 <= int(choice) <= len(labels):
                raise GitSvnError(f"Choose a {item} number shown in the list.")
            return int(choice) - 1

        selected = current if current is not None else 0
        size = shutil.get_terminal_size()
        rows = min(len(labels), max(1, size.lines - 3))
        width = max(1, size.columns - 1)
        heading = terminal_text(f"{title} ({len(labels)} choices; Up/Down, Enter; Esc/q cancels)")
        print(terminal_slice(heading, 0, width))
        try:
            sys.stdout.write("\x1b[?25l")
            while True:
                start = max(0, min(selected - rows // 2, len(labels) - rows))
                for index in range(start, start + rows):
                    line = "> " if index == selected else "  "
                    if index == current:
                        line += f"({current_label}) "
                    line += labels[index]
                    if index == selected:
                        line = "\x1b[7m" + terminal_slice(line, 0, width) + "\x1b[0m"
                    else:
                        line = terminal_slice(line, 0, width)
                    sys.stdout.write("\r\x1b[2K" + line + "\n")
                sys.stdout.flush()
                key = read_key()
                if key in ("\r", "\n"):
                    return selected
                if key in ("\x1b", "q", "Q"):
                    return None
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\x00", "\xe0"):
                    key = read_key()
                    if key == "H":
                        selected = (selected - 1) % len(labels)
                    elif key == "P":
                        selected = (selected + 1) % len(labels)
                    elif key == "G":
                        selected = 0
                    elif key == "O":
                        selected = len(labels) - 1
                sys.stdout.write(f"\x1b[{rows}A")
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h")
            sys.stdout.flush()


def pick_branch(names, current):
    selected = pick_item(names, "Switch branch", "branch", names.index(current))
    return names[selected] if selected is not None else None


def terminal_text(value):
    # Patch content must not be able to issue terminal control sequences.
    return "".join(f"\\x{ord(character):02x}" if ord(character) < 32 or
                   127 <= ord(character) < 160 else character
                   for character in value.expandtabs(4))


def terminal_width(value):
    return sum(0 if unicodedata.category(character) in ("Mn", "Me", "Cf") else
               2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
               for character in value)


def terminal_slice(value, left, width):
    result = []
    column = 0
    for character in value:
        cells = terminal_width(character)
        if not cells:
            if result:
                result.append(character)
            continue
        if column >= left + width:
            break
        end = column + cells
        if end > left:
            if column >= left and end <= left + width:
                result.append(character)
            else:
                # Never draw half of a wide character at a viewport edge.
                result.append(" " * (min(end, left + width) - max(column, left)))
        column = end
    return "".join(result)


def view_diff(title, lines):
    title = terminal_text(title)
    lines = [terminal_text(line) for line in lines]
    with picker_input() as read_key:
        if read_key is None:
            print(title)
            for line in lines:
                print(line)
            return

        top = left = 0
        colors = [32 if line.startswith("+") else 31 if line.startswith("-") else
                  36 if line.startswith(("@@", "##")) else 0 for line in lines]
        longest = max(map(terminal_width, lines), default=0)
        controls = "Read-only: Up/Down, PgUp/PgDn, Home/End; Left/Right; Esc/q back"
        try:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            while True:
                size = shutil.get_terminal_size()
                rows = max(1, size.lines - 3)
                width = max(1, size.columns - 1)
                top = min(top, max(0, len(lines) - rows))
                max_left = max(0, longest - width)
                left = min(left, max_left)
                sys.stdout.write("\x1b[H\x1b[2K" + terminal_slice(title, 0, width) + "\r\n")
                sys.stdout.write("\x1b[2K" + terminal_slice(controls, 0, width) + "\r\n")
                for index in range(top, top + rows):
                    line = terminal_slice(lines[index], left, width) if index < len(lines) else ""
                    color = colors[index] if index < len(lines) else 0
                    sys.stdout.write(f"\x1b[2K\x1b[{color}m{line}\x1b[0m\r\n")
                footer = f"Lines {top + 1}-{min(top + rows, len(lines))}/{len(lines)}  Column {left + 1}"
                sys.stdout.write("\x1b[2K" + footer[:width])
                sys.stdout.flush()
                key = read_key()
                if key in ("\x1b", "q", "Q"):
                    return
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\x00", "\xe0"):
                    key = read_key()
                    if key == "H":
                        top = max(0, top - 1)
                    elif key == "P":
                        top = min(max(0, len(lines) - rows), top + 1)
                    elif key == "I":
                        top = max(0, top - rows)
                    elif key == "Q":
                        top = min(max(0, len(lines) - rows), top + rows)
                    elif key == "G":
                        top = left = 0
                    elif key == "O":
                        top = max(0, len(lines) - rows)
                    elif key == "K":
                        left = max(0, left - 8)
                    elif key == "M":
                        left = min(max_left, left + 8)
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
