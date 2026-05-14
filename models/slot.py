from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional


class SlotStatus(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"


class VerificationCriterion(BaseModel):
    criterion: str
    weight: float = Field(ge=0.0, le=1.0)


class Slot(BaseModel):
    slot_id: str
    proposition_statement: str
    verification_criteria: List[VerificationCriterion] = []
    expected_answer_depth: str = "Conceptual"
    status: SlotStatus = SlotStatus.PENDING
    fulfillment_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: Optional[str] = None
    depth_at_creation: int = 0
    # 연속 슬롯 계보 추적
    parent_slot_id: Optional[str] = None
    # 아카이브 기록용: "VERIFIED" | "REFUSED" (INSUFFICIENT 슬롯은 아카이브 없음)
    resolution_type: Optional[str] = None

    def is_verified(self, threshold: float = 0.7) -> bool:
        return self.fulfillment_score >= threshold

    def to_context_str(self) -> str:
        criteria = "\n  ".join(
            f"- {c.criterion} (weight: {c.weight})"
            for c in self.verification_criteria
        )
        evidence_line = f"Evidence So Far: {self.evidence}" if self.evidence else "Evidence So Far: (none)"
        parent_line = f"Derived From: {self.parent_slot_id}\n" if self.parent_slot_id else ""
        return (
            f"[Slot ID: {self.slot_id}]\n"
            f"{parent_line}"
            f"Proposition: {self.proposition_statement}\n"
            f"Status: {self.status.value} (score: {self.fulfillment_score:.2f})\n"
            f"Expected Depth: {self.expected_answer_depth}\n"
            f"Verification Criteria:\n  {criteria if criteria else '(none)'}\n"
            f"{evidence_line}"
        )
