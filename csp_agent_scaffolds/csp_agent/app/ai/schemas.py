from pydantic import BaseModel, Field
from typing import Literal


class FieldConfidence(BaseModel):
    value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    page: int | None = None
    evidence: str | None = None  # short quote/location the model based this on


class VisualCheck(BaseModel):
    """For signature/stamp detection. Deliberately does NOT claim
    authenticity -- a vision model can say a signature-shaped mark exists
    on a page, never that it is genuine, that the signer had authority, or
    that the document wasn't altered. That gap is why `detected` is the
    only thing this model asserts, and why it always routes to human
    review rather than being treated as proof.
    """
    detected: bool | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    page: int | None = None
    notes: str | None = None


class AgreementExtraction(BaseModel):
    document_type: Literal["AGREEMENT"]
    csp_code: FieldConfidence
    csp_name: FieldConfidence
    agreement_start_date: FieldConfidence
    agreement_expiry_date: FieldConfidence
    signature: VisualCheck
    stamp: VisualCheck
    issues: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


class PoliceVerificationExtraction(BaseModel):
    document_type: Literal["POLICE_VERIFICATION"]
    csp_code: FieldConfidence
    verification_date: FieldConfidence
    expiry_date: FieldConfidence
    issuing_authority: FieldConfidence
    issues: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


class BCFCertificationExtraction(BaseModel):
    """IIBF (Indian Institute of Banking & Finance) Business
    Correspondent/Facilitator certificate. Unlike Agreement and
    PoliceVerification, this document type has NO embedded CSP code in the
    standard certificate layout -- matching it to a CSP relies on the
    sender's email/context, not a code found in the document itself. That
    means csp_code here is often null, and deterministic matching should
    fall back to the inbound email's already-identified CSP rather than
    requiring this field to be present.
    """
    document_type: Literal["IIBF_CERTIFICATION"]
    holder_name: FieldConfidence
    membership_number: FieldConfidence
    exam_name: FieldConfidence
    issue_date: FieldConfidence
    issues: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


class BlurryDocumentExtraction(BaseModel):
    document_type: Literal["REJECTED_BLURRY"] = "REJECTED_BLURRY"
    is_visible: bool = False
    is_blurry: bool = True
    rejection_reason: str = "DOCUMENT_BLURRY_OR_ILLEGIBLE: Text or image quality is too low or blurry to decipher."
    overall_confidence: float = 0.0


class UnknownDocument(BaseModel):
    document_type: Literal["UNKNOWN"]
    reason: str


UnknownDocumentExtraction = UnknownDocument
IIBFCertificationExtraction = BCFCertificationExtraction
