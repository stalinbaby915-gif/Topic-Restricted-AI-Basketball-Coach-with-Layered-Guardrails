"""
logger.py

JSONL event log (logs/chatbot_events.jsonl), one JSON object per line.

Events:
  session_start / session_end   lifecycle and final usage totals
  api_call / api_error          tokens, latency, retries (from api_client)
  guardrail                     layer, decision, score, reason
  turn                          outcome of each user message
  context_trim                  history truncated or summarised
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone


class EventLogger:
    def __init__(self, config):
        log_cfg = config["logging"]
        self.directory = log_cfg.get("log_directory", "logs")
        self.path = os.path.join(self.directory, log_cfg.get("log_file", "chatbot_events.jsonl"))
        self.log_user_text = log_cfg.get("log_user_text", True)
        self.max_chars = log_cfg.get("max_logged_chars", 300)
        self.session_id = uuid.uuid4().hex[:8]
        self.counts = {}  # event and blocked_by counts for the session summary

        os.makedirs(self.directory, exist_ok=True)

    def log(self, event, **fields):
        """Append one event. Write errors are printed, not raised."""
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": self.session_id,
            "event": event,
            **fields,
        }
        self._count(record)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[logger warning: could not write log: {e}]")

    def api_callback(self, record):
        """Pass as AIClient(event_callback=...)."""
        record = dict(record)
        event = record.pop("event", "api_call")
        self.log(event, **record)

    def guardrail(self, layer, decision, user_text=None, score=None, reason=None):
        """
        layer:    'input_rules', 'embedding', 'llm_classifier' or 'output_check'
        decision: 'pass', 'block' or 'escalate' (sent to the next layer)
        """
        fields = {"layer": layer, "decision": decision}
        if score is not None:
            fields["score"] = round(float(score), 4)
        if reason:
            fields["reason"] = reason
        if user_text is not None:
            fields.update(self._text_fields(user_text))
        self.log("guardrail", **fields)

    def turn(self, user_text, outcome, blocked_by=None, history_messages=None):
        """outcome: 'answered', 'blocked' or 'error'"""
        fields = {"outcome": outcome, **self._text_fields(user_text)}
        if blocked_by:
            fields["blocked_by"] = blocked_by
        if history_messages is not None:
            fields["history_messages"] = history_messages
        self.log("turn", **fields)

    def session_end(self, usage):
        self.log("session_end", usage=usage, counts=self.counts)
        print("\n--- Session summary ---")
        print(f"Chat calls:        {usage['chat_calls']}")
        print(f"Embedding calls:   {usage['embedding_calls']}")
        print(f"Failed calls:      {usage['failed_calls']}")
        print(f"Prompt tokens:     {usage['prompt_tokens']}")
        print(f"Completion tokens: {usage['completion_tokens']}")
        print(f"Embedding tokens:  {usage['embedding_tokens']}")
        blocked = {k: v for k, v in self.counts.items() if k.startswith("blocked_by:")}
        if blocked:
            print("Blocked messages:  " + ", ".join(f"{k.split(':', 1)[1]}={v}" for k, v in blocked.items()))
        print(f"Log file:          {self.path}")

    def _text_fields(self, text):
        """With log_user_text off, keep only a length and hash so events can still be matched up."""
        if self.log_user_text:
            clipped = text if len(text) <= self.max_chars else text[: self.max_chars] + "..."
            return {"text": clipped}
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        return {"text_len": len(text), "text_hash": digest}

    def _count(self, record):
        event = record["event"]
        self.counts[event] = self.counts.get(event, 0) + 1
        if event == "turn" and record.get("blocked_by"):
            key = f"blocked_by:{record['blocked_by']}"
            self.counts[key] = self.counts.get(key, 0) + 1


def read_log(path):
    """Load a JSONL log into a list of dicts (used by the test runner)."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records