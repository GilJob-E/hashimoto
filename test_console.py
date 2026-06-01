"""
Hashimoto Engine 대화형 테스트 콘솔.

모든 턴은 실제 Gemini API를 호출하여 STT → Hashimoto 분석 → State 업데이트 →
질문LLM 프롬프트 주입 → 다음 질문 생성까지 end-to-end로 실행된다. (크레딧 필요)
"""

import json
import sys
import time
import traceback
from dataclasses import dataclass
from typing import List, Optional

from engine.hashimoto import HashimotoEngine
from engine.state import (
    PRUNE_DEPTH_THRESHOLD,
    MIN_PRUNE_DEPTH_THRESHOLD,
    TOPIC_QUESTION_THRESHOLD,
    effective_depth_threshold,
)
from llm.prompts import build_system_prompt
from models.io import (
    EngineInput,
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


# ═══════════════════════════════════════════════════════════════
# Latency 측정 — 파이프라인 단계별
# ═══════════════════════════════════════════════════════════════

@dataclass
class TurnLatency:
    turn: int
    context_build_ms: float    # ① active_slots 컨텍스트 문자열 빌드
    hashimoto_llm_ms: float    # ② analyze_turn 전체 (명제 생성·분석 LLM 포함)
    state_update_ms: float     # ③ apply_evaluations + apply_state_commands + PRUNE + packaging
    question_prompt_ms: float  # ④ 질문LLM 프롬프트 빌드
    question_llm_ms: float     # ⑤ 질문LLM 호출

    @property
    def stt_to_question_prompt_ms(self) -> float:
        """STT 수신 → 질문LLM 프롬프트 완성까지 (핵심 파이프라인 지연)."""
        return (
            self.context_build_ms
            + self.hashimoto_llm_ms
            + self.state_update_ms
            + self.question_prompt_ms
        )

    @property
    def total_ms(self) -> float:
        return self.stt_to_question_prompt_ms + self.question_llm_ms


class LatencyTracker:
    """파이프라인 직렬화 latency를 턴별로 기록하고 통계를 제공."""

    def __init__(self) -> None:
        self._records: List[TurnLatency] = []

    def record(self, r: TurnLatency) -> None:
        self._records.append(r)

    def last(self) -> Optional[TurnLatency]:
        return self._records[-1] if self._records else None

    @property
    def count(self) -> int:
        return len(self._records)

    def _stats(self, values: List[float]) -> dict:
        n = len(values)
        if n == 0:
            return {}
        sv = sorted(values)
        return {
            "min": sv[0],
            "avg": sum(values) / n,
            "p50": sv[n // 2],
            "p95": sv[min(int(0.95 * n), n - 1)],
            "max": sv[-1],
        }

    def show_table(self) -> None:
        if not self._records:
            print(f"  {dim('(측정된 턴 없음)')}")
            return

        hdr = (
            f"  {'Turn':>4}  {'Total':>7}  {'①CTX':>6}"
            f"  {'②H-LLM':>8}  {'③State':>7}"
            f"  {'④Q-Prm':>7}  {'⑤Q-LLM':>8}  {'STT→Prm':>9}"
        )
        print(f"\n{hdr}")
        sep = f"  {'─'*4}  {'─'*7}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*9}"
        print(sep)
        for r in self._records:
            tc = green if r.total_ms < 5000 else (yellow if r.total_ms < 12000 else red)
            print(
                f"  {r.turn:>4}"
                f"  {tc(f'{r.total_ms:>6.0f}')} "
                f"  {r.context_build_ms:>6.1f}"
                f"  {r.hashimoto_llm_ms:>8.0f}"
                f"  {r.state_update_ms:>7.1f}"
                f"  {r.question_prompt_ms:>7.2f}"
                f"  {r.question_llm_ms:>8.0f}"
                f"  {r.stt_to_question_prompt_ms:>9.0f}"
            )

        cols = [
            ([r.total_ms                    for r in self._records], "Total(ms)"),
            ([r.context_build_ms            for r in self._records], "①CTX build"),
            ([r.hashimoto_llm_ms            for r in self._records], "②H-LLM"),
            ([r.state_update_ms             for r in self._records], "③State upd"),
            ([r.question_prompt_ms          for r in self._records], "④Q-Prompt"),
            ([r.question_llm_ms             for r in self._records], "⑤Q-LLM"),
            ([r.stt_to_question_prompt_ms   for r in self._records], "STT→Q-Prm ★"),
        ]
        n = len(self._records)
        print(f"\n  {bold(f'집계 통계 (n={n})')}")
        print(f"  {'단계':<14}  {'min':>7}  {'avg':>7}  {'p50':>7}  {'p95':>7}  {'max':>7}")
        print(f"  {'─'*14}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*7}")
        for values, label in cols:
            s = self._stats(values)
            if not s:
                continue
            row = (
                f"  {label:<14}  {s['min']:>7.0f}  {s['avg']:>7.0f}"
                f"  {s['p50']:>7.0f}  {s['p95']:>7.0f}  {s['max']:>7.0f}"
            )
            print(bold(row) if "★" in label else row)


def _show_latency_line(lat: TurnLatency) -> None:
    def _ms(v: float, warn: float = 5000, crit: float = 10000) -> str:
        color = green if v < warn else (yellow if v < crit else red)
        return color(f"{v:.1f} ms")

    print(f"  ① Context 빌드     : {_ms(lat.context_build_ms,   50,  200)}")
    print(f"  ② Hashimoto LLM    : {_ms(lat.hashimoto_llm_ms)}")
    print(f"  ③ State 업데이트   : {_ms(lat.state_update_ms,    50,  200)}")
    print(f"  ④ 질문프롬프트 빌드: {_ms(lat.question_prompt_ms,  5,   20)}")
    print(f"  ⑤ 질문 LLM        : {_ms(lat.question_llm_ms)}")
    print(f"  {bold('STT → 질문프롬프트')} : "
          f"{_ms(lat.stt_to_question_prompt_ms, 5000, 12000)}")
    print(f"  {bold('Total'):<20}: {_ms(lat.total_ms, 5000, 15000)}")


def wrap_engine_timing(engine: HashimotoEngine) -> HashimotoEngine:
    """analyze_turn과 process_turn을 monkey-patch하여 단계별 타이밍을 엔진에 저장.

    engine._get_engine_timing() → {"context_build_ms", "hashimoto_llm_ms", "state_update_ms"}
    """
    _t_proc_start: List[float] = [0.0]
    _t_llm_start:  List[float] = [0.0]
    _t_llm_end:    List[float] = [0.0]
    _t_proc_end:   List[float] = [0.0]

    orig_analyze = engine._llm.analyze_turn

    def timed_analyze(*args, **kwargs):
        _t_llm_start[0] = time.perf_counter()
        result = orig_analyze(*args, **kwargs)
        _t_llm_end[0] = time.perf_counter()
        return result

    engine._llm.analyze_turn = timed_analyze

    orig_process = engine.process_turn

    def timed_process(engine_input):
        _t_proc_start[0] = time.perf_counter()
        _t_llm_start[0] = _t_llm_end[0] = 0.0
        result = orig_process(engine_input)
        _t_proc_end[0] = time.perf_counter()
        return result

    engine.process_turn = timed_process

    def _get_engine_timing() -> dict:
        t0 = _t_proc_start[0]
        ls = _t_llm_start[0]
        le = _t_llm_end[0]
        te = _t_proc_end[0]
        if t0 == 0.0 or ls == 0.0:
            return {"context_build_ms": 0.0, "hashimoto_llm_ms": 0.0, "state_update_ms": 0.0}
        return {
            "context_build_ms": (ls - t0) * 1000,
            "hashimoto_llm_ms": (le - ls) * 1000,
            "state_update_ms":  (te - le) * 1000,
        }

    engine._get_engine_timing = _get_engine_timing
    return engine


# ═══════════════════════════════════════════════════════════════
# 질문 LLM — 프롬프트 빌드 + 클라이언트
# ═══════════════════════════════════════════════════════════════

def build_question_prompt(package: SapienStrategyPackage) -> str:
    """SapienStrategyPackage → 질문LLM에 주입할 프롬프트."""
    s = package.interaction_strategy
    g = s.get("interviewer_persona_guidance", {})
    c = s.get("current_context", {})

    lines = [
        "당신은 전문 면접관입니다. 아래 전략 지침을 바탕으로 면접 질문 하나를 생성하세요.",
        "",
        f"[현재 주제]    {c.get('topic', '')}",
        f"[면접 목표]    {s.get('logic_goal', '')}",
        f"[논리 간극]    {s.get('logical_gap_to_bridge', '')}",
        f"[면접 의도]    {g.get('intent', '')}",
        f"[집중 포인트]  {g.get('focus_point', '')}",
        f"[감정 방향]    {g.get('emotion_direction', '')}",
    ]
    if c.get("topic_changed"):
        lines.append("[주의] 주제가 전환되었습니다. 자연스럽게 전환 질문을 생성하세요.")
    if c.get("multimodal_feedback_requirement"):
        lines.append(f"[피드백 요구] {c['multimodal_feedback_requirement']}")

    resolved = c.get("resolved_history") or []
    if resolved:
        lines.append("")
        lines.append("[이미 질문·답변된 내용 — 재질문 금지]")
        for r in resolved:
            lines.append(f"- ({r.get('status', '')}) {r.get('proposition', '')}")

    lines += [
        "",
        "규칙:",
        "- 한국어로 면접 질문 하나만 출력하세요.",
        "- 서문·설명·부연 없이 질문문 단독으로 출력합니다.",
        "- 위 '이미 질문·답변된 내용'과 중복되는 질문은 절대 생성하지 마세요.",
    ]
    return "\n".join(lines)


class QuestionLLMClient:
    """SapienStrategyPackage 기반 면접 질문을 Gemini로 자동 생성."""

    _SYSTEM = "당신은 한국어 면접 질문을 생성하는 전문 면접관 AI입니다."

    def __init__(self) -> None:
        import os
        from google import genai
        from google.genai import types
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._config = types.GenerateContentConfig(
            system_instruction=self._SYSTEM,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

    def generate(self, prompt: str) -> str:
        response = self._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=self._config,
        )
        return response.text.strip()


# ═══════════════════════════════════════════════════════════════
# 초기화
# ═══════════════════════════════════════════════════════════════

def init_engine() -> HashimotoEngine:
    print(f"\n{DSEP}\n  {bold('Hashimoto Engine — 테스트 콘솔 (API end-to-end)')}\n{DSEP}")
    print("\n초기화 방법:")
    print(f"  {cyan('1')}. 주제 직접 입력")
    print(f"  {cyan('2')}. 자기소개서 붙여넣기  (LLM이 주제 자동 추출)")

    while True:
        choice = input("\n선택 [1/2]: ").strip()

        if choice == "1":
            topic = input("면접 주제: ").strip() or "소프트웨어 엔지니어링 경험"
            engine = HashimotoEngine(root_topic=topic)
            all_topics = [engine.current_topic] + engine.remaining_topics
            print(green("\n✓ 초기화 완료. 주제:"))
            for i, t in enumerate(all_topics, 1):
                mark = bold("▶") if i == 1 else " "
                print(f"  {mark} {i}. {t}")
            return engine

        elif choice == "2":
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
# 상태 표시
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
            print(f"    • {c.criterion}  (weight={c.weight})")


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
    question_text: Optional[str] = None,
    latency: Optional[TurnLatency] = None,
) -> None:
    section("① 처리 전 상태")
    print(f"  주제   : {bold(before['topic'])}")
    print(f"  Depth  : {before['depth']} / {threshold_label(anxiety)}")
    print(f"  Stack  : {' → '.join(before['stack']) if before['stack'] else dim('(비어있음)')}")
    if before["slot_scores"]:
        print("  Scores :")
        for sid, sc in before["slot_scores"].items():
            print(f"    {sid}: {score_bar(sc)}")

    section("② Hashimoto LLM 분석 — 슬롯 평가")
    show_slot_evaluations(engine)

    section("② Hashimoto LLM 분석 — 가추법 (명제 생성)")
    show_abduction(engine)

    section("② Hashimoto LLM 분석 — 상태 명령")
    show_state_commands(engine)

    section("② Hashimoto LLM 분석 — 전략")
    show_strategy(engine)

    section("③ 주제 전환 판정")
    show_prune_verdict(engine, before, anxiety)

    section("④ State 업데이트 후 상태")
    show_state_brief(engine, anxiety)

    section("⑤ Sapien 전달 패키지 (질문LLM 입력)")
    jprint(package)

    if question_text is not None:
        section("⑥ 생성된 면접 질문")
        print(f"\n  {bold(question_text)}\n")

    if latency:
        section("⑦ Latency (단계별)")
        _show_latency_line(latency)


# ═══════════════════════════════════════════════════════════════
# 도움말
# ═══════════════════════════════════════════════════════════════

def show_help() -> None:
    cmds = [
        ("state",        "현재 상태 요약"),
        ("stack",        "Priority Stack 전체 슬롯 상세"),
        ("slot <id>",    "특정 슬롯 전체 필드 조회"),
        ("topics",       "주제 목록 및 진행 현황"),
        ("archive",      "검증/거부 슬롯 아카이브 조회"),
        ("latency",      "파이프라인 단계별 지연시간 통계"),
        ("question",     "마지막 생성된 면접 질문 출력"),
        ("llm",          "마지막 Hashimoto LLM 분석 원시 JSON"),
        ("sapien",       "마지막 Sapien 패키지 원시 JSON"),
        ("sysprompt",    "시스템 프롬프트 전체 출력"),
        ("threshold",    "불안도별 depth 임계값 테이블"),
        ("reset",        "엔진 재초기화"),
        ("help",         "이 도움말"),
        ("exit / quit",  "종료"),
    ]
    print(f"\n  {bold('STT 텍스트를 입력하면 API end-to-end로 턴을 처리합니다.')}")
    print(f"\n  {'명령':<22} 설명")
    print(f"  {'─'*22} {'─'*32}")
    for cmd, desc in cmds:
        print(f"  {cyan(cmd):<22} {desc}")


# ═══════════════════════════════════════════════════════════════
# 메인 루프
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    engine = init_engine()
    tracker = LatencyTracker()
    engine = wrap_engine_timing(engine)
    question_client = QuestionLLMClient()

    turn = 1
    last_package:  Optional[SapienStrategyPackage] = None
    last_question: Optional[str] = None
    last_anxiety = 50.0

    print(f"\n{dim('명령어 목록: help')}")

    while True:
        t = effective_depth_threshold(last_anxiety)
        depth_disp = f"{engine.state.depth_counter}/{t}"
        if t < PRUNE_DEPTH_THRESHOLD:
            depth_disp += yellow("*")

        print(
            f"\n{DSEP}\n"
            f"  {green('[LLM]')}  Turn {bold(turn)}  │  {bold(engine.current_topic)}"
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
            show_help()

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

        elif cmd == "latency":
            section("파이프라인 단계별 Latency")
            tracker.show_table()

        elif cmd == "question":
            if last_question:
                section("마지막 생성된 면접 질문")
                print(f"\n  {bold(last_question)}\n")
            else:
                print(dim("  아직 생성된 질문 없음."))

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
            engine = init_engine()
            engine = wrap_engine_timing(engine)
            turn = 1
            last_package  = None
            last_question = None
            last_anxiety  = 50.0

        else:
            # ── STT 입력으로 턴 처리 (API end-to-end) ─────────────────────
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
            header(f"Turn {turn} 처리 중 ...")

            try:
                # ── ① ② ③ Hashimoto 파이프라인 ──────────────────────────
                package = engine.process_turn(
                    EngineInput(
                        stt_text=stt_text,
                        multimodal=MultimodalVector(anxiety=a, confidence=c),
                        turn_number=turn,
                    )
                )
                last_package = package

                # ── ④ 질문LLM 프롬프트 빌드 ─────────────────────────────
                t_qp = time.perf_counter()
                q_prompt = build_question_prompt(package)
                question_prompt_ms = (time.perf_counter() - t_qp) * 1000

                # ── ⑤ 질문LLM 호출 ──────────────────────────────────────
                header(f"Turn {turn} — 질문 생성 중 ...")
                t_ql = time.perf_counter()
                question_text = question_client.generate(q_prompt)
                question_llm_ms = (time.perf_counter() - t_ql) * 1000
                last_question = question_text

                # ── 전체 TurnLatency 기록 ────────────────────────────────
                et = engine._get_engine_timing()
                lat = TurnLatency(
                    turn=turn,
                    context_build_ms=et["context_build_ms"],
                    hashimoto_llm_ms=et["hashimoto_llm_ms"],
                    state_update_ms=et["state_update_ms"],
                    question_prompt_ms=question_prompt_ms,
                    question_llm_ms=question_llm_ms,
                )
                tracker.record(lat)

                show_full_turn(engine, before, a, package, question_text, lat)
                turn += 1

            except Exception as e:
                print(red(f"\n[오류] {e}"))
                traceback.print_exc()


if __name__ == "__main__":
    main()
