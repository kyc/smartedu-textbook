"""离线冒烟测试：不联网、不写用户缓存，验证 CLI 骨架与关键解析逻辑。

CI 与本地都能跑：python3 -m unittest discover -s tests -v
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "download_smartedu.py"


class OfflineCase(unittest.TestCase):
    """把缓存目录指到临时目录，避免污染真实 HOME 或只读环境。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["SMARTEDU_CACHE_DIR"] = cls._tmp.name
        sys.path.insert(0, str(SCRIPT.parent))
        import download_smartedu  # noqa: E402
        cls.m = download_smartedu

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SMARTEDU_CACHE_DIR", None)
        sys.path.remove(str(SCRIPT.parent)) if str(SCRIPT.parent) in sys.path else None
        cls._tmp.cleanup()


class TestCli(OfflineCase):
    def run_cli(self, argv, env_extra=None):
        """在子进程里跑真实入口，捕获输出与退出码（离线：只测不联网的分支）。"""
        env = {k: v for k, v in os.environ.items() if k != "UC_TOKEN"}
        env.update(env_extra or {})
        env["SMARTEDU_CACHE_DIR"] = self._tmp.name
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            capture_output=True, text=True, env=env, timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_help_lists_all_commands(self):
        code, out, err = self.run_cli(["--help"])
        self.assertEqual(code, 0, err)
        for cmd in ("search", "download", "audio", "doctor"):
            self.assertIn(cmd, out)
        code, out, _ = self.run_cli(["download", "--help"])
        self.assertIn("--token-from-shell", out)
        code, out, _ = self.run_cli(["doctor", "--help"])
        self.assertIn("--verify", out)

    def test_help_survives_non_utf8_console(self):
        """Windows 控制台常见 cp936/cp1252，重定向到管道时历史上会 UnicodeEncodeError。"""
        for encoding in ("ascii", "cp1252", "cp936"):
            with self.subTest(encoding=encoding):
                code, out, err = self.run_cli(["--help"], env_extra={"PYTHONIOENCODING": encoding})
                self.assertEqual(code, 0, err)
                self.assertIn("usage", out)

    def test_each_subcommand_has_help(self):
        for cmd in ("search", "download", "audio", "doctor"):
            with self.subTest(cmd=cmd):
                code, out, err = self.run_cli([cmd, "--help"])
                self.assertEqual(code, 0, err)
                self.assertIn("usage", out)


class TestTokenResolutionMessages(OfflineCase):
    """直接构造 args 调 cmd_*，不联网、不依赖平台当前的鉴权策略。"""

    def make_args(self, token=None, from_shell=False):
        import argparse
        return argparse.Namespace(
            id="x", output=os.path.join(self._tmp.name, "x.pdf"),
            token=token, token_from_shell=from_shell, mode="vector",
        )

    def test_vector_without_token_lists_all_four_options(self):
        with mock.patch.object(self.m, "get_book_details", return_value={"id": "x", "title": "t"}), \
             mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.m.SmartEduError) as ctx:
                self.m.cmd_download(self.make_args())
        message = str(ctx.exception)
        for hint in ("--token", "UC_TOKEN", "--token-from-shell", "--mode slices", "doctor"):
            self.assertIn(hint, message)

    def test_token_from_shell_without_configs_fails_clearly(self):
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.object(self.m, "get_book_details", return_value={"id": "x", "title": "t"}), \
             mock.patch.object(self.m.Path, "home", return_value=Path(home)), \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(home) / ".config")}, clear=True):
            with self.assertRaises(self.m.SmartEduError) as ctx:
                self.m.cmd_download(self.make_args(from_shell=True))
        self.assertIn("没在常见 shell 配置里找到 UC_TOKEN", str(ctx.exception))

    def test_slices_mode_never_asks_for_token(self):
        with mock.patch.object(self.m, "get_book_details", return_value={"id": "x", "title": "t"}), \
             mock.patch.object(self.m, "download_slices_pdf") as fake, \
             mock.patch.dict(os.environ, {}, clear=True):
            args = self.make_args()
            args.mode = "slices"
            self.m.cmd_download(args)
        fake.assert_called_once()


