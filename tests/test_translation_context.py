import unittest

import translate_srt as t


class TranslationContextTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")
        self.segments = [
            t.TranscriptSegment(1, 0.0, 1.0, "before one", translation="之前一"),
            t.TranscriptSegment(2, 1.0, 2.0, "before two", translation="之前二"),
            t.TranscriptSegment(3, 2.0, 3.0, "current", translation="当前"),
            t.TranscriptSegment(4, 3.0, 4.0, "after one", translation="之后一"),
            t.TranscriptSegment(5, 4.0, 5.0, "after two", translation="之后二"),
        ]
        self.transcript = t.Transcript("video.json", "en", self.segments)

    def test_context_clips_at_first_and_last_segment(self):
        first = self.segments[0]
        last = self.segments[-1]

        self.assertEqual(
            t.segment_context_items(
                self.transcript, first, self.ctx, window=2, before=True
            ),
            [],
        )
        self.assertEqual(
            t.segment_context_items(
                self.transcript, last, self.ctx, window=2, before=False
            ),
            [],
        )

    def test_context_items_preserve_before_and_after_order(self):
        current = self.segments[2]

        before = t.segment_context_items(
            self.transcript, current, self.ctx, window=2, before=True
        )
        after = t.segment_context_items(
            self.transcript, current, self.ctx, window=2, before=False
        )

        self.assertEqual([item["id"] for item in before], [1, 2])
        self.assertEqual([item["en"] for item in before], ["before one", "before two"])
        self.assertEqual([item["id"] for item in after], [4, 5])
        self.assertEqual([item["en"] for item in after], ["after one", "after two"])

    def test_context_without_target_omits_target_but_keeps_source_and_id(self):
        context = t.segment_context_items(
            self.transcript,
            self.segments[2],
            self.ctx,
            window=1,
            before=True,
            include_target=False,
        )

        self.assertEqual(context, [{"id": 2, "en": "before two"}])
        self.assertNotIn("zh", context[0])

    def test_context_with_target_includes_existing_translation(self):
        context = t.segment_context_items(
            self.transcript,
            self.segments[2],
            self.ctx,
            window=1,
            before=True,
            include_target=True,
        )

        self.assertEqual(context, [{"id": 2, "en": "before two", "zh": "之前二"}])

    def test_make_source_item_serializes_context_arrays_without_changing_current_item(self):
        before = t.segment_context_items(
            self.transcript,
            self.segments[2],
            self.ctx,
            window=1,
            before=True,
            include_target=False,
        )
        after = t.segment_context_items(
            self.transcript,
            self.segments[2],
            self.ctx,
            window=1,
            before=False,
            include_target=False,
        )

        item = t.make_source_item(
            3,
            self.ctx,
            "current",
            context_before=before,
            context_after=after,
        )

        self.assertEqual(
            item.to_json_value(),
            {
                "id": 3,
                "en": "current",
                "context_before": [{"id": 2, "en": "before two"}],
                "context_after": [{"id": 4, "en": "after one"}],
            },
        )


if __name__ == "__main__":
    unittest.main()
