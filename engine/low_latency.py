"""
저지연 모드 Hashimoto 엔진.

Sapien은 STT 도착 즉시 latest_strategy를 읽어 질문을 생성하고,
Hashimoto의 분석은 백그라운드에서 비동기로 실행되어 다음 턴 전에 strategy를 갱신한다.

사용 패턴:
    async with LowLatencyHashimotoEngine.from_resume(text, 3) as engine:
        engine.submit_turn(input1)          # non-blocking
        strategy = engine.latest_strategy   # 즉시 반환 (이전 턴 결과)
        # Sapien이 strategy + STT로 질문 생성 ...

        engine.submit_turn(input2)
        strategy = engine.latest_strategy   # 직전 분석 완료분
"""

import asyncio
import logging
from typing import Callable, List, Optional

from llm.async_client import AsyncHashimotoLLMClient
from llm.topic_extractor import extract_topics
from engine.state import (
    EngineState,
    effective_depth_threshold,
    resolved_context_str,
    resolved_history_list,
)
from models.io import EngineInput, HashimotoConfig, SapienStrategyPackage
from models.slot import Slot

logger = logging.getLogger(__name__)

# strategy 갱신 시 호출되는 콜백 타입
StrategyCallback = Callable[[SapienStrategyPackage], None]


class LowLatencyHashimotoEngine:
    """
    저지연 모드 엔진.

    - submit_turn(): non-blocking. 분석 요청을 내부 큐에 넣고 즉시 반환.
    - latest_strategy: 가장 최근에 완료된 SapienStrategyPackage. 언제든 블로킹 없이 읽기 가능.
    - 큐는 FIFO 단일 워커로 소비 → 상태 갱신에 락 없음, 경쟁 없음.
    - on_strategy_ready 콜백으로 push 방식 연동도 지원.
    """

    def __init__(
        self,
        root_topic: str,
        topic_queue: Optional[List[str]] = None,
        on_strategy_ready: Optional[StrategyCallback] = None,
        config: Optional[HashimotoConfig] = None,
    ) -> None:
        self.config: HashimotoConfig = config or HashimotoConfig()
        self.state = EngineState(root_topic, self.config)
        self._llm = AsyncHashimotoLLMClient(model=self.config.analysis_model)
        self._topic_queue: List[str] = list(topic_queue or [])
        # topic switch 후에도 소멸되지 않는 엔진 레벨 아카이브
        self._verified_archive: List[Slot] = []
        self._refused_archive: List[Slot] = []
        # 모든 주제 소진 시 True (오케스트레이터 세션 종료 신호)
        self._session_exhausted: bool = False

        self._latest_strategy: Optional[SapienStrategyPackage] = None
        self._on_strategy_ready = on_strategy_ready
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_resume(
        cls,
        resume_text: str,
        topic_count: int = 3,
        on_strategy_ready: Optional[StrategyCallback] = None,
        config: Optional[HashimotoConfig] = None,
    ) -> "LowLatencyHashimotoEngine":
        """자기소개서에서 주제를 추출하여 저지연 엔진을 초기화한다."""
        topics = extract_topics(resume_text, topic_count)
        return cls(
            root_topic=topics[0],
            topic_queue=topics[1:],
            on_strategy_ready=on_strategy_ready,
            config=config,
        )

    @classmethod
    def from_topics(
        cls,
        topics: List[str],
        on_strategy_ready: Optional[StrategyCallback] = None,
        config: Optional[HashimotoConfig] = None,
    ) -> "LowLatencyHashimotoEngine":
        """주제 목록을 직접 지정하여 저지연 엔진을 초기화한다."""
        if not topics:
            raise ValueError("topics는 최소 1개 이상이어야 합니다.")
        return cls(
            root_topic=topics[0],
            topic_queue=topics[1:],
            on_strategy_ready=on_strategy_ready,
            config=config,
        )

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "LowLatencyHashimotoEngine":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """백그라운드 분석 워커를 시작한다. 이벤트 루프 진입 후 호출."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="hashimoto-worker")
        logger.info("[Hashimoto] 저지연 워커 시작. 주제: %s", self.state.root_topic)

    async def stop(self) -> None:
        """워커를 정리한다. 큐에 남은 항목은 처리하지 않는다."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("[Hashimoto] 워커 종료.")

    # ── Public Interface ──────────────────────────────────────────────────────

    @property
    def latest_strategy(self) -> Optional[SapienStrategyPackage]:
        """가장 최근에 완료된 전략. 첫 턴 분석 완료 전에는 None."""
        return self._latest_strategy

    @property
    def current_topic(self) -> str:
        return self.state.root_topic

    @property
    def remaining_topics(self) -> List[str]:
        return list(self._topic_queue)

    @property
    def session_complete(self) -> bool:
        """모든 주제가 소진되어 더 전환할 주제가 없으면 True (세션 종료 신호)."""
        return self._session_exhausted

    @property
    def verified_archive(self) -> List[Slot]:
        """충족(검증)으로 해소된 슬롯 누적 목록 (전 주제 포함)."""
        return list(self._verified_archive)

    @property
    def refused_archive(self) -> List[Slot]:
        """응답 거부로 해소된 슬롯 누적 목록 (전 주제 포함)."""
        return list(self._refused_archive)

    @property
    def is_busy(self) -> bool:
        """큐에 미처리 항목이 있거나 워커가 처리 중이면 True."""
        return not self._queue.empty()

    def submit_turn(self, engine_input: EngineInput) -> None:
        """STT 도착 즉시 호출. 분석을 큐에 넣고 즉시 반환 (블로킹 없음)."""
        self._queue.put_nowait(engine_input)

    async def flush(self) -> Optional[SapienStrategyPackage]:
        """큐의 모든 항목 처리가 완료될 때까지 대기한다. 테스트·종료 전 사용."""
        await self._queue.join()
        return self._latest_strategy

    # ── Background Worker ─────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while self._running:
            try:
                engine_input = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                pkg = await self._run_pipeline(engine_input)
                self._latest_strategy = pkg
                if self._on_strategy_ready:
                    self._on_strategy_ready(pkg)
            except Exception:
                logger.exception("[Hashimoto] 파이프라인 실패. 기존 strategy 유지.")
            finally:
                self._queue.task_done()

    async def _run_pipeline(self, engine_input: EngineInput) -> SapienStrategyPackage:
        # 1. 활성 슬롯 컨텍스트 구성
        active_slots = self.state.active_slots()
        active_slots_context = "\n".join(s.to_context_str() for s in active_slots)

        # 1b. 이미 해소된 명제 요약 (재질문 방지) — 이번 턴 해소분 적재 전 시점
        resolved_context = resolved_context_str(self._verified_archive, self._refused_archive)

        # 2. LLM 비동기 호출
        analysis = await self._llm.analyze_turn(
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
        depth_prune_needed = (
            not topic_switch_needed
            and self.state.should_depth_prune(depth_threshold)
        )

        topic_changed = False
        transition_hint = None

        if depth_prune_needed:
            self.state.depth_prune()
            transition_hint = "direction_change"
            logger.info("[Hashimoto] within-topic depth prune → %s", self.state.root_topic)
        elif topic_switch_needed:
            trigger = "LLM" if llm_pruned else f"topic_count({self.state.topic_question_counter})"
            if self._topic_queue:
                next_topic = self._topic_queue.pop(0)
                self.state = EngineState(next_topic, self.config)
                topic_changed = True
                logger.info("[Hashimoto] 주제 전환(%s) → %s", trigger, next_topic)
            else:
                self.state.prune()
                self._session_exhausted = True
                logger.info("[Hashimoto] 주제 소진(%s). 현재 상태 초기화.", trigger)
            self._llm.reset_history()

        return SapienStrategyPackage.from_analysis(
            strategy=analysis.strategy,
            topic=self.state.root_topic,
            depth_level=self.state.depth_counter,
            multimodal=engine_input.multimodal,
            topic_changed=topic_changed,
            transition_hint=transition_hint,
            resolved_history=resolved_history_list(self._verified_archive, self._refused_archive),
        )
