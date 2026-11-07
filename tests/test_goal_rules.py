"""Success-condition parsing: only wordings we fully understand are judged. No data pack needed."""
import unittest

from src.goals import parse_condition


class ParseConditionTests(unittest.TestCase):
    def test_every_wording_in_the_pack_and_the_replay_correction(self):
        cases = {
            'any title': ('any', None), 'any episode': ('any', None), 'any': ('any', None),
            'is_trial = false': ('paid', None), 'any paid plan': ('paid', None),
            'on a later IST calendar day than the goal start': ('later_day', None),
            'on a later IST calendar day within the window': ('later_day', None),   # the replay's G03 correction
            'same content_slug as the unfinished series': ('same_slug', None),
            'a title released in the 30 days before the goal start': ('new_release', 30),
            'on 3 distinct IST days': ('distinct_days', 3),
        }
        for text, want in cases.items():
            self.assertEqual(parse_condition(text), want, text)

    def test_unknown_or_extended_wording_is_not_guessed(self):
        for text in ('engages meaningfully', 'at least 2 episodes of the same series', '', None,
                     'any title on the TV app',               # an extra clause must not be dropped silently
                     'same content_slug as the unfinished series, two episodes'):
            self.assertIsNone(parse_condition(text), text)

    def test_numbers_are_read_not_assumed(self):
        self.assertEqual(parse_condition('on 5 distinct IST days'), ('distinct_days', 5))
        self.assertEqual(parse_condition('a title released in the 14 days before the goal start'), ('new_release', 14))


if __name__ == '__main__':
    unittest.main()
