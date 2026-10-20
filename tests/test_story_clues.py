"""The story fixes from Episode 2: clues spread across clips, dead air, timelapse order, music."""

import unittest
from pathlib import Path

from kdcut import continuity, llm, music, story, vision
from kdcut.blocks import Block
from kdcut.planner import Plan, continues_across, protect_sentences
from kdcut.timeline import kept_ranges

EDIT = {"pad_before": 0.15, "pad_after": 0.3, "keep_pauses_up_to": 0.6}


def bookshelf_blocks():
    return [Block("B0031", 0, 9.0, 11.0, "All right, so enchanting table, check."),
            Block("B0032", 1, 5.0, 6.0, "And as for bookshelves."),
            Block("B0033", 2, 4.0, 6.0, "They are also"),
            Block("B0034", 3, 6.0, 7.0, "A check."),
            Block("B0035", 4, 5.0, 10.0, "So now I think I just need a good way to get levels.")]


class AcrossClipsTests(unittest.TestCase):
    def test_sentence_carried_over_clips_is_spotted(self):
        b = bookshelf_blocks()
        self.assertTrue(continues_across(b[1], b[2]))     # "And as for bookshelves." -> "They are also"
        self.assertTrue(continues_across(b[2], b[3]))     # "They are also" -> "A check."
        self.assertFalse(continues_across(b[3], b[4]))    # "A check." is done
        self.assertFalse(continues_across(b[0], b[1]))

    def test_checklist_parts_come_back_together(self):
        blocks = bookshelf_blocks()
        plan = Plan(drop={"B0033": "fragment", "B0034": "menu check fragment"})
        protect_sentences(plan, blocks)
        self.assertEqual(plan.drop, {})

    def test_whole_unfinished_thought_can_still_go(self):
        blocks = bookshelf_blocks()
        plan = Plan(drop={"B0032": "x", "B0033": "x", "B0034": "x"})
        protect_sentences(plan, blocks)
        self.assertEqual(set(plan.drop), {"B0032", "B0033", "B0034"})


class DeadAirTests(unittest.TestCase):
    def test_extend_before_he_talks_again_is_cut(self):
        blocks = [Block("B1", 0, 9.0, 26.3, "we found ourself a fortress right next to our spawn"),
                  Block("B2", 0, 30.0, 38.0, "now there is one disappointing thing")]
        rngs = kept_ranges(blocks, Plan(extend={"B1": 3.0}), [60.0], EDIT)
        self.assertEqual(len(rngs), 2)                     # the 26-30s pause is gone

    def test_extend_to_show_something_still_works(self):
        blocks = [Block("B1", 0, 2.0, 6.0, "a check."), Block("B2", 0, 30.0, 38.0, "so now")]
        rngs = kept_ranges(blocks, Plan(extend={"B1": 8.0}), [60.0], EDIT)
        self.assertGreater(rngs[0].end, 13.5)


class StoryOutlineTests(unittest.TestCase):
    def test_lists_sent_as_text_are_read(self):
        schema = story.SCHEMA
        got = llm.coerce({"summary": "s", "beats": '[{"from": "B0001", "to": "B0002", "kind": "intro", '
                                                    '"what": "x", "matters": 3}]'}, schema)
        self.assertEqual(got["beats"][0]["from"], "B0001")

    def test_loose_ids_are_understood(self):
        fix = story.id_fixer({"B0005", "B0012"})
        self.assertEqual(fix("B5"), "B0005")
        self.assertEqual(fix("b0012"), "B0012")
        self.assertEqual(fix("B0005-B0012"), "B0005")
        self.assertIsNone(fix("B0099"))

    def test_empty_outline_is_asked_again_and_must_keep_is_kept(self):
        blocks = bookshelf_blocks()
        calls = []

        def caller(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return {"summary": "s", "beats": []}
            return {"summary": "s", "beats": [{"from": "B31", "to": "B35", "kind": "goal", "what": "gear",
                                               "matters": 3}],
                    "must_keep": [{"id": "B0033", "why": "bookshelf checklist"}]}
        cfg = {"channel": {}}
        o = story.outline(cfg, {}, blocks, caller, log=lambda *_: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(o.beats[0]["from"], "B0031")
        plan = Plan(drop={"B0033": "fragment"})
        story.protect_threads(plan, o, blocks)
        self.assertNotIn("B0033", plan.drop)


class ClipLookTests(unittest.TestCase):
    def test_a_death_screen_at_the_start_of_a_clip_gets_looked_at(self):
        blocks = [Block("B0012", 0, 201.0, 202.0, "horrible"),
                  Block("B0013", 1, 5.0, 7.0, "Well, that's one way to get back."),
                  Block("B0014", 2, 11.0, 20.0, "Now I literally have made a long journey twice as long")]
        looks = vision.clip_looks(blocks, [243.0, 13.3, 22.5], [])
        rec2 = [lk for lk in looks if lk.source == 1]
        self.assertTrue(any(lk.time < 5.0 for lk in rec2))           # the opening seconds
        opening = min(rec2, key=lambda lk: lk.time)
        self.assertEqual(opening.after_block, "B0012")                # shown just before "Well, ..."


class ViewerPassTests(unittest.TestCase):
    def test_restores_and_moves(self):
        blocks = bookshelf_blocks()
        plan = Plan(drop={"B0032": "fragment", "B0035": "x"})

        def caller(cfg, system, user, *a):
            self.assertIn("B0032 CUT (fragment)", user)
            self.assertIn(">> TIMELAPSE Farm.mp4", user)
            return {"restore": [{"id": "B0032", "why": "'also check' needs it"},
                                {"id": "B0031", "why": "not cut, ignored"}],
                    "move_timelapse": [{"file": "Farm.mp4", "after": "B0034", "why": "after the plan"},
                                       {"file": "Nope.mp4", "after": "B0034", "why": "unknown file"}],
                    "problems": [{"at": "B0035", "what": "jump"}]}
        fixes = continuity.check({}, {}, blocks, dict(plan.drop), [20, 10, 10, 10, 20], caller,
                                 inserts={"B0031": ["TIMELAPSE Farm.mp4 (30s) plays here"]},
                                 timelapse_files=["Farm.mp4"])
        self.assertEqual([r["id"] for r in fixes.restore], ["B0032"])
        self.assertEqual([m["file"] for m in fixes.moves], ["Farm.mp4"])
        self.assertEqual(continuity.apply(plan, fixes, blocks), ["B0032"])
        self.assertEqual(set(plan.drop), {"B0035"})


class MusicFeelTests(unittest.TestCase):
    def test_peaceful_folder_is_low_energy(self):
        t = music.Track("p", "Lonely River", "Roots and Recognition", ["Peaceful"], 150)
        offered, _ = music.for_vibe([t], "upbeat")
        self.assertEqual(offered, [])
        self.assertEqual(music.for_vibe([t], "chill")[0], [t])

    def test_tagged_energy_wins(self):
        t = music.Track("p", "Calm Down", "x", [], 150, energy="High")
        self.assertEqual(music.for_vibe([t], "upbeat")[0], [t])


if __name__ == "__main__":
    unittest.main()
