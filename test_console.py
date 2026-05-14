"""
Hashimoto Engine 대화형 테스트 콘솔.

두 가지 모드:
  1. 실제 LLM 모드  — Gemini API를 직접 호출 (크레딧 필요)
  2. 수동 입력 모드 — LLM에 보낼 프롬프트를 출력하고, 사용자가 JSON 응답을 직접 입력
"""

import json
import re
import sys
import traceback
from typing import List, Optional

from engine.hashimoto import HashimotoEngine
from engine.state import (
    PRUNE_DEPTH_THRESHOLD,
    MIN_PRUNE_DEPTH_THRESHOLD,
    TOPIC_QUESTION_THRESHOLD,
    effective_depth_threshold,
)
from llm.prompts import build_system_prompt, build_turn_message
from models.io import (
    EngineInput,
    LLMAnalysisOutput,
    MultimodalVector,
    SapienStrategyPackage,
)

# ── 터미널 컬러 ───────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()

def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if _TTY else t

def bold(t):    return _c("1", str(t))
def dim(t):     return _c("2", str(t))
def green(t):   return _c("32", str(t))
def cyan(t):    return _c("36", str(t))
def yellow(t):  return _c("33", str(t))
def red(t):     return _c("31", str(t))
def magenta(t): return _c("35", str(t))
def blue(t):    return _c("34", str(t))

W    = 66
SEP  = dim("─" * W)
DSEP = "═" * W


# ── 포맷 헬퍼 ────────────────────────────────────────────────────────────────

def header(title: str) -> None:
    print(f"\n{DSEP}\n  {bold(title)}\n{DSEP}")

def section(title: str) -> None:
    print(f"\n{SEP}\n  {cyan(title)}\n{SEP}")

def jdump(obj) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, ensure_ascii=False, indent=2)

def jprint(obj) -> None:
    print(jdump(obj))

def score_bar(score: float) -> str:
    filled = int(score * 10)
    bar = "█" * filled + "░" * (10 - filled)
    color = green if score >= 0.7 else (yellow if score >= 0.4 else red)
    return f"{color(f'{score:.2f}')} [{bar}]"

def threshold_label(anxiety: float) -> str:
    t = effective_depth_threshold(anxiety)
    label = str(t)
    if t < PRUNE_DEPTH_THRESHOLD:
        label += yellow(f"  ← anxiety={anxiety:.0f}로 {PRUNE_DEPTH_THRESHOLD}→{t} 조정")
    return label

def _read_multiline(prompt: str) -> str:
    """빈 줄 2회 연속으로 입력 종료."""
    print(prompt)
    lines, blank = [], 0
    while True:
        line = input()
        if line == "":
            blank += 1
            if blank >= 2:
                break
            lines.append(line)
        else:
            blank = 0
            lines.append(line)
    return "\n".join(lines).strip()

def _read_until_end(prompt: str) -> str:
    """'---' 단독 입력으로 종료 (JSON 입력용 — 빈 줄이 JSON 안에 있어도 무방)."""
    print(prompt)
    lines = []
    while True:
        line = input()
        if line.strip() == "---":
            break
        lines.append(line)
    return "\n".join(lines).strip()


# ═══════════════════════════════════════════════════════════════
# 수동 입력 모드 — Mock LLM Client
# ═══════════════════════════════════════════════════════════════

