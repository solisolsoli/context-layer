#!/bin/sh
# Lab candidate: make a context-helper failure visible instead of silent empty
# success. Installing context-layer does not install, register or run this file.
# It is an example of the wrapper shape an optional host integration could use,
# kept here with the other lab candidates so it can be reviewed and tested.
#
# The failure it addresses: a host wrapper that runs a helper, discards its exit
# code and its stderr, and injects whatever landed on stdout. When the helper
# dies, the prompt is built from nothing and the session looks healthy. That is
# indistinguishable from "there was no memory to add".
#
# Contract: the helper prints one JSON object on stdout and exits 0.
#   {"hookSpecificOutput": {"additionalContext": "<text to inject>"}}
# A missing or empty additionalContext is a legitimate "nothing to add": this
# wrapper stays silent and exits 0. Anything else is reported on stdout as a
# single "[Memory unavailable: ...]" line and exits non-zero.
#
# Usage, with the helper named by environment or as the first argument:
#   CONTEXT_HELPER=/path/to/helper ./hook-visible-error.example.sh [args...]
#   ./hook-visible-error.example.sh /path/to/helper [args...]
# CONTEXT_PYTHON overrides the interpreter used to parse the response.
#
# Limits: it checks the helper's exit code and the shape of its response. It
# does not verify what the helper retrieved, and injecting the returned text
# into a prompt remains the host's decision. No host is configured here.
set -u

python=${CONTEXT_PYTHON:-python3}
helper=${CONTEXT_HELPER:-}
if [ -z "$helper" ] && [ "$#" -gt 0 ]; then
  helper=$1
  shift
fi
if [ -z "$helper" ]; then
  printf '%s\n' '[Memory unavailable: no helper command given; set CONTEXT_HELPER]'
  exit 1
fi
if ! command -v "$helper" >/dev/null 2>&1; then
  printf '%s\n' '[Memory unavailable: helper is not an executable command]'
  exit 1
fi

# Command substitution strips trailing newlines from the JSON envelope only;
# newlines inside additionalContext survive, because they are JSON escapes.
out=$("$helper" "$@" 2>/dev/null)
rc=$?
if [ "$rc" -ne 0 ]; then
  printf '%s\n' "[Memory unavailable: helper exited $rc]"
  exit 1
fi
if [ -z "$out" ]; then
  printf '%s\n' '[Memory unavailable: empty helper response]'
  exit 1
fi

# The parser writes the context only after the payload validates, so a rejected
# response can never leave half a packet on stdout.
printf '%s' "$out" | "$python" -c 'import json, sys
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("response is not a JSON object")
text = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
if not isinstance(text, str):
    raise SystemExit("additionalContext is not a string")
sys.stdout.write(text)' 2>/dev/null || {
  printf '%s\n' '[Memory unavailable: malformed helper response]'
  exit 1
}
exit 0
