"""
Public interface used by the rest of the app. Business logic (LangGraph
nodes, reminder rules, escalation logic) must only ever import from here --
never from app.ai.providers.* directly. Swapping providers later
(OpenRouter, a local model) means changing AI_PROVIDER and adding one new
module under app/ai/providers/, nothing else.
"""
from ..config import AI_PROVIDER
from .schemas import (
    AgreementExtraction, PoliceVerificationExtraction, BCFCertificationExtraction, UnknownDocument, BlurryDocumentExtraction
)

if AI_PROVIDER in ("groq", "nvidia"):
    from .providers import groq_provider as _provider
elif AI_PROVIDER == "gemini":
    from .providers import gemini as _provider
else:
    raise ValueError(f"Unknown AI_PROVIDER: {AI_PROVIDER}")


def validate_document(
    document_text: str = "",
    expected_type: str | None = None,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
):
    """Returns a validated Pydantic object, or raises ValueError if the
    provider returned something unusable. Callers must catch ValueError and
    route to manual review -- never let a bad AI response crash the pipeline.
    """
    raw = _provider.extract_fields(
        document_text=document_text,
        expected_type=expected_type,
        file_bytes=file_bytes,
        mime_type=mime_type,
    )
    doc_type = raw.get("document_type")
    if doc_type == "REJECTED_BLURRY":
        return BlurryDocumentExtraction(
            document_type="REJECTED_BLURRY",
            is_visible=False,
            is_blurry=True,
            rejection_reason=raw.get("rejection_reason", "DOCUMENT_BLURRY_OR_ILLEGIBLE"),
            overall_confidence=0.0
        )
    if doc_type == "AGREEMENT":
        return AgreementExtraction(**raw)
    if doc_type == "POLICE_VERIFICATION":
        return PoliceVerificationExtraction(**raw)
    if doc_type == "IIBF_CERTIFICATION":
        return BCFCertificationExtraction(**raw)
    return UnknownDocument(document_type="UNKNOWN", reason=raw.get("reason", "unrecognized document"))



def draft_message(template_name: str, context: dict) -> str:
    return _provider.draft_message(template_name, context)
