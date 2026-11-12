import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import har_budget as hb


def entry(headers=10, body=90, content=200, **extra):
    return {"response": {"headersSize": headers, "bodySize": body,
                         "content": {"size": content}, **extra}}


def doc(*entries):
    return {"log": {"version": "1.2", "entries": list(entries)}}


class AuditTests(unittest.TestCase):
    def test_standard_sizes_and_decoded_separate(self):
        r = hb.audit(doc(entry()), 100, 1)
        self.assertEqual(r["transfer_bytes"]["known_total"], 100)
        self.assertEqual(r["decoded_content_bytes"]["known_total"], 200)
        self.assertEqual(r["status"], "pass")

    def test_one_over_budget_fails(self):
        self.assertEqual(hb.audit(doc(entry()), 99)["status"], "fail")

    def test_unknown_minus_one_is_not_zero(self):
        r = hb.audit(doc(entry(-1, -1, -1)), 1000)
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["transfer_bytes"]["unknown_entries"], 1)
        self.assertEqual(r["decoded_content_bytes"]["unknown_entries"], 1)

    def test_missing_fields_are_unknown(self):
        self.assertEqual(hb.audit(doc({"response": {}}), 0)["status"], "unknown")

    def test_zero_is_known(self):
        self.assertEqual(hb.audit(doc(entry(0, 0, 0)), 0)["status"], "pass")

    def test_extension_has_precedence(self):
        r = hb.audit(doc(entry(_transferSize=60)), 60)
        self.assertEqual(r["transfer_bytes"]["known_total"], 60)
        self.assertEqual(r["transfer_bytes"]["extension_entries"], 1)
        self.assertEqual(r["status"], "pass")

    def test_zero_extension_cached_can_be_known(self):
        r = hb.audit(doc(entry(-1, -1, 1000, _transferSize=0)), 0)
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["decoded_content_bytes"]["known_total"], 1000)

    def test_unknown_extension_falls_back_to_standard(self):
        self.assertEqual(hb.audit(doc(entry(_transferSize=-1)), 100)["status"], "pass")

    def test_partial_sizes_contribute_lower_bound(self):
        r = hb.audit(doc(entry(10, -1)), 9)
        self.assertEqual(r["transfer_bytes"]["known_total"], 10)
        self.assertEqual(r["status"], "fail")

    def test_known_failure_dominates_unknown(self):
        self.assertEqual(hb.audit(doc(entry(), entry(-1, -1)), 99)["status"], "fail")

    def test_unknown_content_does_not_poison_transfer(self):
        self.assertEqual(hb.audit(doc(entry(content=-1)), 100)["status"], "pass")

    def test_duplicates_not_deduplicated(self):
        one = entry()
        one["request"] = {"url": "https://example.test/same"}
        r = hb.audit(doc(one, one), 200, 2)
        self.assertEqual(r["requests"], 2)
        self.assertEqual(r["transfer_bytes"]["known_total"], 200)

    def test_304_not_assumed_free(self):
        r = hb.audit(doc(entry(20, 0, 999, status=304)), 19)
        self.assertEqual(r["responses_304"], 1)
        self.assertEqual(r["status"], "fail")

    def test_cache_metadata_not_assumed_free(self):
        one = entry(-1, -1)
        one["cache"] = {"beforeRequest": {"comment": "cached"}}
        self.assertEqual(hb.audit(doc(one), 0)["status"], "unknown")

    def test_empty_log_and_zero_budgets(self):
        self.assertEqual(hb.audit(doc(), 0, 0)["status"], "pass")

    def test_no_budget_not_a_pass(self):
        self.assertEqual(hb.audit(doc(entry()))["status"], "not_requested")

    def test_request_budget_can_fail_even_if_transfer_unknown(self):
        self.assertEqual(hb.audit(doc(entry(-1, -1)), 1000, 0)["status"], "fail")

    def test_invalid_size_types(self):
        for key in ("headersSize", "bodySize", "_transferSize"):
            for bad in (True, False, 1.0, -1.0, "0", None, -2, hb.MAX_SIZE + 1, [], {}):
                with self.subTest(key=key, bad=bad), self.assertRaises(hb.InvalidHar):
                    one = entry()
                    one["response"][key] = bad
                    hb.audit(doc(one))

    def test_invalid_content_sizes(self):
        for bad in (False, "-1", -2, None, 1.0):
            with self.subTest(bad=bad), self.assertRaises(hb.InvalidHar):
                hb.audit(doc(entry(content=bad)))

    def test_bad_shapes(self):
        for bad in (None, [], {}, {"log": []}, {"log": {}},
                    {"log": {"entries": {}}}, doc(None), doc({"response": []}),
                    doc({"response": {"content": []}})):
            with self.subTest(bad=bad), self.assertRaises(hb.InvalidHar):
                hb.audit(bad)

    def test_entry_bound(self):
        with patch.object(hb, "MAX_ENTRIES", 1), self.assertRaises(hb.InvalidHar):
            hb.audit(doc(entry(), entry()))

    def test_invalid_budgets(self):
        for bad in (-1, True, 0.0, "1", hb.MAX_SIZE + 1):
            with self.subTest(bad=bad), self.assertRaises(hb.InvalidHar):
                hb.audit(doc(), bad)

    def test_private_strings_never_emitted(self):
        one = entry()
        one.update(request={"url": "SECRET_URL", "headers": [{"value": "SECRET_HEADER"}],
                            "cookies": [{"value": "SECRET_COOKIE"}]}, comment="SECRET_COMMENT")
        one["response"]["content"].update(text="SECRET_BODY", mimeType="SECRET_MIME")
        text = json.dumps(hb.audit(doc(one)))
        self.assertNotIn("SECRET", text)


