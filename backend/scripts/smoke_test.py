"""End-to-end smoke test against a running stack (uses the real AI provider).

    make smoke            # or: python scripts/smoke_test.py --base-url http://localhost:9700

Walks through every feature with the sample documents and prints a readable report:
upload (+ validation errors, duplicates), processing, insights, versioning, multi-document and
multi-turn chat with citations, abstention, answer cache, streaming, custom summary, comparison,
metrics. Exits non-zero on the first failed check.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


class Smoke:
    def __init__(self, base_url: str, api_key: str):
        self.client = httpx.Client(base_url=f"{base_url}/api/v1", headers={"X-API-Key": api_key}, timeout=120)
        self.base_url = base_url

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def check(condition: bool, message: str) -> None:
        print(("  ✔ " if condition else "  ✘ ") + message)
        if not condition:
            sys.exit(1)

    def upload(self, path: Path, **form: str) -> dict[str, Any]:
        with path.open("rb") as fh:
            response = self.client.post("/documents", files={"file": (path.name, fh)}, data=form)
        return {"status": response.status_code, **response.json()}

    def wait_ready(self, document_id: str, timeout: float = 180) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            doc = self.client.get(f"/documents/{document_id}").json()
            if doc["status"] in ("ready", "failed") and doc["insights_status"] != "pending":
                return doc
            time.sleep(2)
        raise TimeoutError(document_id)

    def ask(self, session_id: str, question: str) -> dict[str, Any]:
        response = self.client.post(f"/chat/sessions/{session_id}/messages", json={"question": question})
        response.raise_for_status()
        answer = response.json()["answer"]
        print(f"\n  Q: {question}\n  A: {answer['content']}")
        for c in answer["citations"]:
            print(f"     [{c['number']}] {c['document_title']} p.{c['page_number']}")
        return answer

    # ------------------------------------------------------------------ scenario
    def run(self) -> None:
        print("1. Health")
        ready = httpx.get(f"{self.base_url}/health/ready", timeout=10).json()
        self.check(ready["status"] == "ready", f"stack ready: {json.dumps(ready)}")

        print("2. Upload & validation")
        policy = self.upload(SAMPLES / "acme_property_policy_2025.pdf")
        self.check(policy["status"] in (200, 202), "PDF accepted (202 = queued; 200 = already in the vault)")
        quote = self.upload(SAMPLES / "northwind_general_liability_quote.docx")
        agreement = self.upload(SAMPLES / "bayline_broker_service_agreement.md")
        scanned = self.upload(SAMPLES / "edge-cases" / "scanned_no_text.pdf")
        fake = self.upload(SAMPLES / "edge-cases" / "not_really_a.pdf")
        self.check(fake["status"] == 415, f"renamed executable rejected: {fake.get('error', {}).get('code')}")
        dup = self.upload(SAMPLES / "acme_property_policy_2025.pdf")
        self.check(dup["status"] == 200 and dup["duplicate"], "duplicate upload detected, existing document returned")

        print("3. Processing")
        ids = {k: v["document"]["id"] for k, v in [("policy", policy), ("quote", quote), ("agreement", agreement)]}
        for name, document_id in ids.items():
            doc = self.wait_ready(document_id)
            self.check(
                doc["status"] == "ready",
                f"{name}: ready, {doc['chunk_count']} chunks, {doc['processing_ms']} ms, insights={doc['insights_status']}",
            )
        failed = self.wait_ready(scanned["document"]["id"])
        self.check(failed["status"] == "failed", f"scanned PDF fails clearly: {failed['error']}")

        print("4. Insights")
        insights = self.client.get(f"/documents/{ids['policy']}/insights").json()
        self.check(insights["insights_status"] == "ready", f"category={insights['category']}, tags={insights['tags']}")
        self.check(len(insights["key_facts"]) > 0, f"{len(insights['key_facts'])} key facts with page numbers")

        print("5. Versioning")
        renewal = self.upload(SAMPLES / "acme_property_policy_2026_renewal.pdf", replaces_document_id=ids["policy"])
        ids["renewal"] = renewal["document"]["id"]
        self.check(renewal["document"]["version"] == 2, "renewal stored as version 2")
        self.wait_ready(ids["renewal"])
        versions = self.client.get(f"/documents/{ids['renewal']}/versions").json()
        self.check([v["version"] for v in versions] == [2, 1], "version history: v2 (latest), v1")

        print("6. Chat across documents")
        session = self.client.post(
            "/chat/sessions", json={"document_ids": [ids["renewal"], ids["quote"], ids["agreement"]]}
        ).json()
        a1 = self.ask(session["id"], "What is the flood deductible on the property policy?")
        self.check(a1["grounded"] and "50,000" in a1["content"], "grounded answer with citation")
        a2 = self.ask(session["id"], "And what about earthquake?")
        self.check(a2["grounded"], f"follow-up resolved as: {a2['standalone_question']}")
        a3 = self.ask(session["id"], "What is the GL quote premium and what does the broker charge for cyber services?")
        cited_docs = {c["document_id"] for c in a3["citations"]}
        self.check(len(cited_docs) >= 2, "answer combines two documents")
        a4 = self.ask(session["id"], "Who is the CEO of Acme Logistics?")
        self.check(a4["abstained"], "abstains when the documents don't contain the answer")
        a5 = self.ask(session["id"], "What is the flood deductible on the property policy?")
        self.check(a5["from_cache"], f"repeated question served from cache ({a5['latency_ms']} ms)")

        print("7. Streaming (SSE)")
        events: list[str] = []
        with self.client.stream(
            "POST", f"/chat/sessions/{session['id']}/messages/stream", json={"question": "What is the coinsurance?"}
        ) as response:
            for line in response.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
        self.check(
            events[-1] == "done" and events.count("token") > 1, f"{events.count('token')} token events, then done"
        )

        print("8. Custom summary")
        summary = self.client.post(
            f"/documents/{ids['renewal']}/summaries",
            json={"length": "short", "tone": "executive", "focus": "deductibles"},
        ).json()
        self.check(bool(summary.get("summary")), f"summary: {summary.get('summary', '')[:160]}...")

        print("9. Comparison")
        comparison = self.client.post(
            "/comparisons", json={"document_ids": [ids["policy"], ids["renewal"]], "focus": "what changed at renewal"}
        ).json()
        for _ in range(60):
            comparison = self.client.get(f"/comparisons/{comparison['id']}").json()
            if comparison["status"] in ("completed", "failed"):
                break
            time.sleep(2)
        self.check(comparison["status"] == "completed", "comparison completed")
        for diff in comparison["result"]["differences"]:
            values = ", ".join(f"{v['document_label']}={v['value']}" for v in diff["values"])
            print(f"     - {diff['aspect']} [{diff['significance']}]: {values}")

        print("10. Metrics")
        docs = self.client.get("/metrics/documents").json()
        usage = self.client.get("/metrics/ai-usage").json()
        api = self.client.get("/metrics/api", params={"hours": 1}).json()
        self.check(docs["processing"]["runs"] > 0, f"processing success rate {docs['processing']['success_rate']}")
        self.check(
            usage["totals"]["calls"] > 0,
            f"AI usage: {usage['totals']['total_tokens']} tokens, ${usage['totals']['cost_usd']}",
        )
        self.check(api["requests"] > 0, f"API metrics: {api['requests']} requests recorded")
        print("\nAll checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:9700")
    parser.add_argument("--api-key", default="vault-demo-key")
    args = parser.parse_args()
    Smoke(args.base_url, args.api_key).run()


if __name__ == "__main__":
    main()
