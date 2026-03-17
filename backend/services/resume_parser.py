"""Resume parsing service - extracts text from PDF and DOCX files."""
import os
import re
import logging

logger = logging.getLogger(__name__)


def parse_resume(file_path: str) -> str:
    """Extract text from a resume file (PDF or DOCX)."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return _parse_pdf(file_path)
    elif ext in (".docx", ".doc"):
        return _parse_docx(file_path)
    elif ext == ".txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def _clean_pdf_text(text: str) -> str:
    """Clean up common PDF extraction artifacts."""
    # Fix words broken by spaces (e.g., "Direct or" -> "Director", "Manag emen t" -> "Management")
    # This pattern finds lowercase letter + space + lowercase letter sequences that are likely broken words
    # Replace unicode replacement chars and common garbled chars
    text = text.replace('\ufffd', '•').replace('�', '•')

    # Remove excessive whitespace between characters that are clearly part of one word
    # Pattern: word fragments separated by \n + spaces (common in column-layout PDFs)
    lines = text.split('\n')
    cleaned_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # If line is a continuation of previous (starts with lowercase, no bullet)
        if line and cleaned_lines and not line.startswith('•') and not line.startswith('-'):
            prev = cleaned_lines[-1]
            # If previous line ends mid-word (no punctuation) and this line starts lowercase
            if prev and not prev.endswith(('.', ':', ',', ';', '!', '?')) and line[0].islower():
                cleaned_lines[-1] = prev + ' ' + line
                i += 1
                continue
        if line:
            cleaned_lines.append(line)
        i += 1

    text = '\n'.join(cleaned_lines)

    # Fix broken words: sequences like "Manag emen t" "Direct or" "Technic al"
    # Look for patterns where a space breaks a word that should be continuous
    text = re.sub(r'(\w{2,})\s+(\w{1,4})\b(?=\s|[.,;:!?\-\n]|$)', _try_merge_word, text)

    # Collapse multiple spaces
    text = re.sub(r'  +', ' ', text)
    # Collapse multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def _try_merge_word(match):
    """Try to merge two fragments if they form a real word pattern."""
    part1 = match.group(1)
    part2 = match.group(2)
    merged = part1 + part2

    # Common suffixes that indicate broken words
    common_suffixes = ['ment', 'tion', 'sion', 'ing', 'ity', 'ness', 'ure', 'ous',
                       'ive', 'ble', 'ful', 'less', 'al', 'or', 'er', 'ed', 'ly',
                       'ment', 'ance', 'ence', 'ent', 'ant', 'ary', 'ory', 'ion',
                       'ty', 'cy', 'ry', 'ce', 'se', 'te', 'ne', 'le', 'ge', 'de']

    if part2.lower() in common_suffixes:
        return merged

    # Don't merge if both parts look like separate words (both > 4 chars)
    if len(part1) > 4 and len(part2) > 3:
        return match.group(0)

    return match.group(0)  # Don't merge by default


def _parse_pdf(file_path: str) -> str:
    """Extract text from PDF using pdfplumber (much better than PyPDF2), with fallback."""
    # Try pdfplumber first (best quality)
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text(
                    x_tolerance=2,
                    y_tolerance=2,
                ) or ""
                text += page_text + "\n"
        if text.strip():
            logger.info(f"PDF parsed with pdfplumber: {len(text)} chars")
            return _clean_pdf_text(text)
    except ImportError:
        logger.info("pdfplumber not available, trying PyPDF2")
    except Exception as e:
        logger.warning(f"pdfplumber failed: {e}, trying PyPDF2")

    # Fallback to PyPDF2
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
            text += "\n"
        return _clean_pdf_text(text)
    except Exception as e:
        logger.error(f"PDF parse failed: {e}")
        return ""


def _parse_docx(file_path: str) -> str:
    try:
        from docx import Document
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.error(f"DOCX parse failed: {e}")
        return ""
