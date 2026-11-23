"""Soxta `hermes` CLI: chaqiruvlarni jurnalga yozadi, config.yaml'ni nuqtali kalit bilan o'zgartiradi."""
import json
import os
import pathlib
import shutil
import sys

import yaml

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
home = pathlib.Path(os.environ["HOME"]) / ".hermes"
profile = None
if args[:1] == ["-p"]:
    profile, args = args[1], args[2:]
base = home if profile is None else home / "profiles" / profile


def load():
    path = base / "config.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


if args == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "Hermes Agent v0.23.3"))
elif args[:2] == ["config", "set"]:
    key, raw = args[2], args[3]
    if raw in ("true", "false"):
        value = raw == "true"
    elif raw.isdigit():
        value = int(raw)
    elif raw[:1] in "[{":
        value = json.loads(raw)
    else:
        value = raw
    config = load()
    cursor = config
    parts = key.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    (base / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
elif args[:2] == ["config", "get"]:
    cursor = load()
    for part in args[2].split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            print(f"Config key not set: {args[2]}")
            sys.exit(1)
        cursor = cursor[part]
    print(json.dumps(cursor) if "--json" in args else (str(cursor).lower() if isinstance(cursor, bool) else cursor))
elif args[:2] == ["profile", "create"]:
    target = home / "profiles" / args[2]
    target.mkdir(parents=True)
    env_text = os.environ.get("FAKE_PROFILE_ENV", "OPENAI_API_KEY=sk-test\n")
    (target / ".env").write_text(env_text)
    (target / "config.yaml").write_text("model:\n  default: inherited\n")
elif args[:2] == ["profile", "delete"]:
    shutil.rmtree(home / "profiles" / args[2])
elif args[:1] in (["plugins"], ["gateway"], ["pairing"]):
    print("ok")
else:
    print("unknown", args)
    sys.exit(3)
