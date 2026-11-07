"""Cut lines that the next kept line reacts to come back (Episode 4 opened on "Yeah that's
right, I said it." after the AI cut "my world looks like garbage" as a "cold open")."""

import unittest

from kdcut import continuity
from kdcut.blocks import Block
from kdcut.planner import Plan, protect_context, refers_back, system_prompt


def ep4_opening():
    return [Block("B0001", 0, 4.0, 7.0, "my world looks like garbage yeah"),
            Block("B0002", 0, 11.0, 18.0, "that's right i said it it looks horrible this is literally "
                                         "the survival kingdom series and nothing looks like a kingdom"),
            Block("B0003", 0, 18.0, 26.0, "well except you you're you're kind of nice")]


class ReferBackTests(unittest.TestCase):
    def test_reactions_are_spotted(self):
        for text in ["that's right i said it it looks horrible",
                     "Yeah, that's right. I said it.",
                     "like I said, we need more iron",
                     "told you this would happen",
                     "you heard me, no more caves"]:
            self.assertTrue(refers_back(text), text)

    def test_ordinary_lines_are_not(self):
        for text in ["let's see if this works",
                     "so we need to find some iron first",
                     "I said we'd build a castle and here we are, three episodes later, "
                     "finally doing it, that's right"]:
            self.assertFalse(refers_back(text), text)


class ProtectContextTests(unittest.TestCase):
    def test_episode_4_opening_comes_back(self):
        plan = Plan(drop={"B0001": "weak cold open fragment"})
        self.assertEqual(protect_context(plan, ep4_opening()), ["B0001"])
        self.assertNotIn("B0001", plan.drop)

    def test_creator_cut_stays_cut(self):
        bl = ep4_opening()
        bl[0].flags.add("forced_drop")
        plan = Plan(drop={})
        self.assertEqual(protect_context(plan, bl), [])

    def test_a_retake_stays_cut(self):
        bl = [Block("B1", 0, 0.0, 3.0, "that's right i said it it looks"),
              Block("B2", 0, 3.5, 9.0, "that's right i said it it looks horrible.")]
        plan = Plan(drop={"B1": "false start"})
        self.assertEqual(protect_context(plan, bl), [])

    def test_far_away_or_other_clip_stays_cut(self):
        bl = [Block("B1", 0, 0.0, 3.0, "my world looks like garbage"),
              Block("B2", 1, 0.5, 4.0, "that's right i said it")]
        self.assertEqual(protect_context(Plan(drop={"B1": "x"}), bl), [])
        bl = [Block("B1", 0, 0.0, 3.0, "my world looks like garbage"),
              Block("B2", 0, 40.0, 44.0, "that's right i said it")]
        self.assertEqual(protect_context(Plan(drop={"B1": "x"}), bl), [])

    def test_a_cut_reaction_brings_nothing_back(self):
        plan = Plan(drop={"B0001": "x", "B0002": "y"})
        self.assertEqual(protect_context(plan, ep4_opening()), [])

    def test_walks_back_through_a_run_on_line(self):
        bl = [Block("B1", 0, 0.0, 4.0, "okay so honestly"),
              Block("B2", 0, 4.3, 7.0, "my world looks like garbage"),
              Block("B3", 0, 9.0, 14.0, "that's right i said it")]
        plan = Plan(drop={"B1": "x", "B2": "y"})
        self.assertEqual(protect_context(plan, bl), ["B2", "B1"])


class PromptTests(unittest.TestCase):
    def test_editor_is_told_the_opening_is_not_a_cold_open(self):
        sp = system_prompt({"channel": {"name": "KDPlays"}, "edit": {}}, {})
        self.assertIn("not a cold open", sp)

    def test_viewer_pass_checks_the_first_line(self):
        self.assertIn("first kept line", continuity.SYSTEM)
