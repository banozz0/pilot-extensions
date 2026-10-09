import json
import os
import subprocess

from harness import ExtensionTestCase


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@t"))


class ReposTests(ExtensionTestCase):
    EXTENSION = "repos"

    def setUp(self):
        super().setUp()
        self.code = self.box.home / "code"
        self.repo("clean", remote="git@github.com:someone/clean.git")
        dirty = self.repo("dirty", remote="https://user@gitlab.com/group/dirty.git")
        (dirty / "new.txt").write_text("x")
        self.repo("oss/nested")
        self.repo("a/b/three")
        self.repo("a/b/c/four")
        self.repo("node_modules/skipped")
        self.repo(".hidden/skipped")
        self.repo("dirty/inside")

    def repo(self, rel, remote=None):
        path = self.code / rel
        path.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main", cwd=path)
        (path / "f").write_text("f")
        git("add", "f", cwd=path)
        git("commit", "-q", "-m", "one", cwd=path)
        if remote:
            git("remote", "add", "origin", remote, cwd=path)
        return path

    def rows(self, result):
        return {i["title"]: i for i in result.items}

    def test_lists_repos_to_depth_three_with_branch_and_changes(self):
        r = self.run_ext("repos", home=self.box.home)
        self.assertItemShape(r.items)
        rows = self.rows(r)
        self.assertEqual(sorted(rows), ["a/b/three", "clean", "dirty", "oss/nested"])
        self.assertEqual(rows["clean"]["subtitle"], "main · clean · ~/code/clean")
        self.assertEqual(rows["dirty"]["subtitle"], "main · uncommitted changes · ~/code/dirty")
        self.assertEqual(rows["dirty"]["icon"], "✏️")
        self.assertLess(r.elapsed, 2)

    def test_actions_open_terminal_lazygit_finder_remote_and_copy(self):
        self.box.shim("lazygit")
        rows = self.rows(self.run_ext("repos", home=self.box.home))
        clean = str(self.code / "clean")
        titles = [a["title"] for a in rows["clean"]["actions"]]
        self.assertTrue(titles[0].startswith("Open in "))
        self.assertEqual(titles[1:], ["Open lazygit", "Open in Finder", "Open on GitHub", "Copy path"])
        self.assertEqual(rows["clean"]["actions"][0]["context"], {"path": clean})
        self.assertEqual(rows["clean"]["actions"][3]["url"], "https://github.com/someone/clean")
        self.assertEqual(rows["dirty"]["actions"][3], {"type": "open", "url": "https://gitlab.com/group/dirty",
                                                        "title": "Open on gitlab.com"})
        self.assertNotIn("Open on", " ".join(a["title"] for a in rows["oss/nested"]["actions"]))

    def test_a_detached_head_shows_its_commit(self):
        nested = self.code / "oss" / "nested"
        git("checkout", "-q", "--detach", cwd=nested)
        rows = self.rows(self.run_ext("repos", home=self.box.home))
        self.assertIn("(detached)", rows["oss/nested"]["subtitle"])

    def test_a_slow_git_keeps_the_list_inside_two_seconds_with_the_cached_marks(self):
        self.run_ext("repos", home=self.box.home)
        cache = json.loads((self.box.cache / "repos.json").read_text())
        self.assertEqual(cache[str(self.code / "dirty")], True)
        self.box.shim("git", "sleep 5")
        r = self.run_ext("repos", home=self.box.home)
        self.assertLess(r.elapsed, 2)
        self.assertEqual(self.rows(r)["dirty"]["subtitle"], "main · uncommitted changes · ~/code/dirty")

    def test_the_folder_preference_and_a_missing_folder(self):
        r = self.run_ext("repos", preferences={"folder": str(self.code / "oss")})
        self.assertEqual([i["title"] for i in r.items], ["nested"])
        r = self.run_ext("repos", preferences={"folder": str(self.code / "nope")})
        self.assertIn("has no folder", r.of_type("error")[0]["text"])

    def test_open_terminal_uses_the_chosen_app_through_open(self):
        self.box.shim("open")
        app = self.box.root / "Kitty.app"
        app.mkdir()
        repo = str(self.code / "clean")
        r = self.run_ext("open-terminal", context={"path": repo}, preferences={"terminal": str(app)})
        self.assertEqual(self.box.shim_calls(), ["open -a %s %s" % (app, repo)])
        self.assertEqual(r.of_type("hud")[0]["text"], "clean in Kitty")

    def test_open_terminal_in_ghostty_opens_a_new_window_there(self):
        self.box.shim("open")
        app = self.box.root / "Ghostty.app"
        app.mkdir()
        repo = str(self.code / "clean")
        self.run_ext("open-terminal", context={"path": repo}, preferences={"terminal": str(app)})
        self.assertEqual(self.box.shim_calls(),
                         ["open -na %s --args --working-directory=%s --window-save-state=never" % (app, repo)])

    def test_lazygit_runs_in_ghostty_or_through_a_command_file_in_terminal(self):
        self.box.shim("open")
        lazygit = self.box.shim("lazygit")
        repo = str(self.code / "clean")
        ghostty = self.box.root / "Ghostty.app"
        ghostty.mkdir()
        self.run_ext("lazygit", context={"path": repo}, preferences={"terminal": str(ghostty)})
        other = self.box.root / "Other.app"
        other.mkdir()
        r = self.run_ext("lazygit", context={"path": repo}, preferences={"terminal": str(other)})
        calls = self.box.shim_calls()
        self.assertEqual(calls[0], "open -na %s --args --working-directory=%s --window-save-state=never --command=%s"
                         % (ghostty, repo, lazygit))
        script = self.box.cache / "lazygit.command"
        self.assertEqual(calls[1], "open -a Terminal %s" % script)
        self.assertIn("cd %s && exec %s" % (repo, lazygit), script.read_text())
        self.assertEqual(r.of_type("hud")[0]["text"], "lazygit on clean")

    def test_a_missing_repo_folder_is_an_error(self):
        self.box.shim("open")
        r = self.run_ext("open-terminal", context={"path": str(self.code / "gone")})
        self.assertEqual(r.of_type("error")[0]["text"], "got no repo folder that exists")
        self.assertEqual(self.box.shim_calls(), [])