class TestTokenParsing(OfflineCase):
    def encode(self, payload: dict) -> str:
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def test_decode_valid_token(self):
        token = self.encode({"access_token": "a", "mac_key": "k"})
        self.assertEqual(self.m.parse_uc_token(token), ("a", "k"))

    def test_decode_accepts_bearer_and_urlsafe(self):
        token = self.encode({"access_token": "a", "mac_key": "k"})
        urlsafe = token.replace("+", "-").replace("/", "_").rstrip("=")
        self.assertEqual(self.m.parse_uc_token(f"Bearer {urlsafe}"), ("a", "k"))

    def test_decode_rejects_garbage_and_incomplete(self):
        self.assertIsNone(self.m.decode_uc_token("not-a-token"))
        with self.assertRaises(self.m.SmartEduError):
            self.m.parse_uc_token(self.encode({"access_token": "a"}))  # 缺 mac_key

    def test_expires_at_iso_and_epoch(self):
        from datetime import datetime, timezone
        info = {"expires_at": "2026-10-24T15:11:39.299+0800"}
        parsed = self.m.token_expires_at(info)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 7)

        epoch = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
        self.assertIsNotNone(self.m.token_expires_at({"expires_at": epoch}))
        self.assertIsNotNone(self.m.token_expires_at({"expires_at": epoch * 1000}))
        self.assertIsNone(self.m.token_expires_at({"expires_at": "not-a-date"}))
        self.assertIsNone(self.m.token_expires_at({}))


class TestShellConfigScan(OfflineCase):
    def extract(self, text):
        return self.m._UC_TOKEN_LINE.findall(text)

    def first_value(self, text):
        match = self.m._UC_TOKEN_LINE.search(text)
        return match.group("value") if match else None

    def test_matches_every_definition_in_file(self):
        text = "export UC_TOKEN=old\nexport UC_TOKEN=new\n"
        self.assertEqual(self.extract(text), ["old", "new"])
        self.assertEqual(self.first_value(text), "old")

    def test_shell_syntaxes(self):
        cases = {
            "export UC_TOKEN=abc": ["abc"],
            'export UC_TOKEN="abc"  # note': ["abc"],
            "export  UC_TOKEN =  abc": ["abc"],
            "set -gx UC_TOKEN 'a=b'": ["a=b"],
            "$env:UC_TOKEN = \"abc\"": ["abc"],
            "[Environment]::SetEnvironmentVariable('UC_TOKEN', 'abc')": ["abc"],
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.extract(line + "\n"), expected)

    def test_ignores_unrelated_lines(self):
        text = "export PATH=/usr/bin\nalias UC_TOKEN_X=1\n"
        self.assertEqual(self.extract(text), [])

    def test_scan_shell_configs_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / ".bashrc"
            rc.write_text("export UC_TOKEN=only\n", encoding="utf-8")
            with mock.patch.object(self.m.Path, "home", return_value=Path(tmp)), \
                 mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp) / ".config")}):
                found = self.m.scan_shell_configs()
            self.assertEqual([(str(p.name), shell, v) for p, shell, v in found],
                             [(".bashrc", "bash", "only")])

    def test_resolve_order(self):
        self.assertEqual(self.m.resolve_token("cli", from_shell=False), "cli")
        with mock.patch.dict(os.environ, {"UC_TOKEN": "env"}):
            self.assertEqual(self.m.resolve_token(None, from_shell=True), "env")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(self.m, "scan_shell_configs", return_value=[(Path("x"), "bash", "shell")]):
            self.assertEqual(self.m.resolve_token(None, from_shell=True), "shell")
            self.assertIsNone(self.m.resolve_token(None, from_shell=False))


class TestFetchFallback(OfflineCase):
    def test_signed_request_resigns_each_mirror(self):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            if len(seen) == 1:
                raise urllib.error.URLError("boom")
            return io.BytesIO(b"OK").__enter__()

        headers = lambda url: {"User-Agent": "t", "X-ND-AUTH": "MAC nonce=%s" % hash(url)}  # noqa: E731
        with mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
            data = self.m._fetch_with_fallback(["https://a/1", "https://b/2"], headers)
        self.assertEqual(data, b"OK")
        self.assertEqual(len(seen), 2)

    def test_signed_401_raises_immediately(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        with mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.m.SmartEduError):
                self.m._fetch_with_fallback(["https://a/1", "https://b/2"],
                                            lambda u: {"X-ND-AUTH": "MAC"})
        self.assertEqual(len(calls), 1, "签名失败不应重试其它镜像")

    def test_public_403_tries_all_mirrors(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        with mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.m.SmartEduError):
                self.m._fetch_with_fallback(["https://a/1", "https://b/2", "https://c/3"],
                                            lambda u: {"User-Agent": "t"})
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