_SCHEMA_HINT = """\
  ┌ 출력 스키마 참고 ──────────────────────────────────────────────
  │ {
  │   "slot_evaluations": [
  │     {
  │       "slot_id": "<현재 슬롯 ID>",
  │       "fulfillment_score": 0.0~1.0,
  │       "evidence": "근거",
  │       "response_type": "ANSWERED|INSUFFICIENT|REFUSED"
  │     }
  │   ],
  │   "abduction": {               ← 필요 없으면 null
  │     "observed_gap": "...",
  │     "abductive_hypothesis": "...",
  │     "parent_slot_id": "<부모 슬롯 ID>",
  │     "emergent_slot": {
  │       "slot_id": "snake_case_id",
  │       "proposition_statement": "검증할 명제",
  │       "verification_criteria": [{"criterion": "기준", "weight": 0.5}],
  │       "expected_answer_depth": "Surface|Conceptual|Technical_Deep_Dive|Experiential"
  │     }
  │   },
  │   "state_commands": {
  │     "stack_action": "PUSH|NONE|PRUNE",
  │     "depth_increment": 1,
  │     "slots_to_pop": ["<검증된 슬롯 ID>"]
  │   },
  │   "strategy": {
  │     "logic_goal": "...",
  │     "logical_gap_to_bridge": "...",
  │     "interviewer_intent": "...",
  │     "focus_point": "...",
  │     "suggested_tone": "Analytical_Pressure|Neutral_Curious|Supportive|Skeptical_Professional"
  │   }
  │ }
  └───────────────────────────────────────────────────────────────"""



class MockHashimotoLLMClient:
    """
    실제 API 대신 사람이 LLM 역할을 하는 클라이언트.
    매 턴마다 LLM에 전달될 프롬프트(시스템 + 현재 슬롯 상태 + 현재 메시지)를
    복사하기 편한 형식으로 출력하고, 사용자가 입력한 JSON을 파싱해 반환한다.

    실제 클라이언트와 동일하게 stateless — 히스토리 없음.
    대화 맥락은 active_slots_context(슬롯 충족도·증거)가 담당한다.
    """

    _BLOCK = "━" * W

    def __init__(self) -> None:
        self._turn: int = 0

    def analyze_turn(
        self,
        stt_text: str,
        active_slots_context: str,
        depth_counter: int,
        anxiety: float,
        confidence: float,
    ) -> LLMAnalysisOutput:
        user_msg = build_turn_message(
            stt_text=stt_text,
            active_slots_context=active_slots_context,
            depth_counter=depth_counter,
            anxiety=anxiety,
            confidence=confidence,
        )
        self._turn += 1

        # ── 전체 프롬프트 출력 (복사용) ───────────────────────────────────
        print(f"\n{DSEP}")
        print(blue(bold(f"  ▶ LLM 전송 프롬프트 — Turn {self._turn}")))
        print(f"  {dim('아래 내용을 외부 LLM(Claude.ai 등)에 복사·붙여넣기 하세요.')}")
        print(DSEP)

        # SYSTEM
        print(f"\n{self._BLOCK}")
        print(blue("[ SYSTEM ]"))
        print(self._BLOCK)
        print(build_system_prompt())

        # 현재 유저 메시지 (슬롯 상태 + STT 포함)
        print(f"\n{self._BLOCK}")
        print(f"{blue('[ USER ]')}  {dim(f'Turn {self._turn}')}")
        print(self._BLOCK)
        print(user_msg)

        print(f"\n{DSEP}")
        print(blue(bold("  ▶ 프롬프트 끝 — 위 내용을 LLM에 넣고 응답을 아래에 붙여넣기")))
        print(DSEP)

        # ── 스키마 힌트 ──────────────────────────────────────────────────
        print(_SCHEMA_HINT)
        print(f"\n  {yellow('※ abduction 불필요 시 → \"abduction\": null')}")
        print(f"  {yellow('※ fulfillment_score ≥ 0.7 → VERIFIED 처리')}")

        # ── 사용자 입력 ───────────────────────────────────────────────────
        print(f"\n{bold('LLM 응답 JSON을 붙여넣고 마지막 줄에 --- 를 입력하세요:')}")

        while True:
            raw = _read_until_end("")
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
            if not raw:
                print(red("  입력이 비어있습니다. 다시 입력하세요."))
                continue
            try:
                data = json.loads(raw)
                result = LLMAnalysisOutput.model_validate(data)
                return result
            except json.JSONDecodeError as e:
                print(red(f"\n  [JSON 파싱 오류] {e}"))
                print("  다시 입력하세요. (--- 로 종료)\n")
            except Exception as e:
                print(red(f"\n  [스키마 검증 오류] {e}"))
                print(f"  {dim('필드명·타입·필수값을 확인하고 다시 입력하세요. (--- 로 종료)')}\n")

    def reset_history(self) -> None:
        self._turn = 0




