import unittest

import translate_srt as t


class SubtitleTimeTests(unittest.TestCase):
    def test_ass_rounding_carries_into_seconds_minutes_and_hours(self):
        for value, expected in ((1.9999, "0:00:02.00"), (59.9999, "0:01:00.00"),
                                (3599.9999, "1:00:00.00"), (12345.678, "3:25:45.68")):
            with self.subTest(value=value):
                self.assertEqual(t.ass_time(value), expected)

    def test_srt_rounding_carries_into_seconds_minutes_and_hours(self):
        for value, expected in ((1.9999, "00:00:02,000"), (59.9999, "00:01:00,000"),
                                (3599.9999, "01:00:00,000"), (12345.678, "03:25:45,678")):
            with self.subTest(value=value):
                self.assertEqual(t.srt_time(value), expected)

    def test_negative_time_stays_clamped_at_zero(self):
        self.assertEqual(t.ass_time(-1), "0:00:00.00")
        self.assertEqual(t.srt_time(-1), "00:00:00,000")
