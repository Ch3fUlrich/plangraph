#!/usr/bin/env python3
"""PreToolUse byte limit for SendMessage; documents travel by pointer."""
import json
import os
import sys


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("tool_name") != "SendMessage":
            return 0
        message = payload["tool_input"]["message"]
        if not isinstance(message, str):
            raise ValueError
        size = len(message.encode("utf-8"))
    except (ValueError, TypeError, KeyError, UnicodeError):
        print("message-size-guard: malformed hook payload; allowing", file=sys.stderr)
        return 0
    try:
        limit = int(os.environ.get("PLANGRAPH_MESSAGE_MAX_BYTES", "6000"))
        if limit <= 0:
            raise ValueError
    except ValueError:
        limit = 6000
    if size > limit:
        print(f"message is {size} bytes, limit {limit}: a document goes by pointer: "
              "write it to a file, run fleet-artifact put <file>, send the ARTIFACT line",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
