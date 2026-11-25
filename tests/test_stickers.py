# -*- coding: utf-8 -*-
"""python -m unittest discover tests —— 只用标准库，数据写在临时目录里。"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import labeler  # noqa: E402
import server  # noqa: E402
import stickers  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16


class TempData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        stickers.DATA_DIR, stickers.IMAGE_DIR, stickers.INDEX_PATH = base, base / "images", base / "stickers.json"

    def tearDown(self):
        self.tmp.cleanup()


class StoreTests(TempData):
    def test_formats_and_limits(self):
        self.assertEqual(stickers.sniff_image(GIF), ("gif", "image/gif"))
        self.assertEqual(stickers.sniff_image(WEBP), ("webp", "image/webp"))
        self.assertIsNone(stickers.sniff_image(b"hello"))
        with self.assertRaises(stickers.StickerError):
            stickers.add_sticker(b"not an image")
        with self.assertRaises(stickers.StickerError):
            stickers.add_sticker(PNG + b"\x00" * stickers.MAX_BYTES)

    def test_add_update_delete(self):
        item = stickers.add_sticker(GIF, "  开心   转圈 ", "  高兴时   原地转圈庆祝 ")
        self.assertRegex(item["id"], r"^u-[a-f0-9]{10}$")
        self.assertEqual(item["label"], "开心 转圈")
        self.assertEqual(item["description"], "高兴时 原地转圈庆祝")
        self.assertTrue((stickers.IMAGE_DIR / f"{item['id']}.gif").read_bytes() == GIF)  # 原样保存
        self.assertEqual(stickers.add_sticker(PNG)["label"], "未命名表情")
        updated = stickers.update_sticker(item["id"], label="委屈", description="想被哄时缩成一团", ai_enabled=False)
        self.assertEqual(
            (updated["label"], updated["description"], updated["ai_enabled"]),
            ("委屈", "想被哄时缩成一团", False),
        )
        stickers.delete_sticker(item["id"])
        self.assertIsNone(stickers.find(item["id"]))
        self.assertFalse((stickers.IMAGE_DIR / f"{item['id']}.gif").exists())
        with self.assertRaises(stickers.StickerError):
            stickers.delete_sticker(item["id"])

    def test_old_index_without_description_still_loads(self):
        item = stickers.add_sticker(PNG, "旧表情")
        stored = json.loads(stickers.INDEX_PATH.read_text(encoding="utf-8"))
        stored[0].pop("description")
        stickers.INDEX_PATH.write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(stickers.list_stickers()[0]["description"], "")
        self.assertEqual(stickers.find(item["id"])["label"], "旧表情")


class TextTests(TempData):
    def setUp(self):
        super().setUp()
        self.hug = stickers.add_sticker(PNG, "抱抱")["id"]
        self.hidden = stickers.add_sticker(PNG, "翻白眼")["id"]
        stickers.update_sticker(self.hidden, ai_enabled=False)

    def test_prompt_lists_only_enabled(self):
        context = stickers.prompt_context()
        self.assertIn(f"{self.hug}＝抱抱", context)
        self.assertNotIn(self.hidden, context)

    def test_description_is_the_context_index(self):
        comfort = stickers.add_sticker(PNG, "抱紧", "对方委屈或关系不安时，主动靠近安慰")["id"]
        context = stickers.prompt_context()
        self.assertIn(f"{comfort}＝抱紧；含义/使用时机：对方委屈或关系不安时，主动靠近安慰", context)
        self.assertEqual(
            stickers.expand_for_ai(f"[sticker:{comfort}]"),
            "【表情包：抱紧；含义/使用时机：对方委屈或关系不安时，主动靠近安慰】",
        )

    def test_expand_for_ai(self):
        self.assertEqual(stickers.expand_for_ai(f"[sticker:{self.hug}]"), "【表情包：抱抱】")
        self.assertEqual(stickers.expand_for_ai(f"好\n\n[sticker:{self.hug}]", "assistant"), f"好\n\n[sticker:{self.hug}]")
        self.assertEqual(stickers.expand_for_ai("[sticker:u-0000000000]"), "【一张已删除的表情包】")
        self.assertEqual(stickers.expand_for_ai("普通的一句话"), "普通的一句话")

    def test_normalise_reply(self):
        self.assertEqual(stickers.normalise_reply("来。\n\n【表情包：抱抱】"), f"来。\n\n[sticker:{self.hug}]")
        self.assertEqual(stickers.normalise_reply(f"[sticker:{self.hug}]（抱抱）"), f"[sticker:{self.hug}]")
        self.assertEqual(stickers.normalise_reply("【表情包：不存在的】"), "【表情包：不存在的】")
        # 关掉了的表情包，小机写成文字也不会被换成真的
        self.assertEqual(stickers.normalise_reply("【表情包：翻白眼】"), "【表情包：翻白眼】")


class ServerTests(TempData):
    def setUp(self):
        super().setUp()
        self._available = labeler.available
        labeler.available = lambda: False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        labeler.available = self._available
        super().tearDown()

    def call(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), response.headers

    def test_round_trip(self):
        status, body, _ = self.call("/stickers/api/upload", GIF, {
            "X-Label": "%E8%BD%AC%E5%9C%88",
            "X-Description": "%E5%BC%80%E5%BF%83%E6%97%B6%E8%BD%AC%E5%9C%88%E5%BA%86%E7%A5%9D",
        })
        item = json.loads(body)
        self.assertEqual((status, item["label"], item["auto_labelled"]), (201, "转圈", False))
        self.assertEqual(item["description"], "开心时转圈庆祝")
        status, body, headers = self.call(item["url"])
        self.assertEqual((body, headers["Content-Type"]), (GIF, "image/gif"))
        _, body, headers = self.call("/stickers/context")
        self.assertIn(f"{item['id']}＝转圈；含义/使用时机：开心时转圈庆祝", body.decode("utf-8"))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        _, body, _ = self.call("/stickers/expand", json.dumps({"text": f"[sticker:{item['id']}]"}).encode())
        self.assertEqual(json.loads(body)["text"], "【表情包：转圈；含义/使用时机：开心时转圈庆祝】")
        _, body, _ = self.call("/stickers/normalize", json.dumps({"text": "【表情包：转圈】"}).encode())
        self.assertEqual(json.loads(body)["text"], f"[sticker:{item['id']}]")
        _, body, _ = self.call("/stickers/api/list")
        self.assertEqual(len(json.loads(body)["items"]), 1)
        self.call("/stickers/api/delete", json.dumps({"id": item["id"]}).encode())
        _, body, _ = self.call("/stickers/api/list")
        self.assertEqual(json.loads(body)["items"], [])

    def test_other_sites_cannot_change_data(self):
        with self.assertRaises(HTTPError) as caught:
            self.call("/stickers/api/upload", PNG, {"Origin": "https://evil.example"})
        self.assertEqual(caught.exception.code, 403)

    def test_bad_upload(self):
        with self.assertRaises(HTTPError) as caught:
            self.call("/stickers/api/upload", b"hello")
        self.assertEqual(caught.exception.code, 422)
        self.assertIn("只支持", json.loads(caught.exception.read())["error"])

    def test_missing_static_file_returns_clean_404(self):
        with self.assertRaises(HTTPError) as caught:
            self.call("/favicon.ico")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
