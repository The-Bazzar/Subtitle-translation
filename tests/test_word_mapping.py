import unittest

import translate_srt as t
from subtitle_translation.aligned_transcript import map_display_words


class WordMappingTests(unittest.TestCase):
    def test_repeated_words_project_by_position_without_text_search(self):
        segment = t.TranscriptSegment(1, 0, 3, "go go now", words=[
            t.TranscriptWord("go", 0, 1), t.TranscriptWord("go", 1, 2), t.TranscriptWord("now", 2, 3)])
        boundaries = t.source_word_boundaries(segment)
        events = t.source_events_from_boundaries(segment, boundaries, ["b0", "b1", "b3"], "plan")
        self.assertEqual([e.en for e in events], ["go", "go now"])
        self.assertEqual((events[1].start, events[1].end), (1, 3))

    def test_invalid_word_axes_do_not_enable_default_timestamp_projection(self):
        for words in ([t.TranscriptWord("hello", 0, None)], [t.TranscriptWord("wrong", 0, 1)],
                      [t.TranscriptWord("hello", 0, float("nan"))]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                t.source_word_boundaries(t.TranscriptSegment(1, 0, 1, "hello", words=words))
    def test_contraction_mapping_does_not_invent_internal_word_times(self):
        text = "I'm ready."
        words = [{"word": "I", "start": 0, "end": 0.3}, {"word": "'m", "start": 0.3, "end": 0.6},
                 {"word": "ready", "start": 1, "end": 2}]
        mapped = map_display_words(text, words, t.text_tokens)
        self.assertEqual(mapped[0]["word"], "I'm")
        self.assertEqual((mapped[0]["start"], mapped[0]["end"]), (0, 0.6))
        self.assertEqual(mapped[0]["source_range"], [0, 3])


    def test_many_source_words_can_share_one_indivisible_mfa_span(self):
        mapped = map_display_words("a part now", [{"word": "apart", "start": 0, "end": 1},
                                                 {"word": "now", "start": 2, "end": 3}], t.text_tokens)
        self.assertEqual(mapped[0]["word"], "a part")
        segment = t.TranscriptSegment(1, 0, 3, "a part now", words=[t.TranscriptWord.from_json(word) for word in mapped])
        boundaries = t.source_word_boundaries(segment)
        self.assertEqual([boundary["offset"] for boundary in boundaries], [0, 7, 10])
