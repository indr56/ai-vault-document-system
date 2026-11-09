"""RAG evaluation: runs dataset.json against a running stack and scores the answers.

    make eval     # or: python evals/run_eval.py --base-url http://localhost:9700

Metrics (deterministic, so runs are comparable across prompt/model changes):
- answer_accuracy   expected facts present in the answer (answerable questions)
- citation_doc_acc  the expected document is among the citations
- citation_page_acc the expected page is cited (when the dataset specifies one)
- abstention_acc    "not found" exactly when the documents don't contain the answer
- follow_up_repeat  share of follow-up suggestions that restate the answer (contain one of the
                    case's expected facts, e.g. "24 hours" after an answer about 24 hours) - lower is better
- false_abstention  answerable questions the system refused
- wrong_amounts     dollar amounts in an answer that a correct answer can't contain (cases with
                    `amounts`), e.g. a miscomputed total stated next to correct working
- latency p50/p95 and cost per answer (from the metrics API)
Results are written to evals/results/<timestamp>.json.
"""

import argparse
import json
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
SAMPLES = next(p for p in (HERE.parents[1] / "samples", Path("/samples")) if p.exists())


def ensure_documents(client: httpx.Client, files: dict[str, str]) -> dict[str, str]:
    ids = {}
    for key, filename in files.items():
        with (SAMPLES / filename).open("rb") as fh:
            response = client.post("/documents", files={"file": (filename, fh)})
        response.raise_for_status()
        ids[key] = response.json()["document"]["id"]
    for key, document_id in ids.items():
        for _ in range(120):
            doc = client.get(f"/documents/{document_id}").json()
            if doc["status"] in ("ready", "failed"):
                break
            time.sleep(2)
        if doc["status"] != "ready":
            sys.exit(f"{key} did not become ready: {doc.get('error')}")
    return ids


def score(case: dict[str, Any], answer: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    text = answer["content"].lower()
    result: dict[str, Any] = {"id": case["id"], "abstained": answer["abstained"], "latency_ms": answer["latency_ms"]}
    # A [n] in the text that has no citation behind it can't be opened (#80, "not found" answers too).
    marks = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer["content"])}
    result["dangling_marks"] = len(marks - {c["number"] for c in answer["citations"]})
    if case.get("abstain"):
        result["abstention_correct"] = answer["abstained"]
        return result
    result["answer_correct"] = all(any(s.lower() in text for s in group) for group in case["expect"])
    facts = [s.lower() for group in case["expect"] for s in group]
    follow_ups = [q.lower() for q in answer.get("follow_up_questions", [])]
    if follow_ups:
        result["follow_up_repeats"] = sum(any(f in q for f in facts) for q in follow_ups)
        result["follow_ups"] = len(follow_ups)
    result["false_abstention"] = answer["abstained"]
    if "amounts" in case:
        stated = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", answer["content"])
        result["wrong_amounts"] = sum(a not in case["amounts"] for a in stated)
    source = case.get("source")
    if source:
        cited = answer["citations"]
        result["citation_doc_correct"] = any(c["document_id"] == ids[source["doc"]] for c in cited)
        if "page" in source:
            result["citation_page_correct"] = any(
                c["document_id"] == ids[source["doc"]] and c["page_number"] == source["page"] for c in cited
            )
    return result


def rate(results: list[dict[str, Any]], key: str) -> float | None:
    values = [r[key] for r in results if key in r]
    return round(sum(values) / len(values), 3) if values else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:9700")
    parser.add_argument("--api-key", default="vault-demo-key")
    args = parser.parse_args()

    dataset = json.loads((HERE / "dataset.json").read_text())
    client = httpx.Client(base_url=f"{args.base_url}/api/v1", headers={"X-API-Key": args.api_key}, timeout=120)
    ids = ensure_documents(client, dataset["documents"])
    usage_before = client.get("/metrics/ai-usage", params={"days": 1}).json()["totals"]["cost_usd"] or 0

    results = []
    for case in dataset["cases"]:
        # A fresh session per case and no answer cache, so every run measures the model.
        session = client.post("/chat/sessions", json={"document_ids": list(ids.values())}).json()
        for earlier in case.get("before", []):  # multi-turn cases: earlier questions in the same chat
            client.post(
                f"/chat/sessions/{session['id']}/messages", json={"question": earlier, "use_cache": False}
            ).raise_for_status()
        response = client.post(
            f"/chat/sessions/{session['id']}/messages", json={"question": case["question"], "use_cache": False}
        )
        response.raise_for_status()
        answer = response.json()["answer"]
        result = score(case, answer, ids) | {"question": case["question"], "answer": answer["content"]}
        results.append(result)
        ok = result.get("answer_correct", result.get("abstention_correct"))
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<24} {answer['content'][:90]!r}")

    cost = (client.get("/metrics/ai-usage", params={"days": 1}).json()["totals"]["cost_usd"] or 0) - usage_before
    latencies = sorted(r["latency_ms"] for r in results if r["latency_ms"])
    summary = {
        "answer_accuracy": rate(results, "answer_correct"),
        "citation_doc_acc": rate(results, "citation_doc_correct"),
        "citation_page_acc": rate(results, "citation_page_correct"),
        "abstention_acc": rate(results, "abstention_correct"),
        "false_abstention": rate(results, "false_abstention"),
        "dangling_citation_marks": sum(r["dangling_marks"] for r in results),
        "wrong_amounts": sum(r.get("wrong_amounts", 0) for r in results),
        "follow_up_repeat": (
            round(sum(r.get("follow_up_repeats", 0) for r in results) / total_follow_ups, 3)
            if (total_follow_ups := sum(r.get("follow_ups", 0) for r in results))
            else None
        ),
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": latencies[int(len(latencies) * 0.95) - 1] if latencies else None,
        "cost_usd_total": round(cost, 6),
        "cases": len(results),
    }
    print("\n" + json.dumps(summary, indent=2))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    path = out / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
