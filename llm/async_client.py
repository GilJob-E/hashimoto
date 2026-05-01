import json
import os
import re

import anthropic
from dotenv import load_dotenv

from llm.prompts import build_system_prompt, build_turn_message
from models.io import LLMAnalysisOutput

load_dotenv()

_MODEL = "claude-opus-4-7"
_MAX_TOKENS = 8192


class AsyncHashimotoLLMClient:
    """비동기 Anthropic API 래퍼. asyncio 이벤트 루프 내에서 사용.

    Stateless per-turn: 대화 히스토리 없이 매 턴 단일 메시지만 전송.
    """

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    async def analyze_turn(
        self,
        stt_text: str,
        active_slots_context: str,
        depth_counter: int,
        anxiety: float,
        confidence: float,
    ) -> LLMAnalysisOutput:
        user_message = build_turn_message(
            stt_text=stt_text,
            active_slots_context=active_slots_context,
            depth_counter=depth_counter,
            anxiety=anxiety,
            confidence=confidence,
        )

        response = await self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

        return _parse_output(_extract_text(response))

    def reset_history(self) -> None:
        pass


def _extract_text(response: anthropic.types.Message) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("LLM response contained no text block")


def _parse_output(raw: str) -> LLMAnalysisOutput:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
    return LLMAnalysisOutput.model_validate(json.loads(cleaned))
