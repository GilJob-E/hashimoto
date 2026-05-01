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


class HashimotoLLMClient:
    """Anthropic API wrapper for Hashimoto engine turns.

    Stateless per-turn: sends only system prompt + current slot context + current
    user message. Conversation memory lives in the Slot Map (scores, evidence,
    status), not in raw message history.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def analyze_turn(
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

        response = self._client.messages.create(
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
    # Strip markdown code fences the model occasionally emits despite instructions
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
    return LLMAnalysisOutput.model_validate(json.loads(cleaned))
