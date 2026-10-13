"""Reading Claude Code's screen: the input box, and the tasks its exit question names."""

import unittest

from sessionlift.screen import prompt_text, stopped_tasks

RULE = "─" * 40


class ScreenTest(unittest.TestCase):
    def test_the_input_box_between_rules(self):
        self.assertEqual(prompt_text(f"older output\n{RULE}\n❯ half a sentence\n{RULE}\n  ? for shortcuts"),
                         "half a sentence")
        self.assertEqual(prompt_text(f"{RULE}\n❯ \n{RULE}"), "")

    def test_a_prompt_without_rules(self):
        self.assertEqual(prompt_text("x\n❯ /resume abc"), "/resume abc")
        self.assertIsNone(prompt_text("no prompt here"))

    def test_the_tasks_the_exit_question_names(self):
        screen = ("Background work is running\n  These will stop when you exit:\n"
                  "  subagent · reviewing the board\n  shell · npm test\n\n❯ 1. Exit and stop tasks\n  2. Cancel\n")
        self.assertEqual(stopped_tasks(screen), "subagent · reviewing the board; shell · npm test")
        self.assertEqual(stopped_tasks("nothing asked"), "")


if __name__ == "__main__":
    unittest.main()
