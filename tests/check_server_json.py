"""Validate server.json against the official MCP Registry schema (network needed).

    python tests/check_server_json.py
"""
import json
import sys
import urllib.request
from pathlib import Path

import jsonschema

doc = json.loads((Path(__file__).resolve().parent.parent / "server.json").read_text(encoding="utf-8"))
with urllib.request.urlopen(doc["$schema"], timeout=30) as r:
    schema = json.load(r)
errors = list(jsonschema.validators.validator_for(schema)(schema).iter_errors(doc))
for e in errors:
    print(f"- {'/'.join(map(str, e.path))}: {e.message}")
print(f"server.json: {len(errors)} schema error(s)")
sys.exit(1 if errors else 0)
