"""Chat attachments: reading files / images, and how chat routes them."""
import base64
import io
import tempfile
import unittest
import zipfile

from PIL import Image

from watcher.brain import attachments as A
from watcher.brain import chat
from watcher.planner import Planner
from tests.test_planner import FakeLLM


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def docx_bytes(*paras: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def pdf_bytes(text: str) -> bytes:
    """A minimal one-page PDF with a text layer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref))
    return out.getvalue()


def png_bytes(w=3000, h=2000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (255, 0, 0, 128)).save(buf, "PNG")
    return buf.getvalue()


class ReadTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(A.file_text({"name": "a.py", "data": b64(b"def f():\n    return 1\n")}), "def f():\n    return 1\n")
        self.assertIn("Line two", A.file_text({"name": "notes.docx", "data": b64(docx_bytes("Line one", "Line two"))}))
        self.assertIn("Invoice 42", A.file_text({"name": "x.pdf", "type": "application/pdf", "data": b64(pdf_bytes("Invoice 42"))}))
        self.assertEqual(A.file_text({"name": "weird.ext", "data": b64(b"plain enough")}), "plain enough")
        with self.assertRaises(ValueError):
            A.file_text({"name": "blob.bin", "data": b64(b"\x00\x01\x02" * 100)})

    def test_image_is_shrunk_and_flattened(self):
        out = Image.open(io.BytesIO(base64.b64decode(A.image_b64({"name": "big.png", "data": b64(png_bytes())}))))
        self.assertEqual((out.format, max(out.size), out.mode), ("JPEG", 1280, "RGB"))

    def test_expand_limits_and_history(self):
        big = "x" * 30_000
        msgs = [{"role": "user", "content": "what's in these?", "attachments": [
                    {"name": "a.txt", "data": b64(big.encode())}, {"name": "b.txt", "data": b64(big.encode())},
                    {"name": "c.txt", "data": b64(b"third")}, {"name": "shot.png", "type": "image/png", "data": b64(png_bytes(200, 100))},
                    {"name": "bad.bin", "data": b64(b"\x00" * 50)}]},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "and the first one?"}]
        out, images, names, problems = A.expand(msgs)
        self.assertIn("[Attached file: a.txt]", out[0]["content"])
        self.assertIn("truncated", out[0]["content"])
        self.assertLessEqual(len(out[0]["content"]), A.TOTAL + 2000)            # bounded
        self.assertIn("c.txt: skipped", " ".join(problems))
        self.assertIn("bad.bin", " ".join(problems))
        self.assertEqual((len(images), images[0]["msg"], images[0]["name"]), (1, 0, "shot.png"))   # slot: which message it came with
        self.assertEqual(out[2]["content"], "and the first one?")
        self.assertIn("shot.png", names)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.p = Planner(self.tmp.name)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def test_image_is_read_once_then_text_model_answers(self):
        from watcher.brain import vision
        vision._read_cache.clear()
        llm = FakeLLM(text="unused")
        llm.cfg = {"vision_model": "qwen2.5vl:3b"}
        calls = []

        def fake_chat(messages, **kw):
            calls.append(kw.get("model"))
            if kw.get("images"):
                self.assertEqual((kw["keep_alive"], kw["options"]["num_batch"]), (0, 128))   # gentle on the GPU
                return "22 Oct: SE revision\n23 Oct: SE test\nImage: a study timetable"
            return "Your timetable covers SE on 22-23 Oct."
        llm.chat = fake_chat
        img = A.image_b64({"name": "t.png", "data": b64(png_bytes(1600, 900))})
        used = {}
        out = chat.answer(llm, None, [{"role": "user", "content": "what's in this?"}], images=[{"msg": 0, "name": "t.png", "b64": img}],
                          route_text="what's in this?", planner=self.p, used=used)
        self.assertEqual(out, "Your timetable covers SE on 22-23 Oct.")
        self.assertEqual(calls, ["qwen2.5vl:3b", None])                     # vision reads, text model answers
        self.assertEqual(used["model"], "qwen2.5vl:3b + fake-model")
        # follow-up "add these as TODOs": cached read (no second vision run), tools get the timetable text
        tool_llm = FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "SE revision", "due": "22 Oct"}},
                                   {"name": "add_todo", "arguments": {"title": "SE test", "due": "23 Oct"}}]])
        tool_llm.cfg = llm.cfg
        tool_llm.chat = lambda messages, **kw: self.fail("vision must not run again")
        changes = []
        msgs = [{"role": "user", "content": "make day wise todo of this"}]
        chat.answer(tool_llm, None, msgs, images=[{"msg": 0, "name": "t.png", "b64": img}], route_text="make day wise todo of this",
                    planner=self.p, changes=changes)
        self.assertEqual([c["kind"] for c in changes], ["todo_added", "todo_added"])
        sent = [m for m in tool_llm.sent[0] if m.get("role") == "user"][-1]["content"]
        self.assertIn("SE test", sent)
        self.assertIn("23 Oct [Fri 23 Oct", sent)                 # Strux wrote the exact date next to it

    def test_vision_crash_is_explained(self):
        from watcher.brain import vision
        vision._read_cache.clear()
        llm = FakeLLM()
        llm.cfg = {"vision_model": "qwen2.5vl:3b"}
        tries = []

        def crash(messages, **kw):
            tries.append(kw.get("images"))
            llm.last_error = "HTTP Error 500: Internal Server Error"
            return None
        llm.chat = crash
        img = A.image_b64({"name": "t.png", "data": b64(png_bytes(1600, 900))})
        out = chat.answer(llm, None, [{"role": "user", "content": "?"}], images=[{"msg": 0, "name": "t.png", "b64": img}], route_text="?")
        self.assertEqual(len(tries), 2)                                        # retried once, smaller
        self.assertIn("crashed on this picture", out)

    def test_files_prompt_and_files_to_todos(self):
        llm = FakeLLM(text="It defines f().")
        msgs, _, _, _ = A.expand([{"role": "user", "content": "what does this do?", "attachments": [{"name": "a.py", "data": b64(b"def f(): pass")}]}])
        out = chat.answer(llm, None, msgs, route_text="what does this do?", has_files=True, planner=self.p)
        self.assertEqual(out, "It defines f().")
        self.assertIn("attached one or more files", llm.sent[0][0]["content"])
        # "make TODOs from this file" -> tools, with the file text in the history
        llm = FakeLLM(turns=[[{"name": "add_todo", "arguments": {"title": "Email Sam the draft"}}]])
        msgs, _, _, _ = A.expand([{"role": "user", "content": "add todos from these notes", "attachments": [{"name": "n.txt", "data": b64(b"- email Sam the draft")}]}])
        changes = []
        chat.answer(llm, None, msgs, route_text="add todos from these notes", has_files=True, planner=self.p, changes=changes)
        self.assertEqual([c["kind"] for c in changes], ["todo_added"])
        user_msgs = [m["content"] for m in llm.sent[0] if m.get("role") == "user"]
        self.assertIn("email Sam the draft", user_msgs[-1])                     # the model saw the file


if __name__ == "__main__":
    unittest.main()
