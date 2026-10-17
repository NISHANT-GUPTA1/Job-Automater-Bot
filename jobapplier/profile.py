"""Loads the user's profile and base-resume text (the stable context sent to the LLM)."""
from functools import lru_cache

import yaml
from pypdf import PdfReader

from . import config


@lru_cache(maxsize=1)
def load_profile() -> dict:
    path = config.PROFILE_PATH
    if not path.exists():
        path = config.ROOT / "profile.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("personal", {})["email"] = config.APPLY_EMAIL
    return data


def get(path: str, default: str = "") -> str:
    """Dotted lookup, e.g. get('personal.first_name')."""
    node = load_profile()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return str(node) if node not in (None, "") else default


def profile_text() -> str:
    return yaml.safe_dump(load_profile(), sort_keys=True, allow_unicode=True)


@lru_cache(maxsize=1)
def base_resume_text() -> str:
    tex = config.RESUME_DIR / "base_resume.tex"
    if tex.exists():
        from .latex import to_plain_text
        return to_plain_text(tex.read_text(encoding="utf-8"))
    if not config.BASE_RESUME_PATH.exists():
        return ""
    reader = PdfReader(str(config.BASE_RESUME_PATH))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


def reload() -> None:
    load_profile.cache_clear()
    base_resume_text.cache_clear()
