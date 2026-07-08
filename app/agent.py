import json
import logging
import os
import time
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic, APIError
from anthropic.types import MessageParam, ToolResultBlockParam

from app.tools import call_tool, tools

logger = logging.getLogger("uvicorn")
client = AsyncAnthropic()

SYSTEM_PROMPT = (
    "You are a rate lookup assistant for ModernFi. You answer questions about "
    "interest rates and other U.S. economic indicators (mortgage rates, federal "
    "funds rate, treasury yields, unemployment, inflation, etc.) using live data "
    "from FRED (Federal Reserve Economic Data). Use the available tools to look "
    "up real data rather than relying on your own knowledge, since rates change "
    "frequently. Be concise: state the value, the date it's from, and brief "
    "relevant context only when it materially helps the answer. If a question "
    "is unrelated to interest rates or economic data, say so and redirect the "
    "user rather than answering from general knowledge."
)

MAX_ITERATIONS = 8


@dataclass
class AgentResult:
    """Everything about one /ask exchange that main.py needs — either to
    return to the caller or to persist via save_query."""

    answer: str | None
    status: str  # "success" | "max_iterations" | "error"
    iterations: int
    tool_calls: int
    duration_ms: int
    tools_used: list[str] = field(default_factory=list)
    error_message: str | None = None


async def run_agent(question: str) -> AgentResult:
    """Run the Claude tool-use loop for a single question: call Claude,
    dispatch any requested tool calls, feed results back, and repeat until
    Claude produces a final text answer or MAX_ITERATIONS is hit.

    If the Anthropic API itself fails (out of credits, rate limited, down,
    network error, etc.), this returns an AgentResult with status="error"
    instead of letting the exception propagate — callers get a clean result
    to persist and respond with, rather than an unhandled 500."""
    start = time.perf_counter()
    messages: list[MessageParam] = [{"role": "user", "content": question}]

    iterations = 0
    total_claude_time = 0.0
    total_tool_time = 0.0
    tool_call_count = 0
    tools_used: list[str] = []

    for _ in range(MAX_ITERATIONS):
        iterations += 1
        api_start = time.perf_counter()

        try:
            response = await client.messages.create(
                model=os.environ["ANTHROPIC_MODEL"],
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
        except APIError as e:
            total_elapsed = time.perf_counter() - start
            logger.error(f"  [iter {iterations}] Anthropic API call failed: {e}")
            return AgentResult(
                answer=None,
                status="error",
                iterations=iterations,
                tool_calls=tool_call_count,
                duration_ms=int(total_elapsed * 1000),
                tools_used=tools_used,
                error_message=str(e),
            )

        claude_elapsed = time.perf_counter() - api_start
        total_claude_time += claude_elapsed
        logger.info(
            f"  [iter {iterations}] Claude call took {claude_elapsed:.2f}s, "
            f"stop_reason={response.stop_reason}"
        )

        if response.stop_reason != "tool_use":
            break

        tool_use_blocks = [
            block for block in response.content if block.type == "tool_use"
        ]
        logger.info(
            f"  [iter {iterations}] {len(tool_use_blocks)} tool call(s) requested "
            f"in this turn: {[b.name for b in tool_use_blocks]}"
        )

        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[ToolResultBlockParam] = []
        for block in tool_use_blocks:
            tool_call_count += 1
            tools_used.append(block.name)
            tool_start = time.perf_counter()
            result = await call_tool(block.name, block.input)
            tool_elapsed = time.perf_counter() - tool_start
            total_tool_time += tool_elapsed
            logger.info(
                f"    tool={block.name} input={block.input} took {tool_elapsed:.2f}s"
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        total_elapsed = time.perf_counter() - start
        logger.warning(
            f"Hit MAX_ITERATIONS ({MAX_ITERATIONS}) for question: {question!r}"
        )
        return AgentResult(
            answer=None,
            status="max_iterations",
            iterations=iterations,
            tool_calls=tool_call_count,
            duration_ms=int(total_elapsed * 1000),
            tools_used=tools_used,
        )

    final_text = next(
        (block.text for block in response.content if block.type == "text"),
        "I don't have a text answer for that.",
    )

    total_elapsed = time.perf_counter() - start
    logger.info(
        f"Total: {total_elapsed:.2f}s | "
        f"{iterations} Claude call(s) = {total_claude_time:.2f}s | "
        f"{tool_call_count} tool call(s) = {total_tool_time:.2f}s | "
        f"question={question!r}"
    )

    return AgentResult(
        answer=final_text,
        status="success",
        iterations=iterations,
        tool_calls=tool_call_count,
        duration_ms=int(total_elapsed * 1000),
        tools_used=tools_used,
    )
