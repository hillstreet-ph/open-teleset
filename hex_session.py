"""Convert a Telegram authorization key without logging or persisting it."""
import re

from telethon.crypto import AuthKey
from telethon.sessions import StringSession

# Telegram Desktop production bootstrap addresses, not caller-supplied hosts:
# https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/mtproto/mtproto_dc_options.cpp
PRODUCTION_DCS = {
    1: "149.154.175.50",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "149.154.171.5",
}


def hex_to_string_session(hex_key: str, dc_id: int) -> str:
    """Accept exactly one 256-byte key; whitespace from line wrapping is OK."""
    if type(dc_id) is not int or dc_id not in PRODUCTION_DCS:
        raise ValueError("Choose the original session's DC ID (1–5).")
    if not isinstance(hex_key, str) or len(hex_key) > 4096:
        raise ValueError("Hex session must contain exactly 512 hexadecimal characters.")
    normalized = re.sub(r"\s+", "", hex_key)
    if re.fullmatch(r"[0-9a-fA-F]{512}", normalized) is None:
        raise ValueError("Hex session must contain exactly 512 hexadecimal characters; paste only the key.")
    key = bytes.fromhex(normalized)
    if not any(key):
        raise ValueError("Hex session cannot be an empty authorization key.")
    session = StringSession()
    session.set_dc(dc_id, PRODUCTION_DCS[dc_id], 443)
    session.auth_key = AuthKey(key)
    return session.save()
