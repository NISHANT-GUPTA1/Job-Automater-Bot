"""Encrypts saved site passwords at rest."""
import secrets
import string

from cryptography.fernet import Fernet

from . import config

_KEY_FILE = config.DATA_DIR / "secret.key"


def _load_key() -> bytes:
    if config.SECRET_KEY:
        return config.SECRET_KEY.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    return key


_fernet = Fernet(_load_key())


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def master_password() -> str:
    """The ONE password used for every job-site account (like the one fixed email).
    Created once in a format almost every portal accepts (12 chars: upper, lower, digits, '@'), saved to .env."""
    if config.APPLY_PASSWORD:
        return config.APPLY_PASSWORD
    alphabet = string.ascii_letters + string.digits
    while True:
        core = "".join(secrets.choice(alphabet) for _ in range(10))
        if any(c.islower() for c in core) and any(c.isupper() for c in core) and any(c.isdigit() for c in core) \
                and core[0].isalpha():
            break
    pw = core[:5] + "@" + core[5:] + secrets.choice(string.digits)
    env = config.ROOT / ".env"
    lines = [l for l in (env.read_text(encoding="utf-8").splitlines() if env.exists() else [])
             if not l.strip().startswith("APPLY_PASSWORD=")]
    lines.append(f"APPLY_PASSWORD={pw}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.APPLY_PASSWORD = pw
    return pw


def generate_password(length: int = 14) -> str:
    """Password that satisfies typical job-portal rules (upper, lower, digit, symbol)."""
    symbols = "@#$%&!"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)
                and pw[0].isalpha()):
            return pw
