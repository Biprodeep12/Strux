"""TODO details: files kept with a TODO, editing due / priority / notes, notes from the chat."""
import tempfile
import time
import unittest
from unittest import mock

from watcher.brain import agent, chat
from watcher.planner import MAX_FILE_BYTES, Planner, _safe_name
from tests.test_planner import FakeLLM


class TodoFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)
        self.t = self.p.add_todo("Finish OS lab report")

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_safe_names(self):
        self.assertEqual(_safe_name("a/b\\c.pdf"), "c.pdf")
        self.assertEqual(_safe_name("..\\..\\x.txt"), "x.txt")
        self.assertEqual(_safe_name('con<>:"|?*.txt'), "con_______.txt")
        self.assertEqual(_safe_name("a\x00b.txt"), "a_b.txt")
        self.assertEqual(_safe_name(""), "file")
        self.assertEqual(_safe_name(" . "), "file")

    def test_add_list_delete(self):
        f = self.p.add_file(self.t["id"], "../../evil.txt", b"hello", "text/plain")
        path = self.p.file_path(f)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"hello")
        self.assertEqual(path.parent.parent, self.p.files_dir)            # stays inside data/todo_files/<id>/
        self.assertEqual(f["name"], "evil.txt")
        self.assertEqual([x["id"] for x in self.p.files(self.t["id"])], [f["id"]])
        self.assertEqual(self.p.todos()[0]["files"], 1)
        self.assertTrue(self.p.delete_file(f["id"]))
        self.assertFalse(path.exists())
        self.assertFalse(self.p.delete_file(f["id"]))

    def test_limits(self):
        with self.assertRaises(ValueError):
            self.p.add_file(self.t["id"], "x", b"")
        with self.assertRaises(KeyError):
            self.p.add_file(99999, "x.txt", b"x")
        with mock.patch("watcher.planner.MAX_FILE_BYTES", 10):
            with self.assertRaises(ValueError):
                self.p.add_file(self.t["id"], "big.bin", b"x" * 11)

    def test_deleting_the_todo_deletes_its_files(self):
        f = self.p.add_file(self.t["id"], "a.txt", b"a")
        folder = self.p.file_path(f).parent
        self.p.delete_todo(self.t["id"])
        self.assertFalse(folder.exists())
        self.assertIsNone(self.p.file(f["id"]))

    def test_model_sees_notes_and_files(self):
        self.p.update_todo(self.t["id"], notes="Use the template from the course page\nsubmit as PDF")
        self.p.add_file(self.t["id"], "template.docx", b"x")
        ctx = self.p.context_text()
        self.assertIn("notes: Use the template from the course page submit as PDF", ctx)
        self.assertIn("1 file attached", ctx)


class TodoChatNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)
        self.t = self.p.add_todo("Finish OS lab report", notes="first line")

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_note_is_appended(self):
        llm = FakeLLM(turns=[[{"name": "update_todo", "arguments": {"todo": "OS lab report", "note": "use the new template"}}]])
        chat.answer(llm, None, [{"role": "user", "content": "add a note to the OS lab report todo: use the new template"}], planner=self.p)
        self.assertEqual(self.p.find_todo(str(self.t["id"]))["notes"], "first line\nuse the new template")

    def test_invented_note_is_refused(self):
        llm = FakeLLM(turns=[[{"name": "update_todo", "arguments": {"todo": "OS lab report", "note": "professor extended deadline"}}]])
        chat.answer(llm, None, [{"role": "user", "content": "add a note to the OS lab report todo"}], planner=self.p)
        self.assertEqual(self.p.find_todo(str(self.t["id"]))["notes"], "first line")


class TodoFilesApi(unittest.TestCase):
    def test_endpoints(self):
        from starlette.testclient import TestClient
        from watcher.api.server import create_api
        from watcher.app import App
        from tests.test_api import _cfg
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = App(_cfg(tmp.name))
        app.store.start()
        try:
            c = TestClient(create_api(app), raise_server_exceptions=False)
            t = c.post("/api/todos", json={"title": "Read paper"}).json()["todo"]
            # edit time, priority and notes after creating
            r = c.patch(f"/api/todos/{t['id']}", json={"due": "tomorrow 18:00", "priority": "high", "notes": "pages 1-12"}).json()["todo"]
            self.assertEqual((r["priority"], r["notes"]), (2, "pages 1-12"))
            self.assertGreater(r["due_ts"], time.time())
            self.assertIsNone(c.patch(f"/api/todos/{t['id']}", json={"due": None}).json()["todo"]["due_ts"])
            self.assertEqual(c.patch(f"/api/todos/{t['id']}", json={"due": "blorp"}).status_code, 400)
            # files
            up = c.post(f"/api/todos/{t['id']}/files?name=paper.pdf", content=b"%PDF-1.4 x", headers={"Content-Type": "application/pdf"})
            self.assertEqual(up.status_code, 200, up.text)
            f = up.json()["file"]
            self.assertEqual((f["name"], f["mime"], f["size"]), ("paper.pdf", "application/pdf", 10))
            self.assertEqual(c.get(f"/api/todos/{t['id']}/files").json()["files"][0]["id"], f["id"])
            got = c.get(f"/api/todo-files/{f['id']}")
            self.assertEqual(got.content, b"%PDF-1.4 x")
            self.assertIn("inline", got.headers["content-disposition"])
            self.assertIn("attachment", c.get(f"/api/todo-files/{f['id']}?download=1").headers["content-disposition"])
            self.assertEqual(c.get("/api/todos").json()["todos"][0]["files"], 1)
            # a program is shown in Explorer, never run
            exe = c.post(f"/api/todos/{t['id']}/files?name=setup.exe", content=b"MZ").json()["file"]
            with mock.patch("subprocess.Popen") as popen, mock.patch("os.startfile", create=True) as start:
                self.assertTrue(c.post(f"/api/todo-files/{exe['id']}/open").json()["shown_in_folder"])
                popen.assert_called_once()
                start.assert_not_called()
                self.assertEqual(c.post(f"/api/todo-files/{f['id']}/open").json(), {"ok": True})
                start.assert_called_once()
            # bad input never 500s
            self.assertEqual(c.post("/api/todos/99999/files?name=a.txt", content=b"x").status_code, 404)
            self.assertEqual(c.post(f"/api/todos/{t['id']}/files?name=a.txt", content=b"").status_code, 400)
            self.assertEqual(c.post(f"/api/todos/{t['id']}/files?name=big", content=b"x",
                                    headers={"Content-Length": str(MAX_FILE_BYTES + 1)}).status_code in (400, 413), True)
            self.assertEqual(c.get("/api/todo-files/99999").status_code, 404)
            self.assertEqual(c.delete(f"/api/todo-files/{f['id']}").json(), {"ok": True})
            self.assertEqual(c.delete(f"/api/todo-files/{f['id']}").status_code, 404)
        finally:
            app.planner.close()
            app.memory.close()
            app.store.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