# ═══════════════════════════════════════════════════════════════
# 수동 입력 모드 — Mock Engine
# ═══════════════════════════════════════════════════════════════

class MockHashimotoEngine(HashimotoEngine):
    """MockHashimotoLLMClient를 사용하는 테스트용 엔진. API 호출 없음."""

    def __init__(self, root_topic: str, topic_queue: Optional[List[str]] = None) -> None:
        super().__init__(root_topic, topic_queue)
        self._llm = MockHashimotoLLMClient()  # 실제 클라이언트를 목업으로 교체

    @classmethod
    def from_topics_manual(cls, topic_count: int = 3) -> "MockHashimotoEngine":
        """LLM 없이 사용자가 직접 주제를 입력하여 초기화."""
        print(f"\n면접 주제 {topic_count}개를 직접 입력하세요:")
        topics = []
        for i in range(topic_count):
            t = input(f"  주제 {i+1}: ").strip()
            if t:
                topics.append(t)
        if not topics:
            topics = ["소프트웨어 엔지니어링 경험"]
        while len(topics) < topic_count:
            topics.append(f"추가 주제 {len(topics)+1}")
        return cls(root_topic=topics[0], topic_queue=topics[1:])



# ═══════════════════════════════════════════════════════════════
# 공통 — 초기화
# ═══════════════════════════════════════════════════════════════

def select_mode() -> str:
    print(f"\n{DSEP}\n  {bold('Hashimoto Engine — 테스트 콘솔')}\n{DSEP}")
    print("\n실행 모드:")
    print(f"  {cyan('1')}. 실제 LLM 모드   — Gemini API 호출 (크레딧 필요)")
    print(f"  {cyan('2')}. 수동 입력 모드  — 내가 LLM 역할 (API 불필요)")
    while True:
        c = input("\n선택 [1/2]: ").strip()
        if c in ("1", "2"):
            return c
        print("1 또는 2를 입력하세요.")


def init_engine(mode: str) -> HashimotoEngine:
    print("\n초기화 방법:")
    if mode == "2":
        print(f"  {cyan('1')}. 주제 직접 입력")
        print(f"  {cyan('2')}. 자기소개서 붙여넣기  → LLM이 주제 추출  {red('(API 필요)')}")
    else:
        print(f"  {cyan('1')}. 주제 직접 입력")
        print(f"  {cyan('2')}. 자기소개서 붙여넣기  (LLM이 주제 자동 추출)")

    while True:
        choice = input("\n선택 [1/2]: ").strip()

        if choice == "1":
            if mode == "2":
                try:
                    count = int(input("주제 개수 [기본 1]: ").strip() or "1")
                except ValueError:
                    count = 1
                engine = MockHashimotoEngine.from_topics_manual(topic_count=count)
            else:
                topic = input("면접 주제: ").strip() or "소프트웨어 엔지니어링 경험"
                engine = HashimotoEngine(root_topic=topic)
            all_topics = [engine.current_topic] + engine.remaining_topics
            print(green("\n✓ 초기화 완료. 주제:"))
            for i, t in enumerate(all_topics, 1):
                mark = bold("▶") if i == 1 else " "
                print(f"  {mark} {i}. {t}")
            return engine

        elif choice == "2":
            if mode == "2":
                print(yellow("수동 입력 모드에서는 자기소개서 LLM 추출을 사용할 수 없습니다."))
                print("주제 직접 입력(1)을 선택하세요.")
                continue
            resume = _read_multiline("자기소개서를 붙여넣으세요 (빈 줄 2회로 종료):")
            if not resume:
                print(red("자기소개서가 비어있습니다."))
                continue
            try:
                count = int(input("추출할 주제 수 [기본 3]: ").strip() or "3")
            except ValueError:
                count = 3
            print(yellow("\n[주제 추출 중...]\n"))
            engine = HashimotoEngine.from_resume(resume, topic_count=count)
            all_topics = [engine.current_topic] + engine.remaining_topics
            print(green("✓ 추출된 주제:"))
            for i, t in enumerate(all_topics, 1):
                mark = bold("▶") if i == 1 else " "
                print(f"  {mark} {i}. {t}")
            return engine

        else:
            print("1 또는 2를 입력하세요.")


