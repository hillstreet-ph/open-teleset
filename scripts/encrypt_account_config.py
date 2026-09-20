"""Create a separate encrypted local account config; never overwrite the source.

Run with PYTHONPATH=src and SESSION_ENCRYPTION_KEY supplied by a secret store.
Stop the runtime, retain a protected backup, validate the output, and promote it
only after deployment approval. This script performs no Telegram/network calls.
"""
import argparse
import json
import os
from pathlib import Path

from open_teleset.crypto import encrypt_session, decrypt_session


def convert(source, destination):
    records = json.loads(Path(source).read_text(encoding="utf-8"))
    converted = {}
    for key, value in records.items():
        item = dict(value)
        if "session_string" in item:
            plain = item.pop("session_string")
            encrypted = encrypt_session(plain)
            if decrypt_session(encrypted) != plain:
                raise ValueError("Encryption roundtrip failed")
            item["session_encrypted"] = encrypted
        else:
            decrypt_session(item.get("session_encrypted", ""))
        converted[key] = item
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(converted, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    convert(args.source, args.destination)
