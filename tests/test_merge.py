"""Byte-preserving text conflict choices and refusal of ambiguous markers."""

import unittest

from gitsvn.conflicts import conflicted_property_names, property_values
from gitsvn.merge import merge_text
from gitsvn.models import GitSvnError


class MergeTextTests(unittest.TestCase):
    def block(self, mine, theirs, *, base=b"old\n", ending=b"\n"):
        contents = b"<<<<<<< .mine" + ending + mine
        if base is not None:
            contents += b"||||||| .r1" + ending + base
        return contents + b"=======" + ending + theirs + b">>>>>>> .r2" + ending

    def test_each_choice_keeps_surrounding_changes_and_omits_old_base(self):
        before = b"manual header\nlocal nonconflicting edit\n"
        after = b"incoming nonconflicting edit\nmanual footer\n"
        contents = before + self.block(b"mine\n", b"theirs\n") + after
        choices = {"mine-conflict": b"mine\n", "theirs-conflict": b"theirs\n",
                   "mine-first": b"mine\ntheirs\n", "theirs-first": b"theirs\nmine\n"}
        for method, replacement in choices.items():
            with self.subTest(method=method):
                self.assertEqual(merge_text(contents, method), before + replacement + after)

    def test_two_way_markers_without_base_are_supported(self):
        contents = self.block(b"mine\n", b"theirs\n", base=None)
        self.assertEqual(merge_text(contents, "mine-first"), b"mine\ntheirs\n")
        self.assertEqual(merge_text(contents, "theirs-conflict"), b"theirs\n")

    def test_unlabelled_markers_are_supported(self):
        contents = b"<<<<<<<\nmine\n=======\ntheirs\n>>>>>>>\n"
        self.assertEqual(merge_text(contents, "theirs-first"), b"theirs\nmine\n")

    def test_crlf_markers_and_selected_bytes_keep_crlf(self):
        contents = (b"manual header\r\n" +
                    self.block(b"mine\r\n", b"theirs\r\n", base=b"old\r\n", ending=b"\r\n") +
                    b"manual footer\r\n")
        for method, middle in (("mine-conflict", b"mine\r\n"),
                               ("theirs-conflict", b"theirs\r\n"),
                               ("mine-first", b"mine\r\ntheirs\r\n"),
                               ("theirs-first", b"theirs\r\nmine\r\n")):
            with self.subTest(method=method):
                self.assertEqual(merge_text(contents, method),
                                 b"manual header\r\n" + middle + b"manual footer\r\n")

    def test_bom_before_unchanged_context_is_preserved(self):
        contents = b"\xef\xbb\xbfheader\n" + self.block(b"mine\n", b"theirs\n")
        self.assertEqual(merge_text(contents, "mine-first"), b"\xef\xbb\xbfheader\nmine\ntheirs\n")

    def test_bom_in_both_conflicting_first_lines_is_kept_once_in_both_orders(self):
        bom = b"\xef\xbb\xbf"
        contents = self.block(bom + b"mine\n", bom + b"theirs\n", base=bom + b"old\n")
        choices = {"mine-conflict": b"mine\n", "theirs-conflict": b"theirs\n",
                   "mine-first": b"mine\ntheirs\n", "theirs-first": b"theirs\nmine\n"}
        for method, body in choices.items():
            with self.subTest(method=method):
                self.assertEqual(merge_text(contents, method), bom + body)

    def test_empty_first_side_does_not_strip_the_other_sides_only_bom(self):
        bom = b"\xef\xbb\xbf"
        for method, mine, theirs in (("mine-first", b"", bom + b"theirs\n"),
                                    ("theirs-first", bom + b"mine\n", b"")):
            with self.subTest(method=method):
                self.assertEqual(merge_text(self.block(mine, theirs), method), mine + theirs)

    def test_embedded_bom_bytes_in_later_blocks_are_not_treated_as_file_headers(self):
        bom = b"\xef\xbb\xbf"
        contents = b"unchanged header\n" + self.block(bom + b"mine\n", bom + b"theirs\n")
        self.assertEqual(merge_text(contents, "mine-first"),
                         b"unchanged header\n" + bom + b"mine\n" + bom + b"theirs\n")

    def test_multiple_blocks_keep_intervening_manual_edits_and_unterminated_context(self):
        contents = (b"manual before\n" + self.block(b"mine one\n", b"theirs one\n") +
                    b"manual between\r\n" + self.block(b"mine two\n", b"theirs two\n", base=None) +
                    b"manual after without final newline")
        self.assertEqual(merge_text(contents, "theirs-first"),
                         b"manual before\ntheirs one\nmine one\nmanual between\r\n"
                         b"theirs two\nmine two\nmanual after without final newline")

    def test_empty_selected_side_deletes_only_its_conflict_block(self):
        contents = b"header\n" + self.block(b"", b"incoming addition\n") + b"footer\n"
        self.assertEqual(merge_text(contents, "mine-conflict"), b"header\nfooter\n")

    def test_nested_repeated_or_out_of_order_markers_are_refused(self):
        malformed = (
            b"||||||| .r1\nold\n",
            b">>>>>>> .r2\n",
            b"<<<<<<< .mine\nmine\n<<<<<<< nested\n=======\ntheirs\n>>>>>>> .r2\n",
            b"<<<<<<< .mine\nmine\n>>>>>>> .r2\n",
            b"<<<<<<< .mine\nmine\n=======\ntheirs\n||||||| .r1\n>>>>>>> .r2\n",
            b"<<<<<<< .mine\nmine\n=======\ntheirs\n=======\n>>>>>>> .r2\n",
            b"<<<<<<< .mine\nmine\n||||||| .r1\nold\n||||||| repeated\n=======\ntheirs\n>>>>>>> .r2\n",
        )
        for contents in malformed:
            with self.subTest(contents=contents):
                with self.assertRaises(GitSvnError):
                    merge_text(contents, "mine-first")

    def test_unterminated_conflict_is_refused_instead_of_returning_a_partial_result(self):
        for contents in (b"<<<<<<< .mine\nmine\n",
                         b"<<<<<<< .mine\nmine\n||||||| .r1\nold\n",
                         b"<<<<<<< .mine\nmine\n=======\ntheirs\n"):
            with self.subTest(contents=contents):
                with self.assertRaises(GitSvnError):
                    merge_text(contents, "mine-conflict")

    def test_svn_markers_glued_to_unterminated_side_lines_are_refused_safely(self):
        cases = (
            b"head\n<<<<<<< .mine\nmine||||||| .r1\nbase=======\ntheirs>>>>>>> .r2\n",
            b"head\n<<<<<<< .mine\nmine||||||| .r1\nbase\n=======\ntheirs\n>>>>>>> .r2\n",
            b"head\n<<<<<<< .mine\nmine\n||||||| .r1\nbase\n=======\ntheirs>>>>>>> .r2\n",
            b"head\n<<<<<<< .mine\nmine\n||||||| .r1\nbase=======\ntheirs\n>>>>>>> .r2\n",
        )
        for contents in cases:
            with self.subTest(contents=contents):
                with self.assertRaises(GitSvnError):
                    merge_text(contents, "mine-conflict")

    def test_no_remaining_conflict_requires_the_working_result_choice(self):
        with self.assertRaisesRegex(GitSvnError, "manually edited"):
            merge_text(b"already repaired\n", "mine-conflict")

    def test_binary_content_cannot_be_merged_as_text(self):
        with self.assertRaisesRegex(GitSvnError, "whole-file"):
            merge_text(self.block(b"mine\x00\n", b"theirs\n"), "mine-first")


