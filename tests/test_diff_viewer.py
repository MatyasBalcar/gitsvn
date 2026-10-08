"""Read-only diff viewer navigation without changing the console or workspace."""

import io
import os
import re
import unittest
from unittest.mock import Mock, patch

from main import terminal_slice, terminal_text, terminal_width, view_diff


class DiffViewerTests(unittest.TestCase):
    def run_viewer(self, keys, lines, *, title="Saved diff", size=(80, 7), output=None):
        output = output if output is not None else io.StringIO()
        with patch("main.picker_input") as input_mode, \
                patch("main.shutil.get_terminal_size", return_value=os.terminal_size(size)), \
                patch("main.sys.stdout", output):
            input_mode.return_value.__enter__.return_value = Mock(side_effect=keys)
            view_diff(title, lines)
        return output.getvalue()

    def test_vertical_navigation_preserves_hunks_and_stays_in_bounds(self):
        keys = ["\xe0", "P", "\xe0", "Q", "\xe0", "H", "\xe0", "I",
                "\xe0", "O", "\xe0", "P", "\xe0", "G", "\xe0", "H", "q"]
        output = self.run_viewer(keys, [f" context {number}" for number in range(10)])
        frames = output.split("\x1b[H")[1:]
        expected = [(1, 4), (2, 5), (6, 9), (5, 8), (1, 4), (7, 10),
                    (7, 10), (1, 4), (1, 4)]
        self.assertEqual(len(frames), len(expected))
        for frame, (first, last) in zip(frames, expected):
            self.assertIn(f"Lines {first}-{last}/10", frame)
            self.assertIn(f" context {first - 1}", frame)
            self.assertIn(f" context {last - 1}", frame)

    def test_horizontal_navigation_reveals_long_line_and_home_resets(self):
        line = "+0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        keys = ["\x00", "M", "\x00", "M", "\x00", "K", "\x00", "G", "q"]
        output = self.run_viewer(keys, [line], size=(32, 7))
        frames = output.split("\x1b[H")[1:]
        for frame, left in zip(frames, [0, 8, 16, 8, 0]):
            self.assertIn(line[left:left + 31], frame)
            self.assertIn(f"Column {left + 1}", frame)
        # Scrolling past the '+' still colors the line as an addition.
        self.assertIn("\x1b[32m" + line[8:39], frames[1])

    def test_added_removed_and_hunk_lines_have_distinct_colors(self):
        output = self.run_viewer(["q"], ["+added", "-removed", "@@ -1 +1 @@", " context"])
        self.assertIn("\x1b[32m+added\x1b[0m", output)
        self.assertIn("\x1b[31m-removed\x1b[0m", output)
        self.assertIn("\x1b[36m@@ -1 +1 @@\x1b[0m", output)
        self.assertIn(" context", output)

    def test_escape_and_q_restore_cursor_and_leave_alternate_screen(self):
        for key in ("\x1b", "q", "Q"):
            with self.subTest(key=repr(key)):
                output = self.run_viewer([key], [" context"])
                self.assertTrue(output.startswith("\x1b[?1049h\x1b[?25l"))
                self.assertTrue(output.endswith("\x1b[0m\x1b[?25h\x1b[?1049l"))

    def test_interrupt_restores_terminal_before_propagating(self):
        for key in ("\x03", KeyboardInterrupt):
            with self.subTest(key=repr(key)):
                output = io.StringIO()
                with self.assertRaises(KeyboardInterrupt):
                    self.run_viewer([key], [" context"], output=output)
                self.assertTrue(output.getvalue().endswith("\x1b[0m\x1b[?25h\x1b[?1049l"))

    def test_plain_output_keeps_all_lines_and_sanitizes_terminal_controls(self):
        output = io.StringIO()
        with patch("main.picker_input") as input_mode, patch("main.sys.stdout", output):
            input_mode.return_value.__enter__.return_value = None
            view_diff("Title\x1b]8;;url\x07", ["+added\ttext", "-removed\x1b[2J", " context"])
        self.assertEqual(output.getvalue(),
                         "Title\\x1b]8;;url\\x07\n+added  text\n-removed\\x1b[2J\n context\n")
        self.assertNotIn("\x1b", output.getvalue())

    def test_terminal_text_expands_tabs_and_preserves_unicode(self):
        self.assertEqual(terminal_text("a\tb\r\n\x00\x7f\x9b čé"),
                         "a   b\\x0d\\x0a\\x00\\x7f\\x9b čé")

    def test_cell_slicing_keeps_combining_marks_and_wide_edges_inside_viewport(self):
        self.assertEqual(terminal_width("A界e\u0301"), 4)
        self.assertEqual(terminal_slice("A界B", 0, 2), "A ")
        self.assertEqual(terminal_slice("A界B", 1, 2), "界")
        self.assertEqual(terminal_slice("A界B", 2, 2), " B")
        self.assertEqual(terminal_slice("e\u0301xyz", 0, 1), "e\u0301")
        self.assertEqual(terminal_slice("e\u0301xyz", 1, 2), "xy")

    def test_wide_and_combining_text_scrolls_without_wrapping_terminal_rows(self):
        line = "+012345界e\u0301XYZabcdefghijklmnopqrstuvwxyz"
        output = self.run_viewer(["\xe0", "M", "\xe0", "M", "q"], [line],
                                 title="界" * 20, size=(18, 7))
        frames = output.split("\x1b[H")[1:]
        for frame, expected in zip(frames, ["+012345界e\u0301XYZabcd",
                                           " e\u0301XYZabcdefghijkl", "defghijklmnopqrst"]):
            self.assertIn(expected, frame)
            plain = re.sub(r"\x1b\[[?0-9;]*[A-Za-z]", "", frame)
            self.assertTrue(all(terminal_width(row) <= 17 for row in plain.splitlines()))


if __name__ == "__main__":
    unittest.main()
