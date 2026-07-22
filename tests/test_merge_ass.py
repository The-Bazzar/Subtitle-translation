import os
import tempfile
import unittest

import merge_ass


class MergeAssTests(unittest.TestCase):
    def test_merge_ass_combines_matching_dialogues_and_keeps_unmatched(self):
        zh_ass = """[Script Info]
Title: zh

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: bi-zh,Noto Serif CJK SC,90,&H00CCF2FF,&H00FFFFFF,&H001A1A1A,&H00000000,-1,0,0,0,100,100,0,0,1,4,1.0,2,0,0,74,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,bi-zh,,0,0,0,,中文
Dialogue: 0,0:00:03.00,0:00:04.00,bi-zh,,0,0,0,,仅中文
"""
        en_ass = """[Script Info]
Title: en

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: bi-en,Cascadia Code,48,&H00D8D4D4,&H0000FFFF,&H001A1A1A,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,0,0,20,1
Style: extra-en,Cascadia Code,42,&H00D8D4D4,&H0000FFFF,&H001A1A1A,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,0,0,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,bi-en,,0,0,0,,English
Dialogue: 0,0:00:05.00,0:00:06.00,extra-en,,0,0,0,,Only English
"""

        with tempfile.TemporaryDirectory() as tmp:
            zh_path = os.path.join(tmp, "video.zh.ass")
            en_path = os.path.join(tmp, "video.en.ass")
            out_path = os.path.join(tmp, "video.en-zh.ass")
            with open(zh_path, "w", encoding="utf-8") as f:
                f.write(zh_ass)
            with open(en_path, "w", encoding="utf-8") as f:
                f.write(en_ass)

            merge_ass.merge_ass_files(zh_path, en_path, out_path)

            with open(out_path, "r", encoding="utf-8") as f:
                merged = f.read()

        self.assertIn("Style: bi-en", merged)
        self.assertIn("Style: extra-en", merged)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,bi-zh,,0,0,0,,中文\\N{\\rbi-en}English", merged)
        self.assertIn("Dialogue: 0,0:00:03.00,0:00:04.00,bi-zh,,0,0,0,,仅中文", merged)
        self.assertIn("Dialogue: 0,0:00:05.00,0:00:06.00,extra-en,,0,0,0,,Only English", merged)


if __name__ == "__main__":
    unittest.main()
