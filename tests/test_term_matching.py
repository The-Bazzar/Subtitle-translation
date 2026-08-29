import unittest

import translate_srt as t


class TermMatchingTests(unittest.TestCase):
    def test_mixed_cjk_term_with_leading_digit_matches_after_classifier(self):
        self.assertTrue(t.term_form_in_text("这是一个0刻脉冲。", "0刻脉冲"))

    def test_latin_term_still_requires_a_word_boundary(self):
        self.assertFalse(t.term_form_in_text("party venue", "Art"))

    def test_regular_plural_matches_a_confirmed_latin_lemma(self):
        self.assertTrue(t.term_form_in_text("Set the coil to four qelths.", "qelth"))


if __name__ == "__main__":
    unittest.main()
