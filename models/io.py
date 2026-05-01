from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class MultimodalVector(BaseModel):
    anxiety: float = Field(default=50.0, ge=0.0, le=100.0)
    confidence: float = Field(default=50.0, ge=0.0, le=100.0)


class EngineInput(BaseModel):
    stt_text: str
    multimodal: MultimodalVector = Field(default_factory=MultimodalVector)
    turn_number: int = 1


# ── LLM output schema ────────────────────────────────────────────────────────

class SlotEvaluationResult(BaseModel):
    slot_id: str
    fulfillment_score: float = Field(ge=0.0, le=1.0)
    evidence: str


class EmergentSlotData(BaseModel):
    slot_id: str
    proposition_statement: str
    verification_criteria: List[Dict[str, Any]] = []
    expected_answer_depth: str = "Conceptual"


class AbductionResult(BaseModel):
    observed_gap: str
    abductive_hypothesis: str
    emergent_slot: EmergentSlotData


class StateCommands(BaseModel):
    # PUSH: push emergent_slot; POP: pop verified slots; PRUNE: reset for topic switch
    stack_action: str
    pop_count: int = 0
    depth_increment: int = 1
    slots_to_pop: List[str] = []


class StrategyForSapien(BaseModel):
    logic_goal: str
    logical_gap_to_bridge: str
    interviewer_intent: str
    focus_point: str
    suggested_tone: str


class LLMAnalysisOutput(BaseModel):
    slot_evaluations: List[SlotEvaluationResult]
    abduction: Optional[AbductionResult] = None
    state_commands: StateCommands
    strategy: StrategyForSapien


# ── Final output to Sapien ────────────────────────────────────────────────────

class SapienStrategyPackage(BaseModel):
    interaction_strategy: Dict[str, Any]

    @classmethod
    def from_analysis(
        cls,
        strategy: StrategyForSapien,
        topic: str,
        depth_level: int,
        multimodal: MultimodalVector,
        topic_changed: bool = False,
    ) -> "SapienStrategyPackage":
        adjust_note = ""
        if multimodal.anxiety > 70:
            adjust_note = "사용자가 불안을 보이고 있습니다. 질문 수위를 조절하세요."
        elif multimodal.confidence < 30:
            adjust_note = "사용자의 자신감이 낮습니다. 유도 질문을 활용하세요."

        return cls(
            interaction_strategy={
                "logic_goal": strategy.logic_goal,
                "logical_gap_to_bridge": strategy.logical_gap_to_bridge,
                "interviewer_persona_guidance": {
                    "intent": strategy.interviewer_intent,
                    "emotion_direction": strategy.suggested_tone,
                    "focus_point": strategy.focus_point,
                },
                "current_context": {
                    "topic": topic,
                    "depth_level": depth_level,
                    "topic_changed": topic_changed,
                    "multimodal_feedback_requirement": adjust_note or None,
                },
            }
        )
