#!/usr/bin/env python3
"""Offline, aggregate-only HAR budget checks. No network requests or file writes."""
import argparse
import json
import os
import stat
import sys

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_SIZE = (1 << 63) - 1


class InvalidHar(ValueError):
    """An input error whose text contains no input data."""


def size_value(obj, key):
    if key not in obj or obj[key] == -1 and type(obj[key]) is int:
        return None
    value = obj[key]
    if type(value) is not int or not 0 <= value <= MAX_SIZE:
        raise InvalidHar("Invalid byte size; expected an integer >= 0 or -1.")
    return value


def budget_status(known, unknown_count, limit):
    if limit is None:
        return "not_requested"
    if known > limit:
        return "fail"
    return "unknown" if unknown_count else "pass"


def audit(document, max_transfer_bytes=None, max_requests=None):
    """Return fixed-schema aggregate data; source strings are never copied."""
    for limit in (max_transfer_bytes, max_requests):
        if limit is not None and (type(limit) is not int or not 0 <= limit <= MAX_SIZE):
            raise InvalidHar("Invalid budget; expected a nonnegative integer.")
    if not isinstance(document, dict) or not isinstance(document.get("log"), dict):
        raise InvalidHar("Expected a HAR object with log.entries.")
    entries = document["log"].get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise InvalidHar("Expected entries array within the entry limit.")
    transfer = decoded = 0
    unknown_transfer = unknown_decoded = extension_count = standard_count = 0
    status_304_count = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("response"), dict):
            raise InvalidHar("Each entry must contain a response object.")
        response = entry["response"]
        content = response.get("content", {})
        if not isinstance(content, dict):
            raise InvalidHar("Expected response.content to be an object.")
        # Validate even unused size fields. Never coerce a string/bool/float.
        headers = size_value(response, "headersSize")
        body = size_value(response, "bodySize")
        extension = size_value(response, "_transferSize")
        content_size = size_value(content, "size")
        if extension is not None:
            transferred = extension
            extension_count += 1
        elif headers is not None and body is not None:
            transferred = headers + body
            standard_count += 1
        else:
            transferred = None
        if transferred is None:
            unknown_transfer += 1
            transfer += (headers or 0) + (body or 0)
        else:
            transfer += transferred
        if content_size is None:
            unknown_decoded += 1
        else:
            decoded += content_size
        if type(response.get("status")) is int and response["status"] == 304:
            status_304_count += 1
    request_status = budget_status(len(entries), 0, max_requests)
    transfer_status = budget_status(transfer, unknown_transfer, max_transfer_bytes)
    requested = [s for s in (request_status, transfer_status) if s != "not_requested"]
    overall = ("not_requested" if not requested else "fail" if "fail" in requested
               else "unknown" if "unknown" in requested else "pass")
    return {
        "schema_version": 1,
        "requests": len(entries),
        "responses_304": status_304_count,
        "transfer_bytes": {"known_total": transfer, "unknown_entries": unknown_transfer,
                           "extension_entries": extension_count, "standard_entries": standard_count},
        "decoded_content_bytes": {"known_total": decoded, "unknown_entries": unknown_decoded},
        "budgets": {"requests": {"limit": max_requests, "status": request_status},
                    "transfer_bytes": {"limit": max_transfer_bytes, "status": transfer_status}},
        "status": overall,
    }


def load_har(path):
    try:
        # Nonblocking open avoids hanging on a FIFO. Read only regular files.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise InvalidHar("HAR input must be a regular file.")
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise InvalidHar("HAR exceeds the 32 MiB file limit.")
        # Reject nonstandard JSON NaN/Infinity and duplicate fields, not last-key wins.
        def reject_constant(_):
            raise InvalidHar("Nonstandard JSON number.")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise InvalidHar("Duplicate JSON object key.")
                result[key] = value
            return result
        return json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant,
                          object_pairs_hook=unique_object)
    except InvalidHar:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise InvalidHar("Cannot read valid UTF-8 HAR JSON.") from None


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid command line. Run with --help for usage.\n")


def nonnegative(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer.")
    number = int(value)
    if number > MAX_SIZE:
        raise argparse.ArgumentTypeError("Budget too large.")
    return number


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("har", help="HAR file, read locally; never uploaded")
    parser.add_argument("--max-transfer-bytes", type=nonnegative)
    parser.add_argument("--max-requests", type=nonnegative)
    args = parser.parse_args(argv)
    try:
        report = audit(load_har(args.har), args.max_transfer_bytes, args.max_requests)
    except InvalidHar as error:
        print(json.dumps({"status": "invalid", "error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return {"pass": 0, "not_requested": 0, "fail": 1, "unknown": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
