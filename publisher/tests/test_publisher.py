import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from publisher import affiliate
from publisher import main as main_mod
from publisher.charts import ChartError, as_text, embed, parse_spec
from publisher.note import NoteClient, NoteError, markdown_to_note_html
from publisher.parse import ParseError, parse
from publisher.thumbnail import make_thumbnail, split_title, wrap
from publisher.xpost import (
    XCredentials,
    XPostError,
    build_thread,
    oauth1_header,
    post_thread,
    split_to_fit,
    weighted_length,
)

FIXTURE = Path(__file__).with_name("fixture_weekend.md")


class ParseTest(unittest.TestCase):
    def test_real_weekend_output(self):
        p = parse(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(p.x_posts), 5)
        self.assertTrue(p.x_posts[0].startswith("【週末まとめ】"))
        self.assertTrue(p.x_posts[4].endswith("全文👇"))
        self.assertEqual(p.note_title, "【米国株】2026/09/27 今週の振り返りと週明けの展望")
        self.assertTrue(p.note_body.startswith("金は週足"))
        self.assertTrue(p.note_body.rstrip().endswith("ご自身の責任で行ってください。*"))

    def test_note_only(self):
        p = parse("# ② note記事\n## タイトル\nT\n## 本文\n本文\n")
        self.assertEqual((p.x_posts, p.note_title, p.note_body), ([], "T", "本文"))

    def test_x_only(self):
        p = parse("# ① X投稿スレッド\n### 1/2\nいち\n### 2/2\nに\n")
        self.assertEqual(p.x_posts, ["いち", "に"])
        self.assertEqual(p.note_title, "")

    def test_missing_sections(self):
        with self.assertRaises(ParseError):
            parse("# 何もない\n本文")

    def test_note_without_body(self):
        with self.assertRaises(ParseError):
            parse("# ② note記事\n## タイトル\nT\n")