# ═══════════════════════════════════════════════════════════════
# 공통 — 상태 표시
# ═══════════════════════════════════════════════════════════════

def snapshot(engine: HashimotoEngine) -> dict:
    return {
        "topic":  engine.state.root_topic,
        "depth":  engine.state.depth_counter,
        "stack":  list(engine.state.priority_stack),
        "slot_scores": {
            sid: engine.state.global_slot_map[sid].fulfillment_score
            for sid in engine.state.priority_stack
            if sid in engine.state.global_slot_map
        },
    }


def show_state_brief(engine: HashimotoEngine, anxiety: float = 50.0) -> None:
    s = engine.state
    t = effective_depth_threshold(anxiety)
    depth_disp = f"{s.depth_counter} / {t}"
    if t < PRUNE_DEPTH_THRESHOLD:
        depth_disp += yellow(f"  (anxiety={anxiety:.0f}로 {PRUNE_DEPTH_THRESHOLD}→{t} 조정)")
    tqc_disp = f"{s.topic_question_counter} / {TOPIC_QUESTION_THRESHOLD}"
    if s.topic_question_counter >= TOPIC_QUESTION_THRESHOLD:
        tqc_disp += red("  ← 주제 전환 임계값 도달")
    print(f"  주제        : {bold(s.root_topic)}")
    print(f"  Depth       : {depth_disp}")
    print(f"  주제 질문수 : {tqc_disp}")
    stack_disp = " → ".join(s.priority_stack) if s.priority_stack else dim("(비어있음)")
    print(f"  Stack({len(s.priority_stack):02d})   : {stack_disp}")
    remaining = engine.remaining_topics
    print(f"  대기 주제   : {remaining if remaining else dim('(없음)')}")


def show_slot_evaluations(engine: HashimotoEngine) -> None:
    analysis = engine._last_analysis
    if not analysis or not analysis.slot_evaluations:
        print(f"  {dim('(평가된 슬롯 없음)')}")
        return
    for ev in analysis.slot_evaluations:
        rt = ev.response_type
        rt_color = green if rt == "ANSWERED" else (yellow if rt == "INSUFFICIENT" else red)
        print(f"\n  {bold(ev.slot_id)}")
        print(f"    score        : {score_bar(ev.fulfillment_score)}")
        print(f"    response_type: {rt_color(rt)}")
        print(f"    근거         : {dim(ev.evidence)}")


def show_abduction(engine: HashimotoEngine) -> None:
    analysis = engine._last_analysis
    if not analysis:
        return
    if analysis.abduction is None:
        print(f"  {dim('(가추 없음 — slot pop만으로 충분)')}")
        return
    ab = analysis.abduction
    es = ab.emergent_slot
    print(f"  공백     : {ab.observed_gap}")
    print(f"  가설     : {dim(ab.abductive_hypothesis)}")
    if ab.parent_slot_id:
        print(f"  부모슬롯 : {dim(ab.parent_slot_id)}")
    print(f"  신규슬롯 : {magenta(es.slot_id)}")
    print(f"  명제     : {es.proposition_statement}")
    print(f"  깊이     : {es.expected_answer_depth}")
    if es.verification_criteria:
        print("  기준     :")
        for c in es.verification_criteria:
            print(f"    • {c.get('criterion','')}  (weight={c.get('weight','')})")