class CliTests(unittest.TestCase):
    def run_cli(self, raw, *args):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "private-name.har"
            p.write_bytes(raw)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = hb.main([str(p), *args])
            return result, out.getvalue(), err.getvalue()

    def test_cli_pass_fail_unknown(self):
        for item, budget, code in ((entry(), "100", 0), (entry(), "99", 1),
                                   (entry(-1, -1), "100", 2)):
            with self.subTest(code=code):
                c, out, err = self.run_cli(json.dumps(doc(item)).encode(), "--max-transfer-bytes", budget)
                self.assertEqual(c, code)
                self.assertEqual(err, "")
                json.loads(out)

    def test_malformed_json_no_echo(self):
        c, out, err = self.run_cli(b'{"SECRET":')
        self.assertEqual(c, 2)
        self.assertEqual(out, "")
        self.assertNotIn("SECRET", err)
        self.assertNotIn("private-name", err)

    def test_duplicate_key_rejected(self):
        c, _, err = self.run_cli(b'{"log":{"entries":[],"entries":[]}}')
        self.assertEqual(c, 2)
        self.assertIn("Duplicate", err)

    def test_nonstandard_constant_rejected(self):
        c, _, _ = self.run_cli(b'{"log":{"entries":[]},"secret":NaN}')
        self.assertEqual(c, 2)

    def test_non_utf8_rejected(self):
        self.assertEqual(self.run_cli(b'\xff')[0], 2)

    def test_file_bound(self):
        with patch.object(hb, "MAX_FILE_BYTES", 5):
            self.assertEqual(self.run_cli(b'123456')[0], 2)

    def test_bom_accepted(self):
        self.assertEqual(self.run_cli(b'\xef\xbb\xbf'+json.dumps(doc()).encode())[0], 0)

    def test_missing_file_sanitized(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(hb.main(["/definitely/not/SECRET.har"]), 2)
        self.assertNotIn("SECRET", err.getvalue())

    def test_invalid_cli_does_not_echo_value(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            hb.main(["private.har", "--max-requests", "SECRET"])
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn("SECRET", err.getvalue())

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO only")
    def test_fifo_rejected_without_open_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "fifo.har"
            os.mkfifo(p)
            with self.assertRaises(hb.InvalidHar):
                hb.load_har(p)

    def test_deep_json_rejected_cleanly(self):
        self.assertEqual(self.run_cli(b'['*2000+b']'*2000)[0], 2)


if __name__ == "__main__":
    unittest.main()
