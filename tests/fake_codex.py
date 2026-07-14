#!/usr/bin/env python3
"""Stdlib-only stdio JSON-RPC peer used to exercise StdioProcessTransport.

Modes (argv[1], default "echo"):
- echo:           serve requests until stdin EOF, then exit 0
- ignore-eof:     after stdin EOF, sleep forever (killed by SIGTERM)
- ignore-sigterm: additionally ignore SIGTERM (must be SIGKILLed)

Methods served:
- echo   -> result = params
- big    -> result = {"blob": "x" * params["size"]}
- stderr -> writes params["text"] to stderr, result = {}
- notify-back -> emits a notification params["method"] first, then result = {}
- die    -> writes "dying" to stderr and exits with params["code"]
"""

import json
import signal
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    if mode == "ignore-sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get("method")
        params = message.get("params") or {}
        request_id = message.get("id")
        if method == "echo":
            send({"id": request_id, "result": params})
        elif method == "big":
            send({"id": request_id, "result": {"blob": "x" * params["size"]}})
        elif method == "stderr":
            sys.stderr.write(params["text"] + "\n")
            sys.stderr.flush()
            send({"id": request_id, "result": {}})
        elif method == "notify-back":
            send({"method": params["method"], "params": {"from": "fake"}})
            send({"id": request_id, "result": {}})
        elif method == "die":
            sys.stderr.write("dying\n")
            sys.stderr.flush()
            sys.exit(params["code"])
        elif request_id is not None:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}})

    if mode in ("ignore-eof", "ignore-sigterm"):
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
