"""
main.py: entry point for the chatbot.

Run with:  python main.py
Or with a different topic config:  python main.py other.json

Chat commands: /quit to exit, /reset to clear the history, /usage to show token use.

Each message goes through the input checks, then the chat model, then the
output check. If any check blocks it, the user gets a refusal and the
message is not saved to the history.
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

from api_client import AIClient, AIServiceError, ContentFilteredError
from context import ConversationContext
from guardrails import Guardrails, normalise
from logger import EventLogger


@dataclass
class BotResponse:
    text: str                          # what to show the user
    outcome: str                       # "answered", "blocked" or "error"
    blocked_by: Optional[str] = None


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Chatbot:
    """Wires the components together. Also used directly by the test runner."""

    def __init__(self, config):
        self.config = config
        self.msgs = config["messages"]
        self.gen = config["generation"]

        self.logger = EventLogger(config)
        self.ai = AIClient(config, event_callback=self.logger.api_callback)
        self.guards = Guardrails(config, self.ai, self.logger)
        self.context = ConversationContext(config, self.ai, self.logger,
                                           self.guards.system_prompt)
        self.logger.log("session_start", topic=config["topic"]["name"],
                        chat_model=self.ai.chat_model,
                        embedding_model=self.ai.embedding_model,
                        token_counting=self.context.count_method)

    def respond(self, user_text):
        """Process one user message and return a BotResponse. Never raises on AI errors."""
        try:
            return self._respond(user_text)
        except ContentFilteredError:
            # Azure's content filter rejected the request, so count it as blocked.
            self.logger.turn(user_text, "blocked", blocked_by="service_content_filter",
                             history_messages=len(self.context.turns))
            return BotResponse(self.msgs["injection_refusal"], "blocked", "service_content_filter")
        except AIServiceError as e:
            self.logger.turn(user_text, "error", history_messages=len(self.context.turns))
            self.logger.log("turn_error", detail=str(e)[:200])
            return BotResponse(self.msgs["service_error"], "error")

    def _respond(self, user_text):
        # Layers 1-3: input guardrails
        guard = self.guards.check_input(user_text, self.context)
        if not guard.allowed:
            self.logger.turn(user_text, "blocked", blocked_by=guard.layer,
                             history_messages=len(self.context.turns))
            return BotResponse(guard.message, "blocked", guard.layer)

        clean_text = normalise(user_text)

        # Layer 4: the system prompt is the first message in this list
        messages = self.context.build_messages(clean_text)
        reply = self.ai.chat(messages,
                             temperature=self.gen["chat_temperature"],
                             max_tokens=self.gen["chat_max_tokens"],
                             purpose="chat")

        # Layer 5: output guardrail
        out = self.guards.check_output(reply)
        if not out.allowed:
            self.logger.turn(user_text, "blocked", blocked_by=out.layer,
                             history_messages=len(self.context.turns))
            return BotResponse(out.message, "blocked", out.layer)

        # The model itself refused (it was told to use the standard refusal text).
        # Count it as blocked and keep the exchange out of the history.
        if self.guards.is_standard_refusal(reply):
            self.logger.turn(user_text, "blocked", blocked_by="model_refusal",
                             history_messages=len(self.context.turns))
            return BotResponse(reply, "blocked", "model_refusal")

        # Save the exchange only once every check has passed
        self.context.add_turn(clean_text, reply)
        self.logger.turn(user_text, "answered", history_messages=len(self.context.turns))
        return BotResponse(reply, "answered")

    def reset(self):
        self.context.reset()
        self.logger.log("history_reset")

    def usage_report(self):
        u = self.ai.usage
        return (f"Chat calls: {u['chat_calls']} | Embedding calls: {u['embedding_calls']} | "
                f"Failed: {u['failed_calls']} | Prompt tokens: {u['prompt_tokens']} | "
                f"Completion tokens: {u['completion_tokens']} | "
                f"Embedding tokens: {u['embedding_tokens']} | "
                f"Context now: ~{self.context.token_count()} tokens")

    def close(self):
        self.logger.session_end(self.ai.usage)


def run_cli(bot):
    print(bot.msgs["greeting"])
    try:
        while True:
            try:
                user_text = input("\nYou: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            command = user_text.strip().lower()
            if command == "/quit":
                break
            if command == "/reset":
                bot.reset()
                print("History cleared.")
                continue
            if command == "/usage":
                print(bot.usage_report())
                continue

            response = bot.respond(user_text)
            print(f"\nCoach: {response.text}")
    finally:
        bot.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    config_path = argv[0] if argv else default_path

    try:
        config = load_config(config_path)
        bot = Chatbot(config)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {config_path}")
    except AIServiceError as e:
        raise SystemExit(f"Start-up failed: {e}")

    run_cli(bot)


if __name__ == "__main__":
    main()