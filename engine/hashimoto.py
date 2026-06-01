from typing import List, Optional

from llm.client import HashimotoLLMClient
from llm.topic_extractor import extract_topics
from engine.state import (
    EngineState,
    effective_depth_threshold,
    resolved_context_str,
    resolved_history_list,
)
from models.io import (
    EngineInput,
    HashimotoConfig,
    LLMAnalysisOutput,
    SapienStrategyPackage,
)
from models.slot import Slot


class HashimotoEngine:
    """
    Main orchestrator: Multi-Slot Scanning → Stack Compaction →
    Abductive Branching → State Transition → Strategy Delivery.

    초기화 방법 세 가지:
      1. HashimotoEngine(root_topic="...")               # 단일 주제 직접 지정
      2. HashimotoEngine.from_topics([...])              # 주제 목록 직접 지정
      3. HashimotoEngine.from_resume(text, topic_count)  # 자기소개서에서 자동 추출

    세션 단위 튜닝은 config(HashimotoConfig)로 주입한다. 미지정 시 기본값 사용.
    """

    def __init__(
        self,
        root_topic: str,
        topic_queue: Optional[List[str]] = None,
        config: Optional[HashimotoConfig] = None,
    ) -> None:
        self.config: HashimotoConfig = config or HashimotoConfig()
        self.state = EngineState(root_topic, self.config)
        self._llm = HashimotoLLMClient(model=self.config.analysis_model)
        self._topic_queue: List[str] = list(topic_queue or [])
        # topic switch 후에도 소멸되지 않는 엔진 레벨 아카이브
        self._verified_archive: List[Slot] = []
        self._refused_archive: List[Slot] = []
        # 테스트·디버그용 — 마지막 턴의 중간 결과 보존
        self._last_analysis: Optional[LLMAnalysisOutput] = None
        self._last_prune_trigger: str = "none"  # "none" | "llm" | "system" | "depth" | "topic_count"
        # 모든 주제가 소진되어 더 전환할 곳이 없을 때 True (오케스트레이터 세션 종료 신호)
        self._session_exhausted: bool = False

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_topics(
        cls,
        topics: List[str],
        config: Optional[HashimotoConfig] = None,
    ) -> "HashimotoEngine":
        """주제 목록을 직접 지정하여 엔진을 초기화한다. 첫 주제가 root, 나머지는 큐."""
        if not topics:
            raise ValueError("topics는 최소 1개 이상이어야 합니다.")
        return cls(root_topic=topics[0], topic_queue=topics[1:], config=config)

    @classmethod
    def from_resume(
        cls,
        resume_text: str,
        topic_count: int = 3,
        config: Optional[HashimotoConfig] = None,
    ) -> "HashimotoEngine":
        """자기소개서를 분석하여 topic_count개의 root_topic을 생성하고 엔진을 초기화한다."""
        topics = extract_topics(resume_text, topic_count)
        return cls(root_topic=topics[0], topic_queue=topics[1:], config=config)

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

    @property
    def session_complete(self) -> bool:
        """모든 주제가 소진되어 더 전환할 주제가 없는 시점에 도달하면 True.

        오케스트레이터는 이 값이 True가 되면 면접 세션을 종료할 수 있다.
        (마지막 주제에서 주제 전환이 요구되었으나 큐가 비어있을 때 set 된다.)
        """
        return self._session_exhausted

    @property
    def verified_archive(self) -> List[Slot]:
        """충족(검증)으로 해소된 슬롯 누적 목록 (전 주제 포함)."""
        return list(self._verified_archive)

    @property
    def refused_archive(self) -> List[Slot]:
        """응답 거부로 해소된 슬롯 누적 목록 (전 주제 포함)."""
        return list(self._refused_archive)

    # ── Core ──────────────────────────────────────────────────────────────────

    def process_turn(self, engine_input: EngineInput) -> SapienStrategyPackage:
        # 1. Build slot context string (Multi-Slot Scanning window)
        active_slots = self.state.active_slots()
        active_slots_context = "\n".join(s.to_context_str() for s in active_slots)

        # 1b. 이미 해소된 명제 요약 (재질문 방지) — 이번 턴 해소분 적재 전 시점
        resolved_context = resolved_context_str(self._verified_archive, self._refused_archive)

        # 2. Single LLM call
        analysis = self._last_analysis = self._llm.analyze_turn(
            stt_text=engine_input.stt_text,
            active_slots_context=active_slots_context,
            depth_counter=self.state.depth_counter,
            anxiety=engine_input.multimodal.anxiety,
            confidence=engine_input.multimodal.confidence,
            resolved_context=resolved_context,
        )

        # 3. 슬롯 평가 반영 (response_type Python 교정 포함)
        self.state.apply_evaluations(analysis.slot_evaluations)

        # 3b. ANSWERED/REFUSED 슬롯 스택 제거 → 엔진 레벨 아카이브 적재
        verified, refused = self.state.process_slot_resolutions(analysis.slot_evaluations)
        self._verified_archive.extend(verified)
        self._refused_archive.extend(refused)

        # 4. Abductive Branching + State Transition
        # PUSH: INSUFFICIENT 부모 제거 + evidence 상속 + 연속 슬롯 push
        emergent = analysis.abduction.emergent_slot if analysis.abduction else None
        parent_slot_id = analysis.abduction.parent_slot_id if analysis.abduction else None
        self.state.apply_state_commands(analysis.state_commands, emergent, parent_slot_id)

        # 5. 두 단계 PRUNE 판정
        llm_pruned = analysis.state_commands.stack_action.upper() == "PRUNE"
        depth_threshold = effective_depth_threshold(
            engine_input.multimodal.anxiety,
            self.config.prune_depth_threshold,
            self.config.min_prune_depth_threshold,
            self.config.anxiety_adjust,
        )

        # 5a. root topic 전환 — LLM 명시 PRUNE 또는 주제 누적 질문 수 초과
        topic_switch_needed = llm_pruned or self.state.should_switch_topic(
            self.config.topic_question_threshold
        )

        # 5b. within-topic depth pruning — topic 전환 없이 낮은 depth 슬롯으로 복귀
        # apply_state_commands가 LLM PRUNE을 처리했다면 depth는 이미 0 → should_depth_prune() = False
        depth_prune_needed = (
            not topic_switch_needed
            and self.state.should_depth_prune(depth_threshold)
        )

        topic_changed = False
        transition_hint = None

        if depth_prune_needed:
            self.state.depth_prune()
            transition_hint = "direction_change"
            self._last_prune_trigger = "depth"
        elif topic_switch_needed:
            if self._topic_queue:
                next_topic = self._topic_queue.pop(0)
                self.state = EngineState(next_topic, self.config)
                topic_changed = True
            else:
                self.state.prune()
                self._session_exhausted = True
            self._llm.reset_history()
            self._last_prune_trigger = "llm" if llm_pruned else "topic_count"
        else:
            self._last_prune_trigger = "none"

        # 6. Package strategy for Sapien
        # resolved_history는 이번 턴 해소분까지 반영 (방금 답한 슬롯도 재질문 방지)
        return SapienStrategyPackage.from_analysis(
            strategy=analysis.strategy,
            topic=self.state.root_topic,
            depth_level=self.state.depth_counter,
            multimodal=engine_input.multimodal,
            topic_changed=topic_changed,
            transition_hint=transition_hint,
            resolved_history=resolved_history_list(self._verified_archive, self._refused_archive),
        )

    def reset(self, new_topic: str) -> None:
        self.state = EngineState(new_topic, self.config)
        self._llm.reset_history()
        self._topic_queue = []
        self._session_exhausted = False