def show_state_commands(engine: HashimotoEngine) -> None:
    analysis = engine._last_analysis
    if not analysis:
        return
    cmd = analysis.state_commands
    color_map = {"PUSH": magenta, "PRUNE": red, "NONE": dim}
    action_fn = color_map.get(cmd.stack_action.upper(), str)
    print(f"  stack_action    : {action_fn(cmd.stack_action)}")
    print(f"  depth_increment : {cmd.depth_increment}")
    if cmd.slots_to_pop:
        print(f"  slots_to_pop    : {cmd.slots_to_pop}")


def show_strategy(engine: HashimotoEngine) -> None:
    analysis = engine._last_analysis
    if not analysis:
        return
    s = analysis.strategy
    print(f"  logic_goal            : {s.logic_goal}")
    print(f"  logical_gap_to_bridge : {s.logical_gap_to_bridge}")
    print(f"  interviewer_intent    : {dim(s.interviewer_intent)}")
    print(f"  focus_point           : {s.focus_point}")
    print(f"  suggested_tone        : {cyan(s.suggested_tone)}")


def show_prune_verdict(engine: HashimotoEngine, before: dict, anxiety: float) -> None:
    analysis = engine._last_analysis
    trigger   = engine._last_prune_trigger
    threshold = effective_depth_threshold(anxiety)
    depth_was = before["depth"]
    llm_action = analysis.state_commands.stack_action if analysis else "?"
    print(f"  LLM stack_action    : {bold(llm_action)}")
    print(f"  anxiety             : {anxiety:.0f}")
    print(f"  effective threshold : {threshold}  (base={PRUNE_DEPTH_THRESHOLD}, min={MIN_PRUNE_DEPTH_THRESHOLD})")
    print(f"  depth (처리 전)     : {depth_was}")
    if trigger == "llm":
        print(f"  판정                : {red('LLM PRUNE 발동')}")
    elif trigger == "system":
        print(f"  판정                : {red('시스템 강제 PRUNE')}  (depth {depth_was} ≥ threshold {threshold})")
    elif trigger == "depth":
        print(f"  판정                : {yellow('Depth PRUNE')}  (depth {depth_was} ≥ threshold {threshold})")
    elif trigger == "topic_count":
        print(f"  판정                : {yellow('주제 전환')}  (topic_question_counter ≥ {TOPIC_QUESTION_THRESHOLD})")
    else:
        print(f"  판정                : {green('전환 없음')}")


def show_stack_detail(engine: HashimotoEngine) -> None:
    state = engine.state
    if not state.priority_stack:
        print(f"  {dim('(스택 비어있음)')}")
        return
    for i, sid in enumerate(state.priority_stack):
        slot = state.global_slot_map.get(sid)
        if not slot:
            continue
        label = bold("▶ TOP") if i == 0 else f"  [{i}]"
        status_color = green if slot.status.value == "VERIFIED" else yellow
        print(f"\n  {label}  {bold(sid)}")
        print(f"         status    : {status_color(slot.status.value)}  score={score_bar(slot.fulfillment_score)}")
        print(f"         명제      : {slot.proposition_statement}")
        print(f"         깊이      : {slot.expected_answer_depth}")
        print(f"         생성 depth: {slot.depth_at_creation}")
        if slot.parent_slot_id:
            print(f"         부모슬롯  : {dim(slot.parent_slot_id)}")
        if slot.evidence:
            print(f"         근거      : {dim(slot.evidence)}")
        if slot.verification_criteria:
            print("         기준      :")
            for c in slot.verification_criteria:
                print(f"           • {c.criterion}  (weight={c.weight})")


