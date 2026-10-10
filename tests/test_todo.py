import json
import time

from harness import ExtensionTestCase, manifest


class TodoTests(ExtensionTestCase):
    EXTENSION = "todo"

    def stored(self):
        return json.loads((self.box.data / "todos.json").read_text())["items"]

    def store(self, *items):
        (self.box.data / "todos.json").write_text(json.dumps({"version": 1, "items": list(items)}))

    def todo(self, id_, text, done=False, age=60):
        t = {"id": id_, "text": text, "done": done, "created": time.time() - age}
        if done:
            t["doneAt"] = time.time() - age / 2
        return t

    def rows(self, blocks):
        return [i for b in blocks if b["type"] == "list" for i in b["items"]]

    # ---- adding ----

    def test_add_from_the_input_box_stores_the_typed_text(self):
        r = self.run_ext("add", input="  buy   milk ")
        self.assertEqual(r.actions, [])
        self.assertEqual(r.of_type("hud")[0]["text"], "Added: buy milk")
        [t] = self.stored()
        self.assertEqual((t["text"], t["done"]), ("buy milk", False))

    def test_todo_buy_milk_in_hyper_space_adds_it_in_one_step(self):
        # Pilot's keyword answer: "todo buy milk" runs add with input "buy milk" and no context.
        add = next(c for c in manifest(self.EXTENSION)["commands"] if c["name"] == "add")
        self.assertEqual((add["keyword"], add["mode"]), ("todo", "no-view"))
        r = self.run_ext("add", input="buy milk")
        self.assertEqual(r.of_type("hud")[0]["text"], "Added: buy milk")
        self.assertEqual([t["text"] for t in self.stored()], ["buy milk"])

    def test_add_from_a_row_takes_the_context_text_and_empty_says_so(self):
        self.run_ext("add", context={"text": "call mum"})
        self.assertEqual([t["text"] for t in self.stored()], ["call mum"])
        r = self.run_ext("add", input="   ")
        self.assertEqual(r.of_type("error")[0]["text"], "type a to-do first")
        self.assertEqual(len(self.stored()), 1)

    def test_typed_search_offers_add_first_then_matching_todos(self):
        self.store(self.todo("a", "buy milk"), self.todo("b", "walk dog"), self.todo("c", "milk the cat", done=True))
        r = self.run_ext("todo", query="milk")
        self.assertItemShape(r.items)
        self.assertEqual(r.items[0]["title"], "Add “milk”")
        self.assertEqual(r.items[0]["actions"][0], {"type": "run", "command": "add", "title": "Add",
                                                    "context": {"text": "milk"}})
        self.assertEqual([i["title"] for i in r.items[1:]], ["buy milk", "milk the cat"])

    def test_empty_search_lists_open_newest_first_then_done(self):
        self.store(self.todo("a", "old", age=600), self.todo("b", "new", age=10), self.todo("c", "finished", done=True))
        r = self.run_ext("todo", query="")
        self.assertItemShape(r.items)
        self.assertEqual([i["title"] for i in r.items], ["new", "old", "finished"])
        self.assertEqual(r.items[0]["actions"][0]["command"], "done")
        self.assertEqual(r.items[2]["actions"][0]["command"], "undone")
        self.assertEqual(self.run_ext("todo", query="").items[0]["title"], "new")

    def test_empty_list_says_how_to_add(self):
        r = self.run_ext("todo", query="")
        self.assertEqual(r.items[0]["title"], "Nothing to do")

    # ---- ticking off and deleting ----

    def test_done_undone_and_delete_change_only_the_named_todo(self):
        self.store(self.todo("a", "one"), self.todo("b", "two"))
        self.run_ext("done", context={"id": "a"})
        self.assertEqual([(t["id"], t["done"]) for t in self.stored()], [("a", True), ("b", False)])
        self.assertIn("doneAt", self.stored()[0])
        self.run_ext("undone", context={"id": "a"})
        self.assertNotIn("doneAt", self.stored()[0])
        self.assertFalse(self.stored()[0]["done"])
        self.run_ext("delete", context={"id": "b"})
        self.assertEqual([t["id"] for t in self.stored()], ["a"])

    def test_a_gone_todo_says_so_and_changes_nothing(self):
        self.store(self.todo("a", "one"))
        r = self.run_ext("done", context={"id": "zz"})
        self.assertEqual(r.of_type("error")[0]["text"], "that to-do is gone")
        self.assertFalse(self.stored()[0]["done"])

    def test_clear_done_keeps_the_open_ones(self):
        self.store(self.todo("a", "one", done=True), self.todo("b", "two"), self.todo("c", "three", done=True))
        r = self.run_ext("clear-done")
        self.assertEqual(r.of_type("hud")[0]["text"], "Deleted 2 done to-dos")
        self.assertEqual([t["id"] for t in self.stored()], ["b"])

    def test_an_unreadable_file_reads_as_empty_and_the_next_add_rewrites_it(self):
        (self.box.data / "todos.json").write_text("{not json")
        self.assertEqual(self.bar()["text"], "☑️ 0")
        self.run_ext("add", input="fresh")
        self.assertEqual([t["text"] for t in self.stored()], ["fresh"])

    # ---- surfaces ----

    def test_bar_counts_the_open_todos(self):
        self.store(self.todo("a", "one"), self.todo("b", "two"), self.todo("c", "three", done=True))
        self.assertEqual(self.bar()["text"], "☑️ 2")

    def test_popover_has_the_input_open_rows_with_inline_buttons_and_done_folded(self):
        self.store(self.todo("a", "one"), self.todo("b", "two", done=True), self.todo("c", "three", done=True))
        blocks = self.popover()
        self.assertEqual(blocks[0]["type"], "input")
        self.assertEqual(blocks[0]["placeholder"], "Add a to-do")
        self.assertEqual(blocks[0]["action"]["command"], "add")
        rows = self.rows(blocks)
        self.assertEqual([r["title"] for r in rows], ["one", "2 done ›"])
        self.assertEqual([a["command"] for a in rows[0]["inline"]], ["done", "delete"])
        self.assertEqual(rows[0]["inline"][0]["context"], {"id": "a"})
        self.assertEqual(rows[1]["actions"][0], {"type": "push", "command": "done-list", "title": "Show done to-dos"})

    def test_done_list_in_the_popover_lists_them_with_undo_and_a_confirmed_clear(self):
        self.store(self.todo("a", "one"), self.todo("b", "two", done=True))
        blocks = self.blocks("done-list", "popover")
        self.assertEqual([r["title"] for r in self.rows(blocks)], ["two"])
        self.assertEqual(self.rows(blocks)[0]["inline"][0]["command"], "undone")
        clear = blocks[-1]["actions"][0]
        self.assertEqual((clear["command"], clear["ask"]), ("clear-done", {"confirm": "Delete 1 done to-do?"}))

    def test_page_lists_every_todo_with_counts(self):
        self.store(self.todo("a", "one"), self.todo("b", "two", done=True), self.todo("c", "three", done=True, age=4e5))
        blocks = self.page()
        stats = {s["label"]: s["value"] for s in blocks[0]["items"]}
        self.assertEqual((stats["Open"], stats["Done"]), ("1", "2"))
        self.assertIn(stats["Done today"], ("1", "2"))  # "two" was done 30 s ago, "three" days ago
        self.assertEqual(blocks[1]["type"], "input")
        lists = [b for b in blocks if b["type"] == "list"]
        self.assertEqual([(b["title"], [i["title"] for i in b["items"]]) for b in lists],
                         [("To do", ["one"]), ("Done", ["two", "three"])])

    def test_empty_popover_and_page_still_offer_the_input(self):
        self.assertEqual([b["type"] for b in self.popover()], ["input", "markdown"])
        self.assertEqual([b["type"] for b in self.page()], ["stats", "input", "markdown"])
