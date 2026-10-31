import unittest

from harness import ExtensionTestCase


class SummarisePageTests(ExtensionTestCase):
    """The page does its job (Sven, 2026-10-31): a box to paste a link or choose a file, Summarise, the summary with Copy.
    No network and no AI: a text document is the source, and the AI's answer is handed to `summarised` as Pilot would."""
    EXTENSION = "summarise"

    def setUp(self):
        super().setUp()
        self.box.shim("open")
        self.box.shim("pbpaste", "exit 0")

    def box_of(self, blocks):
        self.assertEqual(blocks[0]["type"], "section")
        self.assertEqual(blocks[0]["title"], "Summarise")
        box = blocks[0]["blocks"][0]
        self.assertEqual(box["type"], "input")
        return box

    def test_an_empty_page_leads_with_the_box(self):
        blocks = self.page()
        box = self.box_of(blocks)
        self.assertEqual(box["action"], {"type": "run", "command": "fetch", "title": "Summarise"})
        self.assertIn("pdf", box["files"])
        self.assertIn("path", box["placeholder"])  # an older Pilot has no Choose button: a pasted path still reads
        self.assertEqual([b["type"] for b in blocks], ["section", "markdown"])
        self.assertNotIn("Hyper+Space", blocks[1]["text"])  # the page does the job itself (Sven, 2026-10-31)
        self.assertNotIn("refresh", self.blocks_message("page", "page"))

    def test_a_chosen_file_is_summarised_and_its_summary_shows_with_copy(self):
        doc = self.box.home / "Notes.txt"
        doc.write_text("Pilot makes a Mac feel like Omarchy. " * 40)
        fetched = self.run_ext("fetch", input=str(doc))
        ai = fetched.actions[-1]
        self.assertEqual(ai["type"], "ai")
        self.assertIn("Pilot makes a Mac", ai["text"])
        self.assertEqual(ai["then"]["command"], "summarised")

        waiting = self.blocks_message("page", "page")
        self.assertEqual(waiting["refresh"], 2)  # asked again until the answer is in
        self.assertNotIn("copy", [a["type"] for b in waiting["blocks"] if b["type"] == "buttons" for a in b["actions"]])

        self.run_ext("summarised", context=ai["then"]["context"], input="- Pilot feels like Omarchy")
        blocks = self.page()
        self.box_of(blocks)  # the box stays, for the next one
        self.assertEqual(blocks[1]["title"], "Notes.txt")
        buttons = next(b["actions"] for b in blocks if b["type"] == "buttons")
        self.assertEqual(buttons[0], {"type": "copy", "text": "- Pilot feels like Omarchy", "title": "Copy summary"})
        summary = next(b for b in blocks if b.get("title") == "Summary · Pilot AI")
        self.assertEqual(summary["blocks"][0]["text"], "- Pilot feels like Omarchy")

    def test_a_failed_summary_offers_try_again_and_no_copy(self):
        doc = self.box.home / "Plan.md"
        doc.write_text("# Plan\n\nShip Pilot next week. " * 20)
        ai = self.run_ext("fetch", input=str(doc)).actions[-1]
        self.run_ext("summarised", context=dict(ai["then"]["context"], error="signed out"))
        blocks = self.page()
        actions = [a for b in blocks if b["type"] == "buttons" for a in b["actions"]]
        self.assertNotIn("copy", [a["type"] for a in actions])
        self.assertIn("Try again", [a["title"] for a in actions])


if __name__ == "__main__":
    unittest.main()