def show_archive(engine: HashimotoEngine) -> None:
    verified = engine._verified_archive
    refused = engine._refused_archive
    if not verified and not refused:
        print(f"  {dim('(아카이브 비어있음)')}")
        return
    if verified:
        print(f"\n  {green(bold(f'검증 완료 [{len(verified)}]'))}")
        for slot in verified:
            print(f"    • {bold(slot.slot_id)}  score={score_bar(slot.fulfillment_score)}")
            print(f"      명제: {slot.proposition_statement}")
            if slot.parent_slot_id:
                print(f"      부모: {dim(slot.parent_slot_id)}")
    if refused:
        print(f"\n  {red(bold(f'응답 거부 [{len(refused)}]'))}")
        for slot in refused:
            print(f"    • {bold(slot.slot_id)}")
            print(f"      명제: {slot.proposition_statement}")
            if slot.evidence:
                print(f"      근거: {dim(slot.evidence)}")


def show_full_turn(
    engine: HashimotoEngine,
    before: dict,
    anxiety: float,
    package: SapienStrategyPackage,
) -> None:
    section("① 처리 전 상태")
    print(f"  주제   : {bold(before['topic'])}")
    print(f"  Depth  : {before['depth']} / {threshold_label(anxiety)}")
    print(f"  Stack  : {' → '.join(before['stack']) if before['stack'] else dim('(비어있음)')}")
    if before["slot_scores"]:
        print("  Scores :")
        for sid, sc in before["slot_scores"].items():
            print(f"    {sid}: {score_bar(sc)}")

    section("② LLM 분석 — 슬롯 평가")
    show_slot_evaluations(engine)

    section("② LLM 분석 — 가추법")
    show_abduction(engine)

    section("② LLM 분석 — 상태 명령")
    show_state_commands(engine)

    section("② LLM 분석 — 전략")
    show_strategy(engine)

    section("③ 주제 전환 판정")
    show_prune_verdict(engine, before, anxiety)

    section("④ 처리 후 상태")
    show_state_brief(engine, anxiety)

    section("⑤ Sapien 전달 패키지")
    jprint(package)


# ═══════════════════════════════════════════════════════════════
# 도움말
# ═══════════════════════════════════════════════════════════════

def show_help(mode: str) -> None:
    cmds = [
        ("state",        "현재 상태 요약"),
        ("stack",        "Priority Stack 전체 슬롯 상세"),
        ("slot <id>",    "특정 슬롯 전체 필드 조회"),
        ("topics",       "주제 목록 및 진행 현황"),
        ("archive",      "검증/거부 슬롯 아카이브 조회"),
        ("llm",          "마지막 LLM 분석 원시 JSON"),
        ("sapien",       "마지막 Sapien 패키지 원시 JSON"),
        ("sysprompt",    "시스템 프롬프트 전체 출력"),
        ("threshold",    "불안도별 depth 임계값 테이블"),
        ("reset",        "엔진 재초기화"),
        ("help",         "이 도움말"),
        ("exit / quit",  "종료"),
    ]
    mode_label = "수동 입력" if mode == "2" else "실제 LLM"
    print(f"\n  {bold(f'현재 모드: {mode_label}')}")
    print(f"  {bold('STT 텍스트를 입력하면 턴을 처리합니다.')}")
    print(f"\n  {'명령':<22} 설명")
    print(f"  {'─'*22} {'─'*32}")
    for cmd, desc in cmds:
        print(f"  {cyan(cmd):<22} {desc}")


