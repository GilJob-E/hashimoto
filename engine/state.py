import math
from typing import Dict, List, Optional
from models.slot import Slot, SlotStatus, VerificationCriterion
from models.io import EmergentSlotData, SlotEvaluationResult, StateCommands


FULFILLMENT_THRESHOLD = 0.7
# How many top slots the LLM considers simultaneously (Multi-Slot Scanning)
ACTIVE_WINDOW = 3
# Depth limit before Pruning triggers (base value, may be reduced by anxiety)
PRUNE_DEPTH_THRESHOLD = 5
# Floor for anxiety-adjusted threshold
MIN_PRUNE_DEPTH_THRESHOLD = 2


def effective_depth_threshold(anxiety: float) -> int:
    """불안도가 높을수록 depth 임계값을 낮춰 주제를 더 빨리 전환한다.

    anxiety 0–50  : PRUNE_DEPTH_THRESHOLD (5) — 변화 없음
    anxiety 50–100: 선형으로 MIN_PRUNE_DEPTH_THRESHOLD (2)까지 감소

    anxiety=75  → 4  |  anxiety=87.5 → 3  |  anxiety=100 → 2
    """
    if anxiety <= 50.0:
        return PRUNE_DEPTH_THRESHOLD
    scale = (anxiety - 50.0) / 50.0
    return max(
        MIN_PRUNE_DEPTH_THRESHOLD,
        math.ceil(PRUNE_DEPTH_THRESHOLD - scale * (PRUNE_DEPTH_THRESHOLD - MIN_PRUNE_DEPTH_THRESHOLD)),
    )


class EngineState:
    """
    핵심 상태 변수를 관리한다.
      - Global_Slot_Map : slot_id → Slot
      - Priority_Stack  : LIFO 슬롯 ID 목록 (인덱스 0 = 최상단)
      - Depth_Counter   : 현재 루트 토픽 내 연속 질문 횟수
    """

    def __init__(self, root_topic: str) -> None:
        self.root_topic: str = root_topic
        self.global_slot_map: Dict[str, Slot] = {}
        self.priority_stack: List[str] = []
        self.depth_counter: int = 0

    # ── Stack helpers ─────────────────────────────────────────────────────────

    def push(self, slot: Slot) -> None:
        slot.depth_at_creation = self.depth_counter
        self.global_slot_map[slot.slot_id] = slot
        self.priority_stack.insert(0, slot.slot_id)

    def pop_verified(self, slot_ids: List[str]) -> List[Slot]:
        popped: List[Slot] = []
        for sid in slot_ids:
            if sid in self.priority_stack:
                self.priority_stack.remove(sid)
            if sid in self.global_slot_map:
                popped.append(self.global_slot_map[sid])
        return popped

    def prune(self, keep_top: int = 0) -> None:
        """주제 전환을 위해 스택 상위 노드를 정리한다."""
        self.priority_stack = self.priority_stack[:keep_top]
        self.depth_counter = 0

    def active_slots(self) -> List[Slot]:
        """Multi-Slot Scanning에서 참조할 상위 N개 슬롯."""
        window_ids = self.priority_stack[:ACTIVE_WINDOW]
        return [self.global_slot_map[sid] for sid in window_ids if sid in self.global_slot_map]

    def peek(self) -> Optional[Slot]:
        if not self.priority_stack:
            return None
        return self.global_slot_map.get(self.priority_stack[0])

    # ── State update from LLM output ─────────────────────────────────────────

    def apply_evaluations(self, evaluations: List[SlotEvaluationResult]) -> None:
        """Multi-Slot Scanning 결과를 Global_Slot_Map에 반영한다."""
        for ev in evaluations:
            if ev.slot_id in self.global_slot_map:
                slot = self.global_slot_map[ev.slot_id]
                slot.fulfillment_score = ev.fulfillment_score
                slot.evidence = ev.evidence
                if slot.is_verified(FULFILLMENT_THRESHOLD):
                    slot.status = SlotStatus.VERIFIED

    def apply_state_commands(
        self,
        commands: StateCommands,
        emergent_slot_data: Optional[EmergentSlotData],
    ) -> None:
        """Stack Compaction + Abductive Branching + State Transition."""
        action = commands.stack_action.upper()

        if action in ("POP", "PUSH"):
            self.pop_verified(commands.slots_to_pop)

        if action == "PUSH" and emergent_slot_data:
            criteria = [
                VerificationCriterion(
                    criterion=c.get("criterion", ""),
                    weight=float(c.get("weight", 0.5)),
                )
                for c in emergent_slot_data.verification_criteria
            ]
            new_slot = Slot(
                slot_id=emergent_slot_data.slot_id,
                proposition_statement=emergent_slot_data.proposition_statement,
                verification_criteria=criteria,
                expected_answer_depth=emergent_slot_data.expected_answer_depth,
            )
            self.push(new_slot)

        if action == "PRUNE":
            self.prune()
        else:
            self.depth_counter += commands.depth_increment

    # ── Pruning check ─────────────────────────────────────────────────────────

    def should_prune(self, threshold: int = PRUNE_DEPTH_THRESHOLD) -> bool:
        return self.depth_counter >= threshold and bool(self.priority_stack)

    # ── Debug / display ───────────────────────────────────────────────────────

    def summary(self) -> str:
        active = self.active_slots()
        lines = [
            f"=== Hashimoto Engine State ===",
            f"Root Topic: {self.root_topic}",
            f"Depth Counter: {self.depth_counter}",
            f"Stack size: {len(self.priority_stack)}",
            "",
            "Active Slots (top window):",
        ]
        for slot in active:
            lines.append(f"  {slot.to_context_str()}")
        if not active:
            lines.append("  (empty)")
        return "\n".join(lines)
