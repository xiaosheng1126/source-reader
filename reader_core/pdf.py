from __future__ import annotations

import pathlib
import sys
from typing import Any

from reader_core.models import ReaderOutput
from reader_core.optional import VENDOR_DIR
from reader_core.utils import cap_text, token_policy


def _load_pdf_reader() -> type[Any] | None:
    if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(VENDOR_DIR))
    try:
        from pypdf import PdfReader  # type: ignore[import]
    except ImportError:
        return None
    return PdfReader


def read_local_pdf(path: pathlib.Path, max_chars: int) -> ReaderOutput:
    reader_cls = _load_pdf_reader()
    metadata: dict[str, object] = {
        "suffix": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "requires_external_upload": False,
    }
    if reader_cls is None:
        return ReaderOutput(
            input_type="file",
            source_type="pdf",
            title=path.stem,
            local_path=str(path),
            read_quality="partial",
            strategy="local_pdf_missing_pypdf",
            token_policy=token_policy(max_chars, False),
            content=(
                "检测到本地 PDF，但未安装轻量 PDF 文本读取依赖 pypdf。"
                "可以安装到项目本地 vendor 后重试；不会默认上传 PDF 到在线服务。"
            ),
            metadata=metadata,
            errors=["pypdf not installed"],
        )

    try:
        reader = reader_cls(str(path))
        pages = list(getattr(reader, "pages", []))
        page_texts = []
        for index, page in enumerate(pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                page_texts.append(f"\n\n[Page {index}]\n{text.strip()}")
    except Exception as exc:  # pypdf exposes parser errors as several exception types.
        return ReaderOutput(
            input_type="file",
            source_type="pdf",
            title=path.stem,
            local_path=str(path),
            read_quality="failed",
            strategy="local_pdf_pypdf_failed",
            token_policy=token_policy(max_chars, False),
            content="PDF 文本读取失败。文件可能已损坏、加密，或使用了 pypdf 不支持的结构。",
            metadata=metadata,
            errors=[f"pypdf failed: {exc}"],
        )

    metadata["page_count"] = len(pages)
    raw_text = "".join(page_texts).strip()
    if not raw_text:
        return ReaderOutput(
            input_type="file",
            source_type="pdf",
            title=path.stem,
            local_path=str(path),
            read_quality="partial",
            strategy="local_pdf_no_extractable_text",
            token_policy=token_policy(max_chars, False),
            content=(
                "PDF 没有可提取文本，可能是扫描件或图片型 PDF。"
                "当前阶段不做 OCR；如需在线模型解析，需要由用户显式选择上传。"
            ),
            metadata=metadata,
            errors=["pdf has no extractable text"],
        )

    content, clipped = cap_text(raw_text, max_chars)
    return ReaderOutput(
        input_type="file",
        source_type="pdf",
        title=path.stem,
        local_path=str(path),
        read_quality="basic",
        strategy="local_pdf_pypdf_text_extraction",
        token_policy=token_policy(max_chars, clipped),
        content=content,
        metadata=metadata,
    )
