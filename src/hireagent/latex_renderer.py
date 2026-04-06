"""Deterministic LaTeX rendering utilities (fixed template only)."""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "templates" / "fixed_resume.tex"
_PDFINFO_PAGES_RE = re.compile(r"^\s*Pages:\s*(\d+)\s*$", re.MULTILINE)
_LOG_TAIL_LINES = 20
_LATEX_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def extract_bullets(tex: str) -> List[str]:
    bullets: List[str] = []
    for line in tex.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\item"):
            bullets.append(stripped[len("\\item"):].strip())
    return bullets


def escape_latex_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    # Normalize common unicode punctuation that can break latex compilation.
    cleaned = (
        cleaned.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    return "".join(_LATEX_ESCAPE_MAP.get(ch, ch) for ch in cleaned)


def apply_summary(tex: str, summary_text: str) -> str:
    """Replace %%SUMMARY%% placeholder with the escaped summary text."""
    return tex.replace("%%SUMMARY%%", escape_latex_text(summary_text))


def apply_bullets(tex: str, new_bullets: List[str]) -> str:
    lines = tex.splitlines()
    out_lines = []
    bullet_iter = iter(new_bullets)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("\\item"):
            try:
                replacement = next(bullet_iter)
            except StopIteration as exc:
                raise ValueError("More bullet lines in template than provided replacements") from exc
            out_lines.append("    \\item " + escape_latex_text(replacement))
        else:
            out_lines.append(line)
    try:
        next(bullet_iter)
        raise ValueError("More replacements provided than bullet lines in template")
    except StopIteration:
        pass
    return "\n".join(out_lines)


def _tail_lines(text: str, n: int = _LOG_TAIL_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join(lines[-n:])


def _read_log_tail(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    try:
        return _tail_lines(log_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""


def _count_pdf_pages_with_pdfinfo(pdf_path: Path) -> int:
    try:
        proc = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        log.debug("pdfinfo not available on PATH while counting pages for %s", pdf_path)
        return 0
    except Exception as exc:
        log.debug("pdfinfo failed for %s: %s", pdf_path, exc)
        return 0

    if proc.returncode != 0:
        log.debug("pdfinfo returned %s for %s: %s", proc.returncode, pdf_path, _tail_lines(proc.stdout))
        return 0

    match = _PDFINFO_PAGES_RE.search(proc.stdout or "")
    if not match:
        log.debug("Could not parse page count from pdfinfo output for %s", pdf_path)
        return 0
    return int(match.group(1))


def _count_pdf_pages_fallback(pdf_path: Path) -> int:
    """Fallback page counter when pdfinfo is unavailable."""
    try:
        data = pdf_path.read_bytes()
    except Exception:
        return 0
    # Count leaf pages, not /Pages nodes.
    return len(re.findall(rb"/Type\s*/Page(?!s)", data))


def count_pdf_pages(pdf_path: Path) -> int:
    pages = _count_pdf_pages_with_pdfinfo(pdf_path)
    if pages > 0:
        return pages
    return _count_pdf_pages_fallback(pdf_path)


def compile_tex(
    tex_content: str,
    output_pdf: Path,
    *,
    template_path: Path | None = None,
    debug_label: str = "latex",
) -> Tuple[bool, int]:
    """Compile LaTeX and return (success, pages)."""
    output_pdf = output_pdf.resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    temp_dir = output_pdf.parent / f"{output_pdf.stem}_build"
    temp_dir.mkdir(parents=True, exist_ok=True)
    tex_path = temp_dir / "resume.tex"
    log_path = temp_dir / "resume.log"
    generated_pdf = temp_dir / "resume.pdf"

    tex_path.write_text(tex_content, encoding="utf-8")
    cmd = [
        "pdflatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(temp_dir),
        str(tex_path),
    ]

    log.info(
        "[%s] pdflatex start template=%s cwd=%s expected_pdf=%s",
        debug_label,
        str(template_path.resolve()) if template_path else "<inline>",
        str(temp_dir.resolve()),
        str(output_pdf),
    )

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )
        stdout = proc.stdout or ""
    except Exception as exc:
        log.error("[%s] pdflatex invocation failed: %s", debug_label, exc)
        return False, 0

    pages = count_pdf_pages(generated_pdf) if generated_pdf.exists() else 0
    log.info(
        "[%s] pdflatex result rc=%s generated_pdf=%s exists=%s pages=%s",
        debug_label,
        proc.returncode,
        str(generated_pdf),
        generated_pdf.exists(),
        pages,
    )

    if proc.returncode != 0:
        tail = _read_log_tail(log_path) or _tail_lines(stdout)
        if tail:
            log.error("[%s] pdflatex failed (rc=%s). Last %s log lines:\n%s", debug_label, proc.returncode, _LOG_TAIL_LINES, tail)
        else:
            log.error("[%s] pdflatex failed (rc=%s) with no captured log output", debug_label, proc.returncode)
        return False, pages

    if not generated_pdf.exists():
        log.error("[%s] pdflatex reported success but generated PDF is missing at %s", debug_label, generated_pdf)
        return False, 0

    if pages <= 0:
        tail = _read_log_tail(log_path) or _tail_lines(stdout)
        log.error("[%s] Could not determine PDF page count for %s", debug_label, generated_pdf)
        if tail:
            log.error("[%s] Last %s log lines:\n%s", debug_label, _LOG_TAIL_LINES, tail)
        return False, 0

    shutil.copy2(generated_pdf, output_pdf)
    return True, pages


def _check_template(template_path: Path, output_pdf: Path) -> int:
    template_path = template_path.expanduser().resolve()
    output_pdf = output_pdf.expanduser().resolve()
    if not template_path.exists():
        print(f"Template not found: {template_path}")
        return 1

    tex_content = template_path.read_text(encoding="utf-8")
    ok, pages = compile_tex(
        tex_content,
        output_pdf,
        template_path=template_path,
        debug_label="check-template",
    )
    if ok and pages == 1:
        print(f"OK: compiled 1 page -> {output_pdf}")
        return 0
    print(f"FAIL: success={ok} pages={pages} output={output_pdf}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="HireAgent LaTeX renderer debug utilities")
    parser.add_argument("--check-template", type=Path, help="Compile and validate a fixed template")
    parser.add_argument("--output", type=Path, help="Output PDF path for --check-template")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if args.check_template:
        output_pdf = args.output or (Path.cwd() / "template_check.pdf")
        raise SystemExit(_check_template(args.check_template, output_pdf))

    parser.error("No action provided. Use --check-template PATH.")


if __name__ == "__main__":
    main()


__all__ = [
    "TEMPLATE_PATH",
    "load_template",
    "extract_bullets",
    "apply_summary",
    "apply_bullets",
    "escape_latex_text",
    "count_pdf_pages",
    "compile_tex",
]
