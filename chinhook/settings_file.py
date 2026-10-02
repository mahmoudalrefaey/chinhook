"""Saving the settings a session is connected with to a file, and loading them back.

Someone who comes back should not have to type a database URL, a model and an API key all
over again. The chat page exports the settings the session is connected with, and the setup
screen imports them. The file is made in memory and goes straight to the visitor's browser,
and an imported one is read in memory: nothing about either is kept on this server.

The database password and the model API key go into a file only encrypted, under a passphrase
the visitor chooses. scrypt turns the passphrase into a key and AES-256-GCM encrypts the
settings with it, all of them rather than only the two secrets. A file that is lost or shared
is then of no use without the passphrase, and one that was altered, for instance to point a
saved password at a different host, does not load at all. Without a passphrase the file holds
everything else, and the password and key are typed in when it is imported.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.engine import URL, make_url

from chinhook import connection, runtime

FORMAT = "chinhook-settings"
VERSION = 1
MIN_PASSPHRASE_LENGTH = 10
# A real file is well under a kilobyte; anything near this size is not one.
MAX_FILE_BYTES = 64 * 1024
_MAX_FIELD_LENGTH = 4096

# scrypt's cost is fixed by the format version rather than read from the file, so a file
# cannot make the server spend more than this on it: 32 MiB and about a tenth of a second
# per attempt. At most two run at once across every session, which bounds the memory a burst
# of imports can take however many visitors send one.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SALT_BYTES, _NONCE_BYTES = 16, 12
_KDF_SLOTS = threading.BoundedSemaphore(2)
# Bound into every encryption, so a block of ciphertext only ever opens as this format.
_ASSOCIATED_DATA = f"{FORMAT}/{VERSION}".encode("ascii")

_DAMAGED = "That configuration file is incomplete or damaged. Export it again from the chat page."


class SettingsFileError(ValueError):
    """A file that cannot be loaded, or a passphrase that cannot be used, said so a person can act on it."""


@dataclass(frozen=True)
class LoadedSettings:
    """Everything the setup form needs, as read from a file.

    url is the connection URL as the form takes it, with the password inside it once there is
    one; like runtime.LLMSettings.api_key it is left out of repr, so neither secret can end up
    in a log line by accident.
    """

    dialect: str
    url: str = field(repr=False)
    schema: str
    ssl_mode: str
    provider: str
    base_url: str
    model: str
    fast_model: str = ""
    api_key: str = field(default="", repr=False)
    # Whether the file carried the password and the key, encrypted under a passphrase.
    encrypted: bool = False

    def describe(self) -> str:
        """Which database and which model, without any secret: what a person checks before connecting."""
        url = make_url(self.url)
        kind = "PostgreSQL" if self.dialect == runtime.POSTGRES else "MySQL"
        port = f":{url.port}" if url.port else ""
        where = urlsplit(self.base_url).hostname or self.base_url
        return _plain(f"{url.database} on {url.host}{port} ({kind}) · {self.model} at {where}")

    def with_secrets(self, password: str = "", api_key: str = "") -> "LoadedSettings":
        """These settings with a password and key typed in for a file that did not carry them."""
        url = self.url
        if password:
            url = make_url(url).set(password=password).render_as_string(hide_password=False)
        return replace(self, url=url, api_key=(api_key or "").strip() or self.api_key)


@dataclass(frozen=True)
class FileInfo:
    """What an uploaded file is, found out before anything is decrypted.

    label says which database and model the file is for. For an encrypted file it is only
    what the file's own readable summary claims, and it is not checked: the settings that are
    actually loaded are the encrypted ones, and they are described again once unlocked.
    """

    encrypted: bool
    label: str


def file_name(rt: runtime.Runtime) -> str:
    """A name for the exported file that says which database it is for."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", rt.db.url.database or "").strip("-.")
    return f"chinhook-{name or 'settings'}.json"


