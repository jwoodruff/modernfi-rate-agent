import time

import httpx

BASE_URL = "http://localhost:8000"

test_cases = [
    # Specific rates
    "What's the 30-year mortgage rate?",
    "What is the federal funds rate?",
    "What's the 2-year treasury yield?",
    "What was the unemployment rate in July of 1960?",
    # Vague/broad
    "Are mortgage rates high right now?",
    "What are current interest rates?",
    # Edge cases
    "What was the unemployment rate?",
    "What's the yield on a 7-year treasury?",
    "Tell me about Bitcoin",
    "",
    "asdfjkl",
    # Compound / multi-series
    "Compare the 30-year and 15-year mortgage rates",
    # Reversed date range
    "What was the unemployment rate between 1990 and 1980?",
    "How are the Dodgers currently doing?",
]


def check_health():
    print(f"\n{'=' * 70}")
    print("HEALTH CHECK")
    try:
        response = httpx.get(f"{BASE_URL}/health", timeout=10.0)
        print(f"Status: {response.status_code}")
        print(f"Body: {response.json()}")
        if response.status_code != 200:
            print("⚠️  App is not healthy — results below may be unreliable.")
    except httpx.HTTPError as e:
        print(f"ERROR: {e}")
        print("⚠️  Couldn't reach the app at all — is it running?")


def run_ask_tests():
    results = []
    for question in test_cases:
        print(f"\n{'=' * 70}")
        print(f"Q: {question!r}")
        start = time.perf_counter()
        try:
            response = httpx.post(
                f"{BASE_URL}/ask",
                json={"question": question},
                timeout=60.0,
            )
            elapsed = time.perf_counter() - start
            print(f"Status: {response.status_code} | Time: {elapsed:.2f}s")
            print(f"Body: {response.text}")
            results.append(
                {
                    "question": question,
                    "status_code": response.status_code,
                    "elapsed": elapsed,
                }
            )
        except httpx.HTTPError as e:
            elapsed = time.perf_counter() - start
            print(f"ERROR after {elapsed:.2f}s: {e}")
            results.append(
                {"question": question, "status_code": None, "elapsed": elapsed}
            )
    return results


def check_request_validation():
    """Malformed/wrong-shaped request bodies to /ask — none of these should
    ever produce a 500. All should be caught by FastAPI/Pydantic and come
    back as 422 (or 400 for unparseable JSON)."""
    print(f"\n{'=' * 70}")
    print("REQUEST VALIDATION CHECK (all should 422 or 400, never 500)")

    cases = [
        ("missing question field", {}),
        ("wrong type: int", {"question": 12345}),
        ("wrong type: null", {"question": None}),
    ]
    for label, body in cases:
        response = httpx.post(f"{BASE_URL}/ask", json=body, timeout=10.0)
        marker = "✅" if response.status_code != 500 else "❌"
        print(f"{marker} {label}: {response.status_code}")

    # Malformed JSON — send raw garbage instead of a JSON body
    response = httpx.post(
        f"{BASE_URL}/ask",
        content=b"{not valid json!!",
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )
    marker = "✅" if response.status_code != 500 else "❌"
    print(f"{marker} malformed JSON body: {response.status_code}")


def check_history(expected_min_count: int):
    print(f"\n{'=' * 70}")
    print("HISTORY CHECK")

    # Basic fetch
    response = httpx.get(f"{BASE_URL}/history?limit=5", timeout=10.0)
    print(f"GET /history?limit=5 -> {response.status_code}")
    if response.status_code == 200:
        rows = response.json()
        print(f"Returned {len(rows)} row(s)")
        if rows:
            print("Most recent row:")
            print(f"  question:  {rows[0]['question']!r}")
            print(f"  status:    {rows[0]['status']}")
            print(f"  tools_used:{rows[0]['tools_used']}")
    else:
        print(f"Body: {response.text}")

    # Confirm the /ask calls above actually got persisted — fetch a large
    # enough page to cover every question this run just sent, and check the
    # total row count in Postgres is at least what we'd expect.
    check_response = httpx.get(f"{BASE_URL}/history?limit=100", timeout=10.0)
    if check_response.status_code == 200:
        total_rows = len(check_response.json())
        if total_rows >= expected_min_count:
            print(
                f"✅ History has {total_rows} row(s), "
                f"at least the {expected_min_count} this run should have added"
            )
        else:
            print(
                f"⚠️  History only has {total_rows} row(s), expected at least "
                f"{expected_min_count} — some /ask calls may not be persisting "
                f"(check save_query / Postgres connectivity)"
            )
    else:
        print(f"⚠️  Couldn't verify row count: {check_response.status_code}")

    # Pagination sanity check — offset page should differ from the first page
    page_1 = httpx.get(f"{BASE_URL}/history?limit=5&offset=0", timeout=10.0).json()
    page_2 = httpx.get(f"{BASE_URL}/history?limit=5&offset=5", timeout=10.0).json()
    ids_1 = [r["id"] for r in page_1] if isinstance(page_1, list) else []
    ids_2 = [r["id"] for r in page_2] if isinstance(page_2, list) else []
    if ids_1 and ids_2:
        overlap = set(ids_1) & set(ids_2)
        if overlap:
            print(f"⚠️  Pagination looks broken — overlapping ids: {overlap}")
        else:
            print("✅ Pagination looks correct — no overlap between pages")
    else:
        print(
            f"(skipping pagination check — not enough rows yet: "
            f"page 1 has {len(ids_1)}, page 2 has {len(ids_2)})"
        )

    # Offset far beyond the total row count should return an empty list,
    # not an error.
    far_offset = httpx.get(f"{BASE_URL}/history?limit=5&offset=99999", timeout=10.0)
    if far_offset.status_code == 200 and far_offset.json() == []:
        print("✅ Offset beyond row count correctly returns an empty list")
    else:
        print(
            f"⚠️  Offset beyond row count returned {far_offset.status_code} "
            f"with body {far_offset.text!r} (expected 200 + [])"
        )

    # Validation sanity check — bad query params should 422, not 500
    bad = httpx.get(f"{BASE_URL}/history?limit=0", timeout=10.0)
    print(f"GET /history?limit=0 -> {bad.status_code} (expect 422)")
    bad = httpx.get(f"{BASE_URL}/history?limit=101", timeout=10.0)
    print(f"GET /history?limit=101 -> {bad.status_code} (expect 422)")


def summarize(results):
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    for r in results:
        marker = "✅" if r["status_code"] and r["status_code"] < 400 else "❌"
        print(
            f"{marker} {r['status_code'] or 'ERR'!s:>4}  "
            f"{r['elapsed']:.2f}s  {r['question']!r}"
        )
    slow = [r for r in results if r["elapsed"] > 10]
    if slow:
        print(
            f"\n⚠️  {len(slow)} question(s) took over 10s — check server logs for these."
        )


if __name__ == "__main__":
    check_health()
    results = run_ask_tests()
    check_request_validation()
    check_history(expected_min_count=len(test_cases))
    summarize(results)
