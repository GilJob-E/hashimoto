import math
from typing import Dict, List, Optional, Tuple
from models.slot import Slot, SlotStatus, VerificationCriterion
from models.io import (
    EmergentSlotData,
    HashimotoConfig,
    SlotEvaluationResult,
    StateCommands,
)


# 기본값 — HashimotoConfig의 default와 동일하게 유지한다 (하위호환·표시용 상수).
FULFILLMENT_THRESHOLD = 0.7
# How many top slots the LLM considers simultaneously (Multi-Slot Scanning)
ACTIVE_WINDOW = 3
# Depth limit before Pruning triggers (base value, may be reduced by anxiety)
PRUNE_DEPTH_THRESHOLD = 5
# Floor for anxiety-adjusted threshold
MIN_PRUNE_DEPTH_THRESHOLD = 2
# Total questions per root topic before triggering topic switch
TOPIC_QUESTION_THRESHOLD = 10


def effective_depth_threshold(
    anxiety: float,
    base: int = PRUNE_DEPTH_THRESHOLD,
    floor: int = MIN_PRUNE_DEPTH_THRESHOLD,
    adjust: bool = True,
) -> int:
    """불안도가 높을수록 depth 임계값을 낮춰 주제를 더 빨리 전환한다.

    anxiety 0–50  : base (기본 5) — 변화 없음
    anxiety 50–100: 선형으로 floor (기본 2)까지 감소

    base/floor 기본값은 모듈 상수이며, 세션별 HashimotoConfig 값을 넘기면
    해당 세션 설정으로 계산한다. adjust=False면 불안도와 무관하게 base를 반환.

    anxiety=75  → 4  |  anxiety=87.5 → 3  |  anxiety=100 → 2  (base=5, floor=2 기준)
    """
    if not adjust or anxiety <= 50.0:
        return base
    scale = (anxiety - 50.0) / 50.0
    return max(floor, math.ceil(base - scale * (base - floor)))


# ── 해소 슬롯 요약 (재질문 방지용 — 분석/질문 LLM 공용) ────────────────────────

def build_resolved_records(
    verified: List[Slot], refused: List[Slot]
) -> List[Tuple[str, str, str]]:
    """(slot_id, proposition_statement, status) 튜플 목록. status ∈ {검증됨, 거부됨}."""
    records: List[Tuple[str, str, str]] = []
    for s in verified:
        records.append((s.slot_id, s.proposition_statement, "검증됨"))
    for s in refused:
        records.append((s.slot_id, s.proposition_statement, "거부됨"))
    return records


def resolved_context_str(verified: List[Slot], refused: List[Slot]) -> str:
    """분석 LLM용 — slot_id 포함 간결 요약. 비어있으면 빈 문자열."""
    return "\n".join(
        f"- [{status}] {sid}: {prop}"
        for sid, prop, status in build_resolved_records(verified, refused)
    )


def resolved_history_list(verified: List[Slot], refused: List[Slot]) -> List[dict]:
    """질문 LLM 패키지용 — proposition + status만 (토큰 절감, slot_id 제외)."""
    return [
        {"proposition": prop, "status": status}
        for _sid, prop, status in build_resolved_records(verified, refused)
    ]


