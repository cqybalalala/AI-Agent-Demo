"""
Document extraction using Qwen3-VL vision model via Ollama.
Supports invoices and payment receipts (PDF or image).
"""

import base64
import json
import re
from pathlib import Path

import config

VISION_MODEL = config.AGENT_MODEL

_MIME_MAP = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
    ".bmp":  "image/bmp",
    ".webp": "image/webp",
}

_INVOICE_PROMPT = """Extract structured data from this invoice document.
Return ONLY a valid JSON object with exactly these keys — no explanation, no markdown fences.

{
  "supplier_name": "string or null",
  "invoice_no": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "currency": "3-letter ISO code e.g. MYR USD",
  "items": [
    {
      "description": "string",
      "qty": number,
      "unit_price": number,
      "amount": number
    }
  ],
  "subtotal": number or null,
  "tax_amount": number or null,
  "total": number or null
}

Rules:
- currency must be the 3-letter ISO code found in the document.
- If a field is truly missing, use null.
- Dates must be YYYY-MM-DD format.
- Items must be a list even if only one item.
"""

_PAYMENT_RECEIPT_PROMPT = """Extract structured data from this payment receipt or bank transfer confirmation.
Return ONLY a valid JSON object with exactly these keys — no explanation, no markdown fences.

{
  "payment_date": "YYYY-MM-DD or null",
  "sender_name": "string or null",
  "sender_bank": "string or null",
  "receiver_name": "string or null",
  "receiver_bank": "string or null",
  "amount_paid": number or null,
  "currency": "3-letter ISO code e.g. MYR USD",
  "reference_no": "string or null",
  "exchange_rate": number or null,
  "original_amount": number or null,
  "original_currency": "3-letter ISO code or null"
}

Rules:
- If the payment was cross-currency (e.g. USD sent MYR received), fill both amount_paid/currency AND original_amount/original_currency.
- If a field is truly missing, use null.
- Dates must be YYYY-MM-DD format.
"""


def _pdf_to_images(file_bytes: bytes) -> list[bytes]:
    """Convert each PDF page to a PNG image (bytes)."""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("Install PyMuPDF: pip install pymupdf")
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        images.append(pix.tobytes("png"))
    return images


def _call_vision(image_bytes: bytes, mime_type: str, prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(
        base_url=config.OPENROUTER_BASE_URL,
        api_key=config.OPENROUTER_API_KEY,
        default_headers={"HTTP-Referer": "http://localhost:8501", "X-Title": "Treasury Agent"},
    )
    b64 = base64.b64encode(image_bytes).decode()
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _parse_json(raw: str) -> dict:
    raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in model response:\n{raw[:500]}")
    return json.loads(match.group())


def _extract(file_bytes: bytes, filename: str, prompt: str) -> dict:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        pages = _pdf_to_images(file_bytes)
        if not pages:
            raise ValueError("PDF has no pages.")
        # Use first page only for speed; most receipts/invoices are single-page
        raw = _call_vision(pages[0], "image/png", prompt)
    else:
        mime = _MIME_MAP.get(ext)
        if not mime:
            raise ValueError(f"Unsupported file type: {ext}")
        raw = _call_vision(file_bytes, mime, prompt)
    return _parse_json(raw)


def extract_invoice(file_bytes: bytes, filename: str) -> dict:
    """Invoice PDF or image → structured dict."""
    return _extract(file_bytes, filename, _INVOICE_PROMPT)


def extract_payment_receipt(file_bytes: bytes, filename: str) -> dict:
    """Payment receipt / bank transfer PDF or image → structured dict."""
    return _extract(file_bytes, filename, _PAYMENT_RECEIPT_PROMPT)