class XTest(unittest.TestCase):
    def test_weighted_length(self):
        self.assertEqual(weighted_length("abc"), 3)
        self.assertEqual(weighted_length("あいう"), 6)
        self.assertEqual(weighted_length("見て https://note.com/a/n/" + "x" * 80), 4 + 1 + 23)
        self.assertEqual(weighted_length("📉"), 2)
        self.assertEqual(weighted_length("⚠️"), 2)

    def test_split_respects_limit_and_keeps_text(self):
        text = "。".join(["あ" * 50] * 8) + "。"
        chunks = split_to_fit(text, 280)
        self.assertTrue(all(weighted_length(c) <= 280 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_split_prefers_line_breaks(self):
        text = "\n".join(["い" * 100] * 3)
        chunks = split_to_fit(text, 280)
        self.assertEqual(chunks, ["い" * 100, "い" * 100, "い" * 100])

    def test_real_thread_fits(self):
        p = parse(FIXTURE.read_text(encoding="utf-8"))
        thread = build_thread(p.x_posts, link="https://note.com/u/n/nabc")
        self.assertTrue(all(weighted_length(t) <= 280 for t in thread))
        self.assertNotIn("**", "".join(thread))
        self.assertTrue(thread[-1].endswith("https://note.com/u/n/nabc"))
        self.assertEqual(sum("https://" in t for t in thread), 1)
        premium = build_thread(p.x_posts, premium=True)
        self.assertEqual(len(premium), 5)

    def test_oauth_signature_matches_oauthlib(self):
        try:
            from oauthlib.oauth1 import Client
        except ImportError:
            self.skipTest("oauthlib がないため比較できない")
        creds = XCredentials(
            "xvz1evFS4wEEPTGEFPHBog",
            "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
            "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
            "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        )
        nonce, ts = "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", "1318622958"
        ours = oauth1_header("POST", "https://api.x.com/2/tweets", creds, nonce=nonce, timestamp=ts)
        ref = Client(creds.api_key, creds.api_secret, creds.access_token, creds.access_secret,
                     nonce=nonce, timestamp=ts)
        _, headers, _ = ref.sign("https://api.x.com/2/tweets", "POST")

        def sig(h):
            import re
            return re.search(r'oauth_signature="([^"]+)"', h).group(1)

        self.assertEqual(sig(ours), sig(headers["Authorization"]))

    def test_post_thread_chains_replies(self):
        calls = []

        class Resp:
            def __init__(self, i):
                self.status_code, self._i, self.text = 201, i, ""

            def json(self):
                return {"data": {"id": str(100 + self._i)}}

        class Sess:
            def post(self, url, json, headers, timeout):
                calls.append(json)
                return Resp(len(calls))

        creds = XCredentials("a", "b", "c", "d")
        ids = post_thread(["1", "2", "3"], creds, session=Sess())
        self.assertEqual(ids, ["101", "102", "103"])
        self.assertNotIn("reply", calls[0])
        self.assertEqual(calls[1]["reply"], {"in_reply_to_tweet_id": "101"})
        self.assertEqual(calls[2]["reply"], {"in_reply_to_tweet_id": "102"})

    def test_post_thread_reports_partial(self):
        class Resp:
            def __init__(self, ok):
                self.status_code = 201 if ok else 429
                self.text = "Too Many Requests"

            def json(self):
                return {"data": {"id": "1"}}

        class Sess:
            n = 0

            def post(self, *a, **k):
                Sess.n += 1
                return Resp(Sess.n == 1)

        with self.assertRaises(XPostError) as cm:
            post_thread(["a", "b"], XCredentials("a", "b", "c", "d"), session=Sess())
        self.assertEqual(cm.exception.posted_ids, ["1"])


class FakeCurl:
    """curl の呼び出しを記録し、決められた応答を返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, input=None, **kw):
        self.calls.append((cmd, json.loads(input) if input else None))
        body, code = self.responses.pop(0)
        return subprocess.CompletedProcess(cmd, 0, f"{json.dumps(body)}\n{code}", "")


class NoteTest(unittest.TestCase):
    def test_markdown_conversion(self):
        out = markdown_to_note_html(
            "### 見出し\n\n**太字**と*斜体*\n続き\n\n- a\n- b\n\n---\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n<script>"
        )
        self.assertIn("<h3", out)
        self.assertIn("<b>太字</b>と斜体<br>続き", out)
        self.assertRegex(out, r"<ul[^>]*><li[^>]*><p[^>]*>a</p></li><li[^>]*><p[^>]*>b</p></li></ul>")
        self.assertIn("<hr", out)
        self.assertIn("x ／ y<br>1 ／ 2", out)
        self.assertIn("&lt;script&gt;", out)

    def test_real_article_converts(self):
        p = parse(FIXTURE.read_text(encoding="utf-8"))
        out = markdown_to_note_html(p.note_body)
        self.assertNotIn("**", out)
        self.assertIn("<h3", out)

    def test_draft_flow(self):
        fake = FakeCurl([({"data": {"id": 1, "key": "nabc"}}, 201), ({}, 200)])
        r = NoteClient("a=1; XSRF-TOKEN=tok", "ryota", runner=fake).create("T", "本文", publish=False)
        self.assertEqual((r.status, r.url), ("draft", None))
        self.assertEqual(len(fake.calls), 2)
        cmd, body = fake.calls[1]
        self.assertIn("https://note.com/api/v1/text_notes/draft_save?id=1&is_temp_saved=true", cmd)
        self.assertIn("X-XSRF-TOKEN: tok", cmd)
        self.assertEqual(body["name"], "T")

    def test_publish_flow(self):
        fake = FakeCurl([({"data": {"id": 1, "key": "nabc"}}, 201), ({}, 200), ({}, 200)])
        r = NoteClient("a=1", "ryota", runner=fake).create("T", "本文", publish=True)
        self.assertEqual(r.url, "https://note.com/ryota/n/nabc")
        cmd, body = fake.calls[2]
        self.assertIn("PUT", cmd)
        self.assertEqual(body["status"], "published")

    def test_login_with_email_and_password(self):
        calls = []

        def run(cmd, input=None, **kw):
            calls.append((cmd, json.loads(input) if input else None))
            if len(calls) == 1:
                out = ("HTTP/2 201\r\nset-cookie: _note_session_v5=abc; path=/; HttpOnly\r\n"
                       "Set-Cookie: XSRF-TOKEN=tok; path=/\r\n\r\n"
                       + json.dumps({"data": {"urlname": "ryota"}}) + "\n201")
            elif len(calls) == 2:
                out = json.dumps({"data": {"id": 1, "key": "nabc"}}) + "\n201"
            else:
                out = "{}\n200"
            return subprocess.CompletedProcess(cmd, 0, out, "")

        client = NoteClient.login("me@example.com", "pw", runner=run)
        self.assertEqual(client.cookie, "_note_session_v5=abc; XSRF-TOKEN=tok")
        self.assertEqual(client.urlname, "ryota")
        login_cmd, login_body = calls[0]
        self.assertEqual(login_body, {"login": "me@example.com", "password": "pw"})
        self.assertNotIn("pw", " ".join(login_cmd))  # パスワードはコマンドラインに出さない
        r = client.create("T", "本文", publish=True)
        self.assertEqual(r.url, "https://note.com/ryota/n/nabc")
        self.assertIn("X-XSRF-TOKEN: tok", calls[1][0])

    def test_login_failure(self):
        fake = FakeCurl([({"error": "invalid"}, 401)])
        with self.assertRaisesRegex(NoteError, "ログインに失敗"):
            NoteClient.login("me@example.com", "bad", runner=fake)

    def test_cookie_header_prefix_is_stripped(self):
        self.assertEqual(NoteClient("cookie: a=1; b=2").cookie, "a=1; b=2")

    def test_only_long_lived_cookies_are_sent(self):
        raw = ("cookie: note_web_visitor_id=v; fp=f; _vid_v1=x; _note_session_v5=SESS; "
               "note_gql_auth_token=SHORT; XSRF-TOKEN=tok")
        self.assertEqual(NoteClient(raw).cookie, "_note_session_v5=SESS; XSRF-TOKEN=tok")

    def test_bare_session_value_is_accepted(self):
        self.assertEqual(NoteClient("abc123").cookie, "_note_session_v5=abc123")

    def test_not_login_reports_cookie_names_only(self):
        fake = FakeCurl([({"error": {"code": "auth", "message": "not_login"}}, 200),
                         ({"data": {"urlname": "ryota-secret"}}, 200)])
        with self.assertRaises(NoteError) as cm:
            NoteClient("a=secret1; _note_session_v5=secret2", runner=fake).create("T", "本文", publish=False)
        self.assertIn("渡した Cookie の名前: _note_session_v5", str(cm.exception))
        self.assertIn("ログイン確認: HTTP 200 keys=['data'] data=['urlname']", str(cm.exception))
        self.assertIn("https://note.com/api/v2/current_user", fake.calls[1][0])
        self.assertNotIn("secret", str(cm.exception))

    def test_logged_in(self):
        self.assertTrue(NoteClient("x", runner=FakeCurl([({"data": {"urlname": "r"}}, 200)])).logged_in())
        self.assertFalse(NoteClient("x", runner=FakeCurl([({"data": {}}, 401)])).logged_in())

    def test_auth_error_has_hint(self):
        fake = FakeCurl([({"error": "x"}, 403)])
        with self.assertRaisesRegex(NoteError, "ログインの期限切れ"):
            NoteClient("a=1", runner=fake).create("T", "本文", publish=False)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("publish/inbox").mkdir(parents=True)
        self.file = Path("publish/inbox/2026-09-27_weekend.md")
        self.file.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_dry_run_posts_nothing(self):
        with mock.patch.dict(os.environ, {"X_API_KEY": "a", "X_API_SECRET": "b", "X_ACCESS_TOKEN": "c",
                                          "X_ACCESS_SECRET": "d", "NOTE_COOKIE": "x"}), \
                mock.patch("publisher.main.post_thread") as pt, mock.patch("publisher.main.NoteClient") as nc:
            self.assertEqual(main_mod.main([str(self.file), "--dry-run"]), 0)
        pt.assert_not_called()
        nc.assert_not_called()
        self.assertFalse(Path("publish/done").exists())

    def test_live_run_records_and_is_idempotent(self):
        env = {"X_API_KEY": "a", "X_API_SECRET": "b", "X_ACCESS_TOKEN": "c", "X_ACCESS_SECRET": "d",
               "NOTE_COOKIE": "x", "NOTE_MODE": "publish"}
        from publisher.note import NoteResult

        with mock.patch.dict(os.environ, env), \
                mock.patch("publisher.main.post_thread", return_value=["1", "2"]) as pt, \
                mock.patch("publisher.main.NoteClient") as nc:
            nc.return_value.create.return_value = NoteResult("1", "nabc", "published", "https://note.com/u/n/nabc")
            self.assertEqual(main_mod.main([str(self.file)]), 0)
            thread = pt.call_args[0][0]
            self.assertTrue(thread[-1].endswith("https://note.com/u/n/nabc"))
            # 2 回目は何も投稿しない
            self.assertEqual(main_mod.main([str(self.file)]), 0)
            self.assertEqual(pt.call_count, 1)
            self.assertEqual(nc.return_value.create.call_count, 1)
        saved = json.loads(Path("publish/done/2026-09-27_weekend.json").read_text())
        self.assertEqual(saved["x"]["ids"], ["1", "2"])
        self.assertEqual(saved["note"]["status"], "published")

    def test_draft_mode_has_no_link(self):
        from publisher.note import NoteResult

        env = {"X_API_KEY": "a", "X_API_SECRET": "b", "X_ACCESS_TOKEN": "c", "X_ACCESS_SECRET": "d",
               "NOTE_COOKIE": "x", "NOTE_MODE": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch("publisher.main.post_thread", return_value=["1"]) as pt, \
                mock.patch("publisher.main.NoteClient") as nc:
            nc.return_value.create.return_value = NoteResult("1", "nabc", "draft", None)
            main_mod.main([str(self.file)])
        self.assertFalse(any("https://" in t for t in pt.call_args[0][0]))

    def test_note_failure_still_posts_x_and_fails_job(self):
        env = {"X_API_KEY": "a", "X_API_SECRET": "b", "X_ACCESS_TOKEN": "c", "X_ACCESS_SECRET": "d",
               "NOTE_COOKIE": "x"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("publisher.main.post_thread", return_value=["1"]) as pt, \
                mock.patch("publisher.main.NoteClient") as nc:
            nc.return_value.create.side_effect = NoteError("403")
            self.assertEqual(main_mod.main([str(self.file)]), 1)
        pt.assert_called_once()

    def test_drafttest_file_creates_draft_only(self):
        from publisher.note import NoteResult

        f = Path("publish/inbox/2026-10-03_drafttest.md")
        f.write_text(self.file.read_text(encoding="utf-8"), encoding="utf-8")
        env = {"X_API_KEY": "a", "X_API_SECRET": "b", "X_ACCESS_TOKEN": "c", "X_ACCESS_SECRET": "d",
               "NOTE_EMAIL": "me@example.com", "NOTE_PASSWORD": "pw", "NOTE_MODE": "publish"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("publisher.main.post_thread") as pt, mock.patch("publisher.main.NoteClient") as nc:
            nc.login.return_value.create.return_value = NoteResult("1", "nabc", "draft", None)
            self.assertEqual(main_mod.main([str(f)]), 0)
        pt.assert_not_called()
        self.assertIs(nc.login.return_value.create.call_args.kwargs["publish"], False)

    def test_cookie_is_preferred_over_password(self):
        from publisher.note import NoteResult

        env = {"NOTE_COOKIE": "_note_session_v5=abc", "NOTE_EMAIL": "me@example.com", "NOTE_PASSWORD": "pw"}
        with mock.patch.dict(os.environ, env), mock.patch("publisher.main.NoteClient") as nc:
            nc.return_value.create.return_value = NoteResult("1", "nabc", "draft", None)
            main_mod.main([str(self.file)])
        nc.login.assert_not_called()
        nc.assert_called_once_with("_note_session_v5=abc", None)

    def test_test_file_is_always_dry_run(self):
        f = Path("publish/inbox/2026-10-03_test.md")
        f.write_text(self.file.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch("publisher.main.post_thread") as pt:
            self.assertEqual(main_mod.main([str(f)]), 0)
        pt.assert_not_called()


class GmailExtractTest(unittest.TestCase):
    ADDR = "me@gmail.com"

    def mail(self, *, subject="[PUBLISH] 2026-10-04_weekend", sender=ADDR, token="secret-token",
             body=None, html=False):
        from email.message import EmailMessage

        content = FIXTURE.read_text(encoding="utf-8")
        text = body if body is not None else f"TOKEN: {token}\n-----BEGIN-----\n{content}\n-----END-----\n"
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, f"Ryota <{sender}>", self.ADDR
        if html:
            msg.set_content("<div>" + text.replace("\n", "<br>") + "</div>", subtype="html")
        else:
            msg.set_content(text)
        return msg.as_bytes()

    def extract(self, raw):
        from publisher.gmail_inbox import extract
        return extract(raw, sender=self.ADDR, token="secret-token")

    def test_valid_mail_roundtrips_exactly(self):
        stem, content = self.extract(self.mail())
        self.assertEqual(stem, "2026-10-04_weekend")
        self.assertEqual(content, FIXTURE.read_text(encoding="utf-8").strip() + "\n")
        self.assertEqual(len(parse(content).x_posts), 5)

    def test_rejects_wrong_sender_token_subject(self):
        from publisher.gmail_inbox import Rejected

        for raw in (self.mail(sender="evil@example.com"), self.mail(token="nope"),
                    self.mail(subject="[PUBLISH] 2026-10-04_other"), self.mail(subject="Re: hi"),
                    self.mail(body="TOKEN: secret-token\n本文だけ")):
            with self.assertRaises(Rejected):
                self.extract(raw)

    def test_fetch_and_mark_with_fake_imap(self):
        from publisher import gmail_inbox

        good, bad = self.mail(), self.mail(token="nope")
        stored = []

        class FakeImap:
            def __init__(self, host):
                pass

            def login(self, a, p):
                assert (a, p) == ("inbox@gmail.com", "abcdabcdabcdabcd")

            def select(self, box):
                pass

            def uid(self, cmd, *args):
                if cmd == "SEARCH":
                    assert "-label:published" in args[1] and f"from:{GmailExtractTest.ADDR}" in args[1]
                    # 全体を囲む " 以外に " が入ると Gmail が「Could not parse command」を返す
                    assert args[1].startswith('"') and args[1].endswith('"') and '"' not in args[1][1:-1]
                    return "OK", [b"7 8"]
                if cmd == "FETCH":
                    return "OK", [(b"x", good if args[0] == b"7" else bad), b")"]
                if cmd == "STORE":
                    stored.append(args[0])
                    return "OK", []

            def logout(self):
                pass

        env = {"GMAIL_ADDRESS": "inbox@gmail.com", "PUBLISH_SENDER": self.ADDR,
               "GMAIL_APP_PASSWORD": "abcd abcd abcd abcd", "PUBLISH_TOKEN": "secret-token"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
                mock.patch.object(gmail_inbox.imaplib, "IMAP4_SSL", FakeImap):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                saved = gmail_inbox.fetch()
                self.assertEqual(saved, ["publish/inbox/2026-10-04_weekend.md"])
                self.assertEqual(gmail_inbox.fetch(), [])  # 2回目は保存済み
                gmail_inbox.mark()
            finally:
                os.chdir(cwd)
        self.assertEqual(stored, ["7"])  # なりすましメール(8)にはラベルを付けない

    def test_real_mail_sent_by_gmail_connector(self):
        # Cowork と同じ Gmail コネクタ（send_message）で実際に送ったメールの生データ
        raw = Path(__file__).with_name("fixture_gmail_raw.eml").read_bytes()
        stem, content = self.extract(raw)
        self.assertEqual(stem, "2026-10-03_test")
        p = parse(content)
        self.assertEqual(build_thread(p.x_posts), ["受け渡しテストです📉 太字も入れておきます。", "これは2本目。#テスト"])
        self.assertEqual(p.note_title, "【テスト】受け渡し確認")

    def test_flowed_soft_breaks_are_joined(self):
        # Gmail は長い行を「行末に空白を残して改行」する format=flowed で届ける（2026-10-04 の週末版で発生）
        import quopri as qp

        text = ("TOKEN: secret-token\n-----BEGIN-----\n# ② note記事\n## タイトル\nT\n## 本文\n"
                "ところが中を開けると、情報技術が \n+1.80%、ヘルスケアが \n−2.65%。\n\n"
                '```chart\n{"title": "騰落率", \n"source": "S&P"}\n```\n-----END-----\n')
        encoded = qp.encodestring(text.replace("\n", "\r\n").encode()).decode()
        # 行末の空白は literal でも =20 でも来うる
        encoded = encoded.replace("=E3=81=8C=20\r\n", "=E3=81=8C \r\n", 1)
        raw = ("Subject: [PUBLISH] 2026-10-04_weekend\r\nFrom: me@gmail.com\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/plain; charset=UTF-8; format=flowed\r\n"
               "Content-Transfer-Encoding: quoted-printable\r\n\r\n" + encoded).encode()
        _, content = self.extract(raw)
        self.assertIn("情報技術が +1.80%、ヘルスケアが −2.65%。\n", content)
        self.assertIn('{"title": "騰落率", "source": "S&P"}', content)

    def test_gmail_hard_wraps_use_html_line_breaks(self):
        # 2026-10-04 の実メール: 受け取ったテキスト版は約75字で空白の位置に改行が入っていた（HTML 版は元のまま）
        from email.message import EmailMessage

        body = ("TOKEN: secret-token\n-----BEGIN-----\n# ② note記事\n## タイトル\nT\n## 本文\n"
                "ところが中を開けると、情報技術が +1.80%、ヘルスケアが −2.65% で、**開き**ができていました。\n"
                "金：R1 が生命線**\nBTC：週足は上昇 & 収縮\n-----END-----")
        wrapped = body.replace("情報技術が +1.80%", "情報技術が\n+1.80%")
        rich = "<div dir=\"auto\">" + body.replace("&", "&amp;").replace("\n", "<br/>\n") + "</div>"

        def raw(plain, html_body):
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = "[PUBLISH] 2026-10-04_weekend", self.ADDR, self.ADDR
            msg.set_content(plain)
            msg.add_alternative(html_body, subtype="html")
            return msg.as_bytes()

        _, content = self.extract(raw(wrapped, rich))
        self.assertIn("情報技術が +1.80%、ヘルスケアが −2.65% で、**開き**ができていました。\n", content)
        self.assertIn("生命線**\nBTC：週足は上昇 & 収縮", content)  # 本物の改行は残る
        # HTML 版の中身が違うときはテキスト版をそのまま使う
        _, content = self.extract(raw(wrapped, rich.replace("収縮", "拡大")))
        self.assertIn("情報技術が\n+1.80%", content)

    def test_indented_body(self):
        content = FIXTURE.read_text(encoding="utf-8")
        indented = "\n".join("    " + l if l else l for l in content.split("\n"))
        body = f"    TOKEN: secret-token\n    -----BEGIN-----\n{indented}\n    -----END-----\n"
        stem, got = self.extract(self.mail(body=body))
        self.assertEqual(got, content.strip() + "\n")

    def test_html_only_mail(self):
        stem, content = self.extract(self.mail(html=True))
        self.assertEqual(len(parse(content).x_posts), 5)


BAR = {"type": "bar", "title": "セクター別騰落率", "unit": "%", "labels": ["情報技術", "ヘルスケア"],
       "values": [1.8, -2.65], "source": "S&P Dow Jones Indices"}


def chart_block(spec) -> str:
    return "```chart\n" + json.dumps(spec, ensure_ascii=False) + "\n```"


class AffiliateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "affiliate.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_shipped_file_adds_nothing(self):
        # リポジトリに置いてある雛形はコメントだけなので、記事は変わらない
        self.assertEqual(affiliate.apply("本文"), "本文")

    def test_comment_only_or_missing_adds_nothing(self):
        self.assertEqual(affiliate.apply("本文", self.path), "本文")
        self.path.write_text("<!--\n- [例](https://example.com)\n-->\n", encoding="utf-8")
        self.assertEqual(affiliate.apply("本文", self.path), "本文")

    def test_footer_comes_with_disclosure(self):
        self.path.write_text("<!-- メモ -->\n- [口座を開く](https://px.a8.net/svt/ejp?a8mat=X&b=1)\n", encoding="utf-8")
        body = affiliate.apply("本文\n", self.path)
        self.assertTrue(body.startswith(affiliate.DISCLOSURE))
        self.assertTrue(body.rstrip().endswith("- [口座を開く](https://px.a8.net/svt/ejp?a8mat=X&b=1)"))
        self.assertNotIn("メモ", body)
        html_ = markdown_to_note_html(body)
        self.assertIn('<a href="https://px.a8.net/svt/ejp?a8mat=X&amp;b=1" target="_blank" '
                      'rel="nofollow noopener noreferrer">口座を開く</a>', html_)
        self.assertIn("プロモーション", html_)

    def test_link_cannot_break_out_of_attribute(self):
        html_ = markdown_to_note_html('[x](https://e.com/"onclick=alert(1))')
        self.assertNotIn('"onclick', html_)

    def test_main_applies_footer(self):
        from publisher.note import NoteResult

        self.path.write_text("[広告](https://example.com)", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            src = Path("publish/inbox/2026-09-27_weekend.md")
            src.parent.mkdir(parents=True)
            src.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch.object(affiliate, "FOOTER", self.path), \
                    mock.patch.dict(os.environ, {"NOTE_COOKIE": "x"}), \
                    mock.patch("publisher.main.make_thumbnail", return_value=None), \
                    mock.patch("publisher.main.NoteClient") as nc:
                nc.return_value.create.return_value = NoteResult("1", "nabc", "draft", "")
                self.assertEqual(main_mod.main([str(src)]), 0)
        finally:
            os.chdir(cwd)
        body = nc.return_value.create.call_args[0][1]
        self.assertTrue(body.startswith(affiliate.DISCLOSURE))
        self.assertTrue(body.endswith("[広告](https://example.com)\n"))


class ThumbnailTest(unittest.TestCase):
    def test_split_title(self):
        self.assertEqual(split_title("【米国株】2026/10/04 今週の振り返りと週明けの展望"),
                         ("米国株", "2026/10/04", "今週の振り返りと週明けの展望"))
        self.assertEqual(split_title("ただの題名"), ("", "", "ただの題名"))

    def test_wrap_keeps_text_and_avoids_punctuation_at_line_start(self):
        from PIL import ImageFont
        from publisher.thumbnail import find_font

        font = ImageFont.truetype(find_font()[0], 72, index=find_font()[1])
        text = "雇用統計ショックで金利急騰、ハイテク株に試練の一日"
        lines = wrap(text, font, 600)
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(font.getlength(l) <= 600 for l in lines))
        self.assertFalse(any(l[0] in "、。" for l in lines))

    def test_make_thumbnail_size(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            out = make_thumbnail("【米国株】2026/10/04 今週の振り返りと週明けの展望", Path(d) / "e.png")
            with Image.open(out) as im:
                self.assertEqual(im.size, (1280, 670))


class ChartTest(unittest.TestCase):
    def test_parse_spec_validates(self):
        self.assertEqual(parse_spec(json.dumps(BAR)).labels, ["情報技術", "ヘルスケア"])
        for broken, msg in (({**BAR, "source": ""}, "出典"), ({**BAR, "values": [1]}, "数が合いません"),
                            ({**BAR, "type": "pie"}, "type"), ({**BAR, "values": ["x", 1]}, "数値")):
            with self.assertRaisesRegex(ChartError, msg):
                parse_spec(json.dumps(broken))
        with self.assertRaisesRegex(ChartError, "JSON"):
            parse_spec("{")

    def test_text_fallback_keeps_numbers_and_source(self):
        text = as_text(parse_spec(json.dumps(BAR)))
        self.assertIn("- 情報技術: +1.8%", text)
        self.assertIn("- ヘルスケア: -2.65%", text)
        self.assertIn("出典: S&P Dow Jones Indices", text)

    def test_embed_uploads_and_inserts_image(self):
        body = "前置き\n\n" + chart_block(BAR) + "\n\n続き\n\n" + chart_block(
            {"type": "line", "title": "米10年債利回り", "unit": "%", "labels": ["10/1", "10/2"],
             "series": [{"name": "米10年", "values": [3.9, 4.0]}], "source": "米財務省"})
        uploaded = []
        with tempfile.TemporaryDirectory() as d:
            rep = embed(body, Path(d), lambda p: uploaded.append(p) or f"https://assets.st-note.com/{p.name}")
            self.assertTrue(all(p.exists() for p in uploaded))
        self.assertEqual([p.name for p in uploaded], ["chart_1.png", "chart_2.png"])
        self.assertIn('![セクター別騰落率（出典: S&P Dow Jones Indices）](https://assets.st-note.com/chart_1.png '
                      '"620x380")', rep.body)
        self.assertNotIn("```", rep.body)
        self.assertEqual(rep.warnings, [])
        html_out = markdown_to_note_html(rep.body)
        self.assertRegex(html_out, r'<figure name="[^"]+" id="[^"]+"><img src="https://assets.st-note.com/chart_1.png"'
                                   r' alt="" width="620" height="380"[^>]*><figcaption>セクター別騰落率'
                                   r'（出典: S&amp;P Dow Jones Indices）</figcaption></figure>')

    def test_embed_falls_back_to_text(self):
        def fail(_):
            raise NoteError("HTTP 500")

        with tempfile.TemporaryDirectory() as d:
            rep = embed("a\n\n" + chart_block(BAR) + "\n\n" + chart_block({"title": "x"}), Path(d), fail)
            preview = embed(chart_block(BAR), Path(d), None)
            self.assertTrue((Path(d) / "chart_1.png").exists())
        self.assertIn("出典: S&P Dow Jones Indices", rep.body)
        self.assertNotIn("```", rep.body)
        self.assertEqual(len(rep.warnings), 2)
        self.assertIn("アップロードに失敗", rep.warnings[0])
        self.assertIn("書式が正しくない", rep.warnings[1])
        self.assertIn("- 情報技術: +1.8%", preview.body)


class NoteImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = make_thumbnail("【米国株】2026/10/04 テスト", Path(self.tmp.name) / "eyecatch.png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_with_eyecatch(self):
        fake = FakeCurl([({"data": {"id": 1, "key": "nabc"}}, 201), ({}, 200),
                         ({"data": {"url": "https://assets.st-note.com/e.png"}}, 201)])
        r = NoteClient("_note_session_v5=s; XSRF-TOKEN=a%2Bb", runner=fake).create(
            "T", "本文", publish=False, eyecatch=self.img)
        self.assertEqual((r.status, r.eyecatch, r.warnings), ("draft", "https://assets.st-note.com/e.png", []))
        cmd, _ = fake.calls[2]
        self.assertIn("https://note.com/api/v1/image_upload/note_eyecatch", cmd)
        for part in ("note_id=1", "width=1280", "height=670", "X-XSRF-TOKEN: a+b", "Origin: https://editor.note.com"):
            self.assertIn(part, cmd)
        self.assertIn(f'file=@"{self.img}";type=image/png;filename=eyecatch.png', cmd)
        self.assertFalse(any("application/json" in c and c.startswith("Content-Type") for c in cmd))

    def test_eyecatch_failure_keeps_draft(self):
        fake = FakeCurl([({"data": {"id": 1, "key": "nabc"}}, 201), ({}, 200), ({"error": "x"}, 500)])
        r = NoteClient("_note_session_v5=s; XSRF-TOKEN=t", runner=fake).create(
            "T", "本文", publish=False, eyecatch=self.img)
        self.assertEqual(r.status, "draft")
        self.assertIsNone(r.eyecatch)
        self.assertIn("見出し画像を付けられませんでした", r.warnings[0])

    def test_xsrf_is_fetched_when_missing(self):
        calls = []

        def run(cmd, input=None, **kw):
            calls.append(cmd)
            if "-D" in cmd:
                out = "HTTP/2 200\r\nset-cookie: XSRF-TOKEN=fresh%3D; path=/\r\n\r\n{}\n200"
            else:
                out = json.dumps({"data": {"url": "https://assets.st-note.com/e.png"}}) + "\n201"
            return subprocess.CompletedProcess(cmd, 0, out, "")

        NoteClient("abc", runner=run).upload_eyecatch("1", self.img)
        self.assertIn("X-XSRF-TOKEN: fresh=", calls[1])

    def test_body_image_presigned_upload(self):
        fake = FakeCurl([
            ({"data": {"action": "https://bucket.s3.amazonaws.com/", "url": "https://assets.st-note.com/c.png",
                       "post": {"key": "img/c.png", "policy": "@p"}}}, 200),
            ("", 204),
        ])
        url = NoteClient("_note_session_v5=s; XSRF-TOKEN=t", runner=fake).upload_body_image(self.img)
        self.assertEqual(url, "https://assets.st-note.com/c.png")
        presign, s3 = fake.calls[0][0], fake.calls[1][0]
        self.assertIn("https://note.com/api/v3/images/upload/presigned_post", presign)
        self.assertIn("filename=eyecatch.png", presign)
        self.assertFalse(any(c.startswith("file=@") for c in presign))
        self.assertIn("https://bucket.s3.amazonaws.com/", s3)
        self.assertFalse(any(c.startswith("Cookie") for c in s3))  # S3 にはログイン情報を送らない
        self.assertIn("policy=@p", s3)
        self.assertTrue(s3[-1].startswith("file=@"))  # ファイルは最後

    def test_live_run_attaches_thumbnail_and_charts(self):
        from publisher.note import NoteResult

        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            Path("publish/inbox").mkdir(parents=True)
            f = Path("publish/inbox/2026-10-05_weekday.md")
            f.write_text("# ② note記事\n## タイトル\n【米国株】2026/10/05 引け後の振り返りと明日の展望\n## 本文\n"
                         "本文\n\n" + chart_block(BAR) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NOTE_COOKIE": "x"}), \
                    mock.patch("publisher.main.NoteClient") as nc:
                nc.return_value.upload_body_image.return_value = "https://assets.st-note.com/c.png"
                nc.return_value.create.return_value = NoteResult("1", "nabc", "draft", None,
                                                                 "https://assets.st-note.com/e.png")
                self.assertEqual(main_mod.main([str(f)]), 0)
            args, kwargs = nc.return_value.create.call_args
            self.assertIn("![セクター別騰落率", args[1])
            self.assertEqual(kwargs["eyecatch"], Path("publish/media/2026-10-05_weekday/eyecatch.png"))
            self.assertTrue(kwargs["eyecatch"].exists())
            saved = json.loads(Path("publish/done/2026-10-05_weekday.json").read_text())
            self.assertEqual((saved["note"]["eyecatch"], saved["note"]["charts"]), (True, 1))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
