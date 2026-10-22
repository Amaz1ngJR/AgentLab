"""Summarize exported AgentManager records without model calls or file mutation.

Usage: python -m app.agent.delegation_metrics path/to/records.json
Supply separate fixed-task exports for brief / resume / full_prefix comparisons.
"""
from __future__ import annotations

import json
import sys
from collections import Counter


def summarize(records: list[dict]) -> dict:
    groups = {}
    seen = set()
    for record in records:
        identity = (record.get("parent_run_id"), record.get("result_id"))
        if identity in seen:
            continue
        seen.add(identity)
        mode = record.get("context_diagnostics", {}).get("mode", "unknown")
        group = groups.setdefault(mode, {"runs": 0, "usage": {}, "elapsed_seconds": 0,
                                         "first_generation_delta_seconds": [], "tool_calls": 0,
                                         "model_rounds": 0, "first_visible_output_seconds": [],
                                         "statuses": {}, "read_signatures": []})
        group["runs"] += 1
        status = record.get("status", "unknown")
        group["statuses"][status] = group["statuses"].get(status, 0) + 1
        for key, value in record.get("usage", {}).items():
            group["usage"][key] = group["usage"].get(key, 0) + value
        metrics = record.get("metrics", {})
        group["elapsed_seconds"] += metrics.get("submitted_to_finish_seconds", metrics.get("elapsed_seconds", 0))
        group["model_rounds"] += metrics.get("model_rounds", 0)
        visible = metrics.get("first_visible_output_seconds")
        if visible is not None:
            group["first_visible_output_seconds"].append(visible)
        group["tool_calls"] += metrics.get("tool_calls", 0)
        group["read_signatures"].extend(metrics.get("read_signatures", []))
        for request in metrics.get("requests", []):
            delta = request.get("first_generation_delta_seconds")
            if delta is not None:
                group["first_generation_delta_seconds"].append(delta)
    for group in groups.values():
        signatures = Counter(group.pop("read_signatures"))
        group["repeated_read_calls"] = sum(n - 1 for n in signatures.values())
        group["quality"] = "manual_review_required"
    return groups


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as source:
        print(json.dumps(summarize(json.load(source)), ensure_ascii=False, indent=2))
