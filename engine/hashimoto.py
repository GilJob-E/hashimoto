from typing import List, Optional

from llm.client import HashimotoLLMClient
from llm.topic_extractor import extract_topics
from engine.state import EngineState, effective_depth_threshold
from models.io import EngineInput, LLMAnalysisOutput, SapienStrategyPackage


class HashimotoEngine:
    """
    Main orchestrator: Multi-Slot Scanning → Stack Compaction →
    Abductive Branching → State Transition → Strategy Delivery.

    초기화 방법 두 가지:
      1. HashimotoEngine(root_topic="...")               # 단일 주제 직접 지정
      2. HashimotoEngine.from_resume(text, topic_count)  # 자기소개서에서 자동 추출
    """

    def __init__(
        self,
        root_topic: str,
        topic_queue: Optional[List[str]] = None,
    ) -> None:
        self.state = EngineState(root_topic)
        self._llm = HashimotoLLMClient()
        self._topic_queue: List[str] = list(topic_queue or [])
        # 테스트·디버그용 — 마지막 턴의 중간 결과 보존
        self._last_analysis: Optional[LLMAnalysisOutput] = None
        self._last_prune_trigger: str = "none"  # "none" | "llm" | "system"

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_resume(cls, resume_text: str, topic_count: int = 3) -> "HashimotoEngine":
        """자기소개서를 분석하여 topic_count개의 root_topic을 생성하고 엔진을 초기화한다."""
        topics = extract_topics(resume_text, topic_count)
        return cls(root_topic=topics[0], topic_queue=topics[1:])

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def current_topic(self) -> str:
        return self.state.root_topic

    @property
    def remaining_topics(self) -> List[str]:
        return list(self._topic_queue)

    @property
    def all_topics_exhausted(self) -> bool:
        """현재 주제가 마지막이고 큐가 비어있으면 True."""
        return not self._topic_queue

    # ── Core ──────────────────────────────────────────────────────────────────

    def process_turn(self, engine_input: EngineInput) -> SapienStrategyPackage:
        # 1. Build slot context string (Multi-Slot Scanning window)
        active_slots = self.state.active_slots()
        active_slots_context = "\n".join(s.to_context_str() for s in active_slots)

        # 2. Single LLM call
        analysis = self._last_analysis = self._llm.analyze_turn(
            stt_text=engine_input.stt_text,
            active_slots_context=active_slots_context,
            depth_counter=self.state.depth_counter,
            anxiety=engine_input.multimodal.anxiety,
            confidence=engine_input.multimodal.confidence,
        )

        # 3. Stack Compaction
        self.state.apply_evaluations(analysis.slot_evaluations)

        # 4. Abductive Branching + State Transition
        emergent = analysis.abduction.emergent_slot if analysis.abduction else None
        self.state.apply_state_commands(analysis.state_commands, emergent)

        # 5. 주제 전환 판정 — LLM 출력과 독립적으로 depth 기반 시스템 체크
        llm_pruned = analysis.state_commands.stack_action.upper() == "PRUNE"
        # apply_state_commands가 LLM PRUNE을 처리했다면 depth는 이미 0 → should_prune() = False
        system_prune = not llm_pruned and self.state.should_prune(
            effective_depth_threshold(engine_input.multimodal.anxiety)
        )

        self._last_prune_trigger = "llm" if llm_pruned else ("system" if system_prune else "none")

        topic_changed = False
        if llm_pruned or system_prune:
            if self._topic_queue:
                next_topic = self._topic_queue.pop(0)
                self.state = EngineState(next_topic)
                topic_changed = True
            else:
                # 남은 주제 없음 — 현재 상태만 초기화
                self.state.prune()
            self._llm.reset_history()

        # 6. Package strategy for Sapien
        return SapienStrategyPackage.from_analysis(
            strategy=analysis.strategy,
            topic=self.state.root_topic,
            depth_level=self.state.depth_counter,
            multimodal=engine_input.multimodal,
            topic_changed=topic_changed,
        )

    def reset(self, new_topic: str) -> None:
        self.state = EngineState(new_topic)
        self._llm.reset_history()
        self._topic_queue = []
