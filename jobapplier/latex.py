"""LaTeX resume handling: the Overleaf template's header stays fixed, only the body is tailored.

Compiled locally with Tectonic (tools/tectonic/tectonic.exe, or `tectonic` on PATH).
"""
import re
import shutil
import subprocess
from pathlib import Path

from . import config

BASE_TEX = config.RESUME_DIR / "base_resume.tex"
TECTONIC = config.ROOT / "tools" / "tectonic" / "tectonic.exe"
BODY_START = r"\section{Education}"
# the real \section{Education} at the start of a line (optionally with its "% --- EDUCATION ---" banner),
# never one mentioned inside a comment
BODY_START_RE = re.compile(r"^[ \t]*(?:% -+ EDUCATION -+[ \t]*\n[ \t]*)?\\section\{Education\}", re.M)

# Tectonic can't find system fonts by family name, so point at the bundled Latin Modern files.
FONT_BY_NAME = r"\setmainfont{Latin Modern Roman}"
FONT_BY_FILE = (r"\setmainfont{lmroman10}[Extension=.otf,UprightFont=*-regular,BoldFont=*-bold,"
                r"ItalicFont=*-italic,BoldItalicFont=*-bolditalic,SmallCapsFont=lmromancaps10-regular]")


def tectonic_path() -> str:
    for candidate in (TECTONIC, TECTONIC.with_suffix("")):  # Windows .exe / Linux binary
        if candidate.exists():
            return str(candidate)
    found = shutil.which("tectonic")
    if not found:
        raise RuntimeError("Tectonic not found. Put tectonic.exe in tools/tectonic/ (see README).")
    return found


def split_template(tex: str) -> tuple[str, str, str]:
    """Returns (preamble+header, body, ending). The header (name + contact line) is never changed."""
    m = BODY_START_RE.search(tex)
    if not m:
        raise ValueError(r"Template must contain \section{Education} where the body starts")
    end = tex.rfind(r"\end{document}")
    return tex[:m.start()], tex[m.start():end].strip(), tex[end:]


def base_parts() -> tuple[str, str, str]:
    return split_template(BASE_TEX.read_text(encoding="utf-8"))


def set_pdf_meta(head: str, title: str | None, keywords: str | None) -> str:
    if title:
        head = re.sub(r"pdftitle=\{[^}]*\}", lambda _: "pdftitle={" + title + "}", head)
    if keywords:
        head = re.sub(r"pdfkeywords=\{[^}]*\}", lambda _: "pdfkeywords={" + keywords + "}", head)
    return head


def build(body: str, title: str | None = None, keywords: str | None = None) -> str:
    head, _, end = base_parts()
    return f"{set_pdf_meta(head, title, keywords)}{body.strip()}\n\n{end}"


def compile_pdf(tex: str, out_pdf: Path) -> None:
    """Compile LaTeX source to out_pdf. Raises RuntimeError with the LaTeX error on failure."""
    work = config.DATA_DIR / "latex"
    work.mkdir(parents=True, exist_ok=True)
    src = work / (out_pdf.stem + ".tex")
    src.write_text(tex.replace(FONT_BY_NAME, FONT_BY_FILE), encoding="utf-8")
    proc = subprocess.run([tectonic_path(), "-X", "compile", "--outdir", str(work), str(src)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    pdf = work / (out_pdf.stem + ".pdf")
    if proc.returncode != 0 or not pdf.exists():
        errors = [l for l in (proc.stderr + proc.stdout).splitlines() if l.startswith("error") or "!" in l]
        raise RuntimeError("\n".join(errors[:8]) or "LaTeX compile failed")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf), out_pdf)
    shutil.copy(src, out_pdf.with_suffix(".tex"))  # keep the source next to the PDF (for Overleaf edits)


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def to_plain_text(tex: str, body_only: bool = False) -> str:
    """Rough LaTeX -> text, used as compact LLM context. body_only=True when tex is just the body
    (its sections may be in any order, so don't search for where Education starts)."""
    body = tex if body_only else split_template(tex)[1]
    t = re.sub(r"(?<!\\)%.*", "", body)
    t = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\(section)\{([^}]*)\}", r"\n## \2\n", t)
    t = t.replace(r"\item", "-").replace(r"\\", "\n").replace(r"\%", "%").replace(r"\&", "&")
    t = t.replace(r"$\rightarrow$", "->")
    t = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?", "", t)
    t = re.sub(r"\\vspace\{[^}]*\}|\\hfill|\\,", " ", t)
    t = re.sub(r"\\[a-zA-Z]+\*?\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\[a-zA-Z]+", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()
