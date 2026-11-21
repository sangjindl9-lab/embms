"""Password hashing (scrypt, stdlib) and opaque token helpers."""
import base64
import hashlib
import hmac
import secrets

_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# Dummy hash used to keep timing similar when the user does not exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