# ═══════════════════════════════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    mode = select_mode()
    engine = init_engine(mode)
    turn = 1
    last_package: Optional[SapienStrategyPackage] = None
    last_anxiety = 50.0

    mode_tag = yellow("[수동]") if mode == "2" else green("[LLM]")
    print(f"\n{dim('명령어 목록: help')}")

    while True:
        t = effective_depth_threshold(last_anxiety)
        depth_disp = f"{engine.state.depth_counter}/{t}"
        if t < PRUNE_DEPTH_THRESHOLD:
            depth_disp += yellow("*")

        print(
            f"\n{DSEP}\n"
            f"  {mode_tag}  Turn {bold(turn)}  │  {bold(engine.current_topic)}"
            f"  │  depth {depth_disp}\n"
            f"{DSEP}"
        )

        raw = input("\n> ").strip()
        if not raw:
            continue

        cmd = raw.lower()

        if cmd in ("exit", "quit", "종료"):
            print("테스트 종료.")
            break

        elif cmd == "help":
            show_help(mode)

        elif cmd == "state":
            section("현재 엔진 상태")
            show_state_brief(engine, last_anxiety)

        elif cmd == "stack":
            section("Priority Stack 상세")
            show_stack_detail(engine)

        elif cmd.startswith("slot "):
            sid = raw[5:].strip()
            slot = engine.state.global_slot_map.get(sid)
            if slot:
                section(f"슬롯: {sid}")
                jprint(slot)
            else:
                print(red(f"  슬롯 '{sid}' 없음."))
                ids = list(engine.state.global_slot_map.keys())
                print(f"  등록된 슬롯: {ids or dim('(없음)')}")

        elif cmd == "topics":
            section("주제 현황")
            print(f"  현재  : {bold(engine.current_topic)}")
            remaining = engine.remaining_topics
            if remaining:
                print("  대기  :")
                for i, t in enumerate(remaining, 1):
                    print(f"    {i}. {t}")
            else:
                print(f"  대기  : {dim('(마지막 주제)')}")

        elif cmd == "archive":
            section("슬롯 아카이브")
            show_archive(engine)

        elif cmd == "llm":
            if engine._last_analysis:
                section("마지막 LLM 분석 (raw JSON)")
                jprint(engine._last_analysis)
            else:
                print(dim("  아직 처리된 턴 없음."))

        elif cmd == "sapien":
            if last_package:
                section("마지막 Sapien 패키지 (raw JSON)")
                jprint(last_package)
            else:
                print(dim("  아직 처리된 턴 없음."))

        elif cmd == "sysprompt":
            section("시스템 프롬프트")
            for line in build_system_prompt().splitlines():
                print(f"  {line}")

        elif cmd == "threshold":
            section("불안도 → depth 임계값 테이블")
            print(f"  {'anxiety':>8}  {'threshold':>10}  변화")
            print(f"  {'─'*8}  {'─'*10}  {'─'*6}")
            for a in range(0, 101, 10):
                th = effective_depth_threshold(float(a))
                diff = "" if th == PRUNE_DEPTH_THRESHOLD else yellow(f"↓{PRUNE_DEPTH_THRESHOLD - th}")
                print(f"  {a:>8}  {th:>10}  {diff}")

        elif cmd == "reset":
            engine = init_engine(mode)
            turn = 1
            last_package = None
            last_anxiety = 50.0

        else:
            # ── STT 입력으로 턴 처리 ──────────────────────────────────────
            stt_text = raw
            print("\n멀티모달 신호 입력 (Enter = 기본값 50):")
            try:
                a = float(input("  anxiety    [0-100]: ").strip() or "50")
            except ValueError:
                a = 50.0
            try:
                c = float(input("  confidence [0-100]: ").strip() or "50")
            except ValueError:
                c = 50.0
            a = max(0.0, min(100.0, a))
            c = max(0.0, min(100.0, c))
            last_anxiety = a

            before = snapshot(engine)

            # 수동 모드: 프롬프트 출력 + JSON 입력은 MockLLMClient 내부에서 처리
            if mode == "2":
                header(f"Turn {turn} — 프롬프트 출력 및 응답 입력")
            else:
                header(f"Turn {turn} 처리 중 ...")

            try:
                package = engine.process_turn(
                    EngineInput(
                        stt_text=stt_text,
                        multimodal=MultimodalVector(anxiety=a, confidence=c),
                        turn_number=turn,
                    )
                )
                last_package = package
                show_full_turn(engine, before, a, package)
                turn += 1

            except Exception as e:
                print(red(f"\n[오류] {e}"))
                traceback.print_exc()


if __name__ == "__main__":
    main()
