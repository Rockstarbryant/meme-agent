from __future__ import annotations

import base64
import hashlib
import time
import uuid

import bcrypt
import jwt

_DUMMY = bcrypt.hashpw(b"dummy", bcrypt.gensalt(12)).decode()


def _pre(pw: str) -> bytes:
    # bcrypt truncates at 72 bytes; pre-hash so long passphrases are fully used
    return base64.b64encode(hashlib.sha256(pw.encode()).digest())


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(_pre(pw), bcrypt.gensalt(12)).decode()


def verify_password(pw: str, hashed: str | None) -> bool:
    ok = bcrypt.checkpw(_pre(pw), (hashed or _DUMMY).encode())  # constant work even for unknown users
    return ok and hashed is not None


def make_token(user_id: str, secret: str, minutes: int) -> tuple[str, int]:
    now = int(time.time())
    exp = now + minutes * 60
    return jwt.encode({"sub": user_id, "iat": now, "exp": exp, "jti": uuid.uuid4().hex}, secret, algorithm="HS256"), minutes * 60


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "jti"]})
