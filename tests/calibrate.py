"""
calibrate.py
------------
Helps choose the embedding thresholds in config.json using real embeddings.

It embeds the labelled prompts from adversarial_prompts.json and shows how
the on-topic and off-topic messages are distributed. It uses ONLY the
embedding endpoint (cheap, one request), not the chat model.

  on-topic set   = single-turn cases with expect "answer" in the on_topic categories
  off-topic set  = single-turn cases in off_topic_direct and off_topic_edge

Usage:  python tests/calibrate.py

How to read the output:
  * pass_threshold  should be high enough that (almost) no off-topic message reaches it
  * block_threshold should be low enough that (almost) no on-topic message falls below it
  * messages between the two go to the LLM classifier, so a wider gap costs more API
    calls but is safer. Record your chosen values and the reasoning in the README.
"""

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from guardrails import normalise  # noqa: E402
from main import Chatbot, load_config  # noqa: E402

ON_CATEGORIES = {"on_topic", "on_topic_edge"}
OFF_CATEGORIES = {"off_topic_direct", "off_topic_edge"}


def main():
    config = load_config(os.path.join(ROOT, "config.json"))
    config["logging"]["log_directory"] = os.path.join(ROOT, "logs")
    config["logging"]["log_file"] = "calibration_events.jsonl"

    with open(os.path.join(ROOT, "tests", "adversarial_prompts.json"), encoding="utf-8") as f:
        cases = json.load(f)["single_turn"]

    on_cases = [c for c in cases if c["category"] in ON_CATEGORIES and c["expect"] == "answer"]
    off_cases = [c for c in cases if c["category"] in OFF_CATEGORIES]

    bot = Chatbot(config)
    guards = bot.guards
    texts = [normalise(c["prompt"]) for c in on_cases + off_cases]
    vectors = bot.ai.embed(texts, purpose="calibration")

    def scores(vec):
        unit = guards._unit(vec)
        return float((guards.on_matrix @ unit).max()), float((guards.off_matrix @ unit).max())

    results = [scores(v) for v in vectors]
    on_scores = results[: len(on_cases)]
    off_scores = results[len(on_cases):]

    print("\nON-TOPIC prompts (score = best similarity to on-topic anchors, 'off' = to off-topic anchors)")
    for case, (on, off) in sorted(zip(on_cases, on_scores), key=lambda x: x[1][0]):
        print(f"  on={on:.3f} off={off:.3f}  {case['id']}  {case['prompt'][:60]}")
    print("\nOFF-TOPIC prompts")
    for case, (on, off) in sorted(zip(off_cases, off_scores), key=lambda x: -x[1][0]):
        print(f"  on={on:.3f} off={off:.3f}  {case['id']}  {case['prompt'][:60]}")

    on_vals = np.array([s[0] for s in on_scores])
    off_vals = np.array([s[0] for s in off_scores])

    print("\nThreshold sweep (on-topic score)")
    print("  t     on-topic >= t   off-topic >= t   on-topic < t   off-topic < t")
    for t in np.arange(0.10, 0.71, 0.05):
        print(f"  {t:.2f}   {np.mean(on_vals >= t):>10.0%}   {np.mean(off_vals >= t):>13.0%}"
              f"   {np.mean(on_vals < t):>11.0%}   {np.mean(off_vals < t):>12.0%}")

    # Suggestions: safest values that separate this particular labelled set.
    pass_candidates = [t for t in np.arange(0.10, 0.91, 0.01) if np.mean(off_vals >= t) == 0]
    block_candidates = [t for t in np.arange(0.10, 0.91, 0.01) if np.mean(on_vals < t) == 0]
    print("\nSuggestions (from this labelled set only, so treat as a starting point)")
    if pass_candidates:
        print(f"  pass_threshold  >= {min(pass_candidates):.2f}  (no off-topic prompt reaches it)")
    else:
        print("  pass_threshold: off-topic prompts overlap everything; rely on the classifier")
    if block_candidates:
        print(f"  block_threshold <= {max(block_candidates):.2f}  (no on-topic prompt falls below it)")
    else:
        print("  block_threshold: some on-topic prompts score very low; keep block_threshold low")

    pt = config["guardrails"]["embedding"]["pass_threshold"]
    bt = config["guardrails"]["embedding"]["block_threshold"]
    margin = config["guardrails"]["embedding"].get("margin", 0.05)
    block_margin = config["guardrails"]["embedding"].get("block_margin", 0.10)

    def decide(on, off):
        if on >= pt and (on - off) >= margin:
            return "pass"
        if on < bt and (off - on) >= block_margin:
            return "block"
        return "escalate"

    for label, group in (("on-topic", on_scores), ("off-topic", off_scores)):
        counts = {"pass": 0, "block": 0, "escalate": 0}
        for on, off in group:
            counts[decide(on, off)] += 1
        print(f"\nWith the CURRENT config ({pt} / {bt}), {label} prompts: {counts}")
    print("\n(For on-topic prompts, 'block' is a false refusal; for off-topic prompts, 'pass' is a leak.)")
    bot.close()


if __name__ == "__main__":
    main()