def check_new_passphrase(passphrase: str, repeated: str) -> None:
    """Refuse a passphrase too short to protect a password and a key, or typed two ways."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise SettingsFileError(
            f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters. A few words "
            "that mean something only to you are easier to remember than random letters."
        )
    if passphrase != repeated:
        raise SettingsFileError("The two passphrases are not the same.")


def export_settings(rt: runtime.Runtime, passphrase: str | None = None) -> bytes:
    """The file for the settings rt is connected with.

    With a passphrase, everything, the database password and the API key included, is
    encrypted under it. Without one, the file holds everything except those two.
    """
    settings = _settings_of(rt, with_secrets=passphrase is not None)
    document: dict = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": _loaded(settings, encrypted=passphrase is not None).describe(),
    }
    if passphrase is None:
        document["settings"] = settings
    else:
        if len(passphrase) < MIN_PASSPHRASE_LENGTH:
            raise SettingsFileError(f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters.")
        document["encrypted"] = _encrypt(settings, passphrase)
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def inspect(data: bytes) -> FileInfo:
    """Whether a file is one this app can load, and whether it needs a passphrase to."""
    document = _document(data)
    if "encrypted" in document:
        _encrypted_block(document["encrypted"])
        return FileInfo(encrypted=True, label=_plain(document.get("summary")) or "an unnamed connection")
    return FileInfo(encrypted=False, label=_loaded(document["settings"], encrypted=False).describe())


def load_settings(data: bytes, passphrase: str = "") -> LoadedSettings:
    """The settings in a file, decrypted with passphrase when the file is encrypted."""
    document = _document(data)
    if "settings" in document:
        return _loaded(document["settings"], encrypted=False)

    salt, nonce, ciphertext = _encrypted_block(document["encrypted"])
    if not passphrase:
        raise SettingsFileError("Enter the passphrase this file was exported with.")
    try:
        plaintext = AESGCM(_key(passphrase, salt)).decrypt(nonce, ciphertext, _ASSOCIATED_DATA)
    except InvalidTag:
        raise SettingsFileError(
            "That passphrase does not open this file, or the file was changed after it was "
            "exported."
        ) from None
    try:
        settings = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SettingsFileError(_DAMAGED) from None
    return _loaded(settings, encrypted=True)


# ---------- writing ----------

def _settings_of(rt: runtime.Runtime, with_secrets: bool) -> dict:
    url = rt.db.url
    # The scheme a provider writes rather than this app's own driver name, and no password:
    # the file reads like the URL a person would have typed.
    plain_url = URL.create(
        rt.db.dialect,
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
    database = {
        "dialect": rt.db.dialect,
        "url": plain_url,
        # For MySQL the schema is the database itself and is not asked for separately.
        "schema": rt.db.schema if rt.db.dialect == runtime.POSTGRES else "",
        "ssl_mode": rt.db.ssl_mode,
    }
    model = {
        "provider": connection.provider_for(rt.llm.base_url),
        "base_url": rt.llm.base_url,
        "model": rt.llm.model,
        "fast_model": rt.llm.fast_model,
    }
    if with_secrets:
        database["password"] = url.password or ""
        model["api_key"] = rt.llm.api_key
    return {"database": database, "model": model}


def _encrypt(settings: dict, passphrase: str) -> dict:
    salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(settings, ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(_key(passphrase, salt)).encrypt(nonce, plaintext, _ASSOCIATED_DATA)
    return {
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "data": base64.b64encode(ciphertext).decode("ascii"),
    }


def _key(passphrase: str, salt: bytes) -> bytes:
    # NFKC first, so the same passphrase typed on two keyboards that compose an accented
    # letter differently still makes the same key.
    secret = unicodedata.normalize("NFKC", passphrase).encode("utf-8")
    with _KDF_SLOTS:
        return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(secret)


# ---------- reading ----------

def _document(data: bytes) -> dict:
    if len(data) > MAX_FILE_BYTES:
        raise SettingsFileError("That file is too large to be a Chinhook configuration file.")
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SettingsFileError("That is not a Chinhook configuration file: it is not valid JSON.") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise SettingsFileError(
            "That is not a Chinhook configuration file. Export one from the chat page's "
            "sidebar, with Export configuration."
        )
    version = document.get("version")
    if version != VERSION:
        if isinstance(version, int) and version > VERSION:
            raise SettingsFileError(
                "That file was made by a newer version of Chinhook than this one, and cannot be "
                "read here."
            )
        raise SettingsFileError(_DAMAGED)
    if ("settings" in document) == ("encrypted" in document):
        raise SettingsFileError(_DAMAGED)
    return document


def _encrypted_block(block) -> tuple[bytes, bytes, bytes]:
    if not isinstance(block, dict):
        raise SettingsFileError(_DAMAGED)
    expected = {"kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "cipher": "AES-256-GCM"}
    if any(block.get(name) != value for name, value in expected.items()):
        raise SettingsFileError(
            "That file is encrypted in a way this version of Chinhook does not read. Export it "
            "again from the chat page."
        )
    try:
        salt, nonce, ciphertext = (
            base64.b64decode(str(block.get(name, "")), validate=True) for name in ("salt", "nonce", "data")
        )
    except (binascii.Error, ValueError):
        raise SettingsFileError(_DAMAGED) from None
    if len(salt) != _SALT_BYTES or len(nonce) != _NONCE_BYTES or not ciphertext:
        raise SettingsFileError(_DAMAGED)
    return salt, nonce, ciphertext


def _loaded(settings, encrypted: bool) -> LoadedSettings:
    if not isinstance(settings, dict):
        raise SettingsFileError(_DAMAGED)
    database, model = settings.get("database"), settings.get("model")
    if not isinstance(database, dict) or not isinstance(model, dict):
        raise SettingsFileError(_DAMAGED)

    dialect = _text(database, "dialect")
    if dialect not in runtime.SUPPORTED_DIALECTS:
        raise SettingsFileError("The database in that file is neither PostgreSQL nor MySQL.")
    url, schema = _text(database, "url"), _text(database, "schema")
    ssl_mode = _text(database, "ssl_mode")
    if ssl_mode not in runtime.SSL_MODES:
        # A Postgres URL can carry any libpq sslmode, and it wins over this setting anyway.
        ssl_mode = "prefer"
    # The same checks a URL typed on the setup screen gets, so a file cannot carry anything
    # the form itself would refuse.
    try:
        checked = connection.database_settings_from_url(url, schema=schema, ssl_mode=ssl_mode)
    except connection.ConnectionSetupError as exc:
        raise SettingsFileError(f"The database in that file cannot be used: {exc}") from None
    if checked.dialect != dialect:
        raise SettingsFileError(_DAMAGED)
    password = _text(database, "password") if encrypted else ""
    if password:
        url = make_url(url).set(password=password).render_as_string(hide_password=False)

    base_url = _text(model, "base_url").rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise SettingsFileError("The model's base URL in that file must start with https:// (or http://).")
    model_name = _text(model, "model")
    if not model_name:
        raise SettingsFileError("That file does not name a model.")
    provider = _text(model, "provider")
    if provider not in connection.PROVIDERS:
        provider = connection.provider_for(base_url)

    return LoadedSettings(
        dialect=dialect,
        url=url,
        schema=checked.schema if dialect == runtime.POSTGRES else "",
        ssl_mode=ssl_mode,
        provider=provider,
        base_url=base_url,
        model=model_name,
        fast_model=_text(model, "fast_model"),
        api_key=_text(model, "api_key") if encrypted else "",
        encrypted=encrypted,
    )


def _text(mapping: dict, name: str) -> str:
    value = mapping.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _MAX_FIELD_LENGTH:
        raise SettingsFileError(_DAMAGED)
    return value.strip()


def _plain(value) -> str:
    """Text from a file made safe to show as one line: no line breaks, and not too long."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= 160 else text[:157] + "..."