class PropertyConflictParserTests(unittest.TestCase):
    def reject(self, name, *, operation=b"change", value=b"local", closed=True):
        contents = (b"Trying to " + operation + b" property '" + name + b"'\n"
                    b"but the property has been locally changed.\n"
                    b"<<<<<<< (local property value)\r\n" + value + b"\n"
                    b"||||||| (incoming 'changed from' value)\r\n"
                    b"base=======\r\nincoming")
        if closed:
            contents += b">>>>>>> (incoming 'changed to' value)\r\n"
        return contents

    def test_all_three_real_header_forms_identify_only_conflicted_names(self):
        reject = (self.reject(b"custom:edit") +
                  self.reject(b"custom:delete", operation=b"delete") +
                  self.reject(b"custom:add", operation=b"add new"))
        self.assertEqual(conflicted_property_names(reject, 3),
                         ["custom:edit", "custom:delete", "custom:add"])

    def test_header_like_property_payload_is_ignored(self):
        reject = self.reject(b"custom:flag", value=b"Trying to change property 'custom:unrelated'")
        self.assertEqual(conflicted_property_names(reject, 1), ["custom:flag"])

    def test_marker_like_text_with_trailing_content_does_not_end_property_payload(self):
        value = (b"not a marker: >>>>>>> (incoming 'changed to' value) trailing content\n"
                 b"Trying to change property 'custom:unrelated'")
        reject = self.reject(b"custom:flag", value=value)
        self.assertEqual(conflicted_property_names(reject, 1), ["custom:flag"])

    def test_ambiguous_embedded_closing_marker_cannot_authorize_an_unrelated_property(self):
        value = (b">>>>>>> (incoming 'changed to' value)\n"
                 b"Trying to change property 'custom:unrelated'")
        self.assertEqual(conflicted_property_names(self.reject(b"custom:flag", value=value), 2), [])

    def test_semantic_property_count_requires_an_exact_header_count(self):
        reject = self.reject(b"custom:flag")
        self.assertEqual(conflicted_property_names(reject, 1), ["custom:flag"])
        self.assertEqual(conflicted_property_names(reject, 2), [])
        reject += self.reject(b"custom:other")
        self.assertEqual(conflicted_property_names(reject, 2), ["custom:flag", "custom:other"])
        self.assertEqual(conflicted_property_names(reject, 4), [])

    def test_localized_header_is_refused_instead_of_guessing_property_names(self):
        reject = self.reject(b"custom:flag").replace(b"Trying to change property", b"Localized header")
        self.assertEqual(conflicted_property_names(reject, 1), [])

    def test_unclosed_property_payload_is_refused(self):
        self.assertEqual(conflicted_property_names(self.reject(b"custom:flag", closed=False), 1), [])

    def test_duplicate_headers_and_incompatible_xml_counts_are_refused(self):
        reject = self.reject(b"custom:flag")
        self.assertEqual(conflicted_property_names(reject + reject, 2), [])
        reject += self.reject(b"custom:other")
        for expected in (0, 1, 3):
            with self.subTest(expected=expected):
                self.assertEqual(conflicted_property_names(reject, expected), [])

    def test_property_xml_preserves_binary_base64_and_unicode_values(self):
        xml = (b'<properties><target path="file"><property name="custom:binary" encoding="base64">'
               b'AP8NCg==</property><property name="custom:unicode">caf\xc3\xa9</property>'
               b'<property name="custom:empty" /></target></properties>')
        self.assertEqual(property_values(xml), {"custom:binary": b"\x00\xff\r\n",
                                              "custom:unicode": b"caf\xc3\xa9", "custom:empty": b""})


if __name__ == "__main__":
    unittest.main()