class EngineState:
    """
    핵심 상태 변수를 관리한다.
      - Global_Slot_Map : slot_id → Slot
      - Priority_Stack  : LIFO 슬롯 ID 목록 (인덱스 0 = 최상단)
      - Depth_Counter   : 현재 루트 토픽 내 연속 질문 횟수
    """

    def __init__(self, root_topic: str, config: Optional[HashimotoConfig] = None) -> None:
        self.root_topic: str = root_topic
        self.config: HashimotoConfig = config or HashimotoConfig()
        self.global_slot_map: Dict[str, Slot] = {}
        self.priority_stack: List[str] = []
        self.depth_counter: int = 0
        self.topic_question_counter: int = 0

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
        """root topic 전환을 위해 스택을 전체 초기화한다."""
        self.priority_stack = self.priority_stack[:keep_top]
        self.depth_counter = 0

    def depth_prune(self) -> Optional[Slot]:
        """깊은 브랜치 슬롯을 제거하고 가장 낮은 depth의 슬롯으로 복귀한다.
        root_topic과 topic_question_counter는 유지된다.
        복귀 대상 슬롯을 반환하고, 없으면 None을 반환한다.
        """
        if not self.priority_stack:
            return None
        valid_ids = [sid for sid in self.priority_stack if sid in self.global_slot_map]
        if not valid_ids:
            return None
        min_depth = min(self.global_slot_map[sid].depth_at_creation for sid in valid_ids)
        self.priority_stack = [
            sid for sid in self.priority_stack
            if sid in self.global_slot_map
            and self.global_slot_map[sid].depth_at_creation <= min_depth
        ]
        top = self.peek()
        self.depth_counter = top.depth_at_creation if top else 0
        return top

    def active_slots(self) -> List[Slot]:
        """Multi-Slot Scanning에서 참조할 상위 N개 슬롯."""
        window_ids = self.priority_stack[:self.config.active_window]
        return [self.global_slot_map[sid] for sid in window_ids if sid in self.global_slot_map]

    def peek(self) -> Optional[Slot]:
        if not self.priority_stack:
            return None
        return self.global_slot_map.get(self.priority_stack[0])

    # ── State update from LLM output ─────────────────────────────────────────

    def apply_evaluations(self, evaluations: List[SlotEvaluationResult]) -> None:
        """Multi-Slot Scanning 결과를 Global_Slot_Map에 반영한다.
        Python 보정: LLM이 ANSWERED로 분류했더라도 score < threshold 이면 INSUFFICIENT로 교정.
        """
        threshold = self.config.fulfillment_threshold
        for ev in evaluations:
            if ev.slot_id not in self.global_slot_map:
                continue
            slot = self.global_slot_map[ev.slot_id]
            slot.fulfillment_score = ev.fulfillment_score
            slot.evidence = ev.evidence
            # Python-enforced override: ANSWERED + 낮은 점수 → INSUFFICIENT
            effective_type = ev.response_type.upper()
            if effective_type == "ANSWERED" and not slot.is_verified(threshold):
                effective_type = "INSUFFICIENT"
            slot.resolution_type = effective_type
            if slot.is_verified(threshold):
                slot.status = SlotStatus.VERIFIED

    def process_slot_resolutions(
        self, evaluations: List[SlotEvaluationResult]
    ) -> Tuple[List[Slot], List[Slot]]:
        """ANSWERED(충족) / REFUSED(거부) 슬롯을 스택에서 제거하고 반환한다.
        INSUFFICIENT 슬롯은 apply_state_commands()의 PUSH 흐름에서 처리한다.
        반환: (verified_slots, refused_slots) — 엔진 레벨 아카이브에 적재.
        """
        verified: List[Slot] = []
        refused: List[Slot] = []
        for ev in evaluations:
            slot = self.global_slot_map.get(ev.slot_id)
            if not slot or ev.slot_id not in self.priority_stack:
                continue
            eff = (slot.resolution_type or "").upper()
            if eff == "ANSWERED":
                slot.resolution_type = "VERIFIED"
                self.priority_stack.remove(ev.slot_id)
                verified.append(slot)
            elif eff == "REFUSED":
                self.priority_stack.remove(ev.slot_id)
                refused.append(slot)
        return verified, refused

    def apply_state_commands(
        self,
        commands: StateCommands,
        emergent_slot_data: Optional[EmergentSlotData],
        parent_slot_id: Optional[str] = None,
    ) -> None:
        """Abductive Branching + State Transition.
        슬롯 제거(ANSWERED/REFUSED)는 process_slot_resolutions()가 사전에 처리한다.
        여기서는 PUSH(연속 슬롯 생성 + INSUFFICIENT 부모 제거)와 PRUNE만 담당한다.
        """
        action = commands.stack_action.upper()

        self.topic_question_counter += 1

        if action == "PUSH" and emergent_slot_data:
            # 부모 슬롯 특정: LLM이 명시한 parent_slot_id 우선, 없으면 스택 최상단
            parent: Optional[Slot] = None
            if parent_slot_id and parent_slot_id in self.global_slot_map:
                parent = self.global_slot_map[parent_slot_id]
            elif self.priority_stack:
                parent = self.global_slot_map.get(self.priority_stack[0])

            # INSUFFICIENT 부모를 스택에서 제거 (global_slot_map은 계보 참조용으로 보존)
            if parent and parent.slot_id in self.priority_stack:
                self.priority_stack.remove(parent.slot_id)

            # 부모의 evidence를 연속 슬롯에 상속
            inherited_evidence: Optional[str] = None
            if parent:
                parts = [f"[부모 명제] {parent.proposition_statement}"]
                parts.append(f"[달성도] {parent.fulfillment_score:.2f}")
                if parent.verification_criteria:
                    crit_str = " / ".join(
                        f"{c.criterion}(weight={c.weight})"
                        for c in parent.verification_criteria
                    )
                    parts.append(f"[미충족 기준] {crit_str}")
                if parent.evidence:
                    parts.append(f"[기존 응답 근거] {parent.evidence}")
                inherited_evidence = " | ".join(parts)

            criteria = [
                VerificationCriterion(
                    criterion=c.criterion,
                    weight=float(c.weight),
                )
                for c in emergent_slot_data.verification_criteria
            ]
            new_slot = Slot(
                slot_id=emergent_slot_data.slot_id,
                proposition_statement=emergent_slot_data.proposition_statement,
                verification_criteria=criteria,
                expected_answer_depth=emergent_slot_data.expected_answer_depth,
                parent_slot_id=parent.slot_id if parent else None,
                evidence=inherited_evidence,
            )
            self.push(new_slot)

        if action == "PRUNE":
            self.prune()
        else:
            self.depth_counter += commands.depth_increment

    # ── Pruning checks ────────────────────────────────────────────────────────

    def should_prune(self, threshold: int = PRUNE_DEPTH_THRESHOLD) -> bool:
        """depth_counter가 임계값에 도달했으나 스택에 다른 depth 레벨 슬롯이 없을 때 True.
        (= within-topic depth_prune이 불가능한 상황의 fallback)
        """
        return self.depth_counter >= threshold and bool(self.priority_stack)

    def should_depth_prune(self, threshold: int) -> bool:
        """depth_counter가 임계값에 도달했고, 스택에 더 낮은 depth의 슬롯이 존재할 때 True.
        이 조건이 충족되면 root_topic을 유지한 채 depth_prune()으로 복귀한다.
        """
        if self.depth_counter < threshold:
            return False
        if len(self.priority_stack) < 2:
            return False
        depths = [
            self.global_slot_map[sid].depth_at_creation
            for sid in self.priority_stack
            if sid in self.global_slot_map
        ]
        return len(set(depths)) > 1

    def should_switch_topic(self, threshold: int = TOPIC_QUESTION_THRESHOLD) -> bool:
        """현재 root_topic에 대한 누적 질문 수가 임계값에 도달하면 True."""
        return self.topic_question_counter >= threshold

    # ── Debug / display ───────────────────────────────────────────────────────

    def summary(self) -> str:
        active = self.active_slots()
        lines = [
            f"=== Hashimoto Engine State ===",
            f"Root Topic: {self.root_topic}",
            f"Depth Counter: {self.depth_counter}",
            f"Topic Question Count: {self.topic_question_counter}",
            f"Stack size: {len(self.priority_stack)}",
            "",
            "Active Slots (top window):",
        ]
        for slot in active:
            lines.append(f"  {slot.to_context_str()}")
        if not active:
            lines.append("  (empty)")
        return "\n".join(lines)
