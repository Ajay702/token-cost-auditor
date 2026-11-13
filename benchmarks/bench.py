"""Reproducible benchmarks for token-cost-auditor (stdlib only, no API key).

    python3 benchmarks/bench.py            # default: 1,000,000-call log
    python3 benchmarks/bench.py --calls 200000

1. Throughput  - parse + group + price + report on a large synthetic log.
2. Cost math   - every group total checked against an independent re-computation.
3. Grouping    - calls generated from 8 known prompt templates (no template_id);
                 how many land in the right group?
4. Savings     - the "turn on caching" estimate vs. the real saving when caching
                 is actually switched on, for different fixed-prefix sizes.
"""
import argparse
import json
import os
import random
import resource
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auditor  # noqa: E402

PRICES, DOWNGRADE = auditor.load_prices(auditor.DEFAULT_PRICES)
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
NAMES = ["Priya", "Marco", "Aiko", "Tunde", "Sofia", "Liam", "Mei", "Omar"]
WORDS = "refund invoice login error billing shipping delay crash export password plan".split()

# 8 realistic templates. The lambda returns the prompt; `{}` parts vary per call.
TEMPLATES = {
    "sentiment":  lambda r: f'Classify the sentiment of: "{r.choice(WORDS)} {r.randint(1, 9999)}"',
    "ticket":     lambda r: f"Summarize support ticket #{r.randint(1000, 99999)} from {r.randint(1,9)}@x.io in two lines.",
    "sql":        lambda r: f"Write a SQL query that returns orders over {r.randint(10, 999)} USD placed after 2026-{r.randint(1,12):02d}-{r.randint(1,28):02d}.",
    "translate":  lambda r: f"Translate to French: '{' '.join(r.choices(WORDS, k=5))}'",
    "page":       lambda r: f"Extract the product name and price from https://shop.example.com/p/{r.randint(1, 10**6)}",
    "uuid":       lambda r: f"Look up account {fmt_uuid(r)} and list open invoices.",
    # Variable text that the normaliser cannot recognise (an unquoted name):
    "greeting":   lambda r: f"Write a friendly welcome email to {r.choice(NAMES)} about their new plan.",
    # Variable free text at the very start of the prompt:
    "freeform":   lambda r: f"{' '.join(r.choices(WORDS, k=3))}? Answer the customer's question using the FAQ.",
}


def fmt_uuid(r):
    h = f"{r.getrandbits(128):032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def make_call(r, i):
    t = r.choice(list(TEMPLATES))
    model = MODELS[i % 3]
    inp = r.randint(100, 6000)
    cread = r.choice([0, 0, r.randint(500, 4000)])
    return t, {"model": model, "prompt": TEMPLATES[t](r),
               "usage": {"input_tokens": inp, "output_tokens": r.randint(5, 2500),
                         "cache_read_input_tokens": cread,
                         "cache_creation_input_tokens": r.choice([0, 0, 0, 1500])}}


def bench_throughput(n):
    r = random.Random(42)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        path = f.name
        for i in range(n):
            f.write(json.dumps(make_call(r, i)[1]) + "\n")
    size_mb = os.path.getsize(path) / 1e6
    t0 = time.perf_counter()
    recs = auditor.load_logs(path)
    t1 = time.perf_counter()
    report = auditor.build_report(recs, PRICES, DOWNGRADE)
    auditor.render_markdown(report)
    t2 = time.perf_counter()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1e6 if sys.platform == "darwin" else 1e3)
    os.unlink(path)
    print(f"== throughput: {n:,} calls, {size_mb:,.0f} MB JSONL ==")
    print(f"  parse+group {t1-t0:6.2f}s   price+report {t2-t1:5.2f}s   total {t2-t0:6.2f}s"
          f"   {n/(t2-t0):,.0f} calls/s   peak RSS {rss:,.0f} MB")
    return recs


def bench_cost(recs):
    expected = defaultdict(float)
    for rec in recs:
        p = auditor.price_for(rec["model"], PRICES)
        expected[(rec["template"], rec["model"])] += (
            rec["input_tokens"] * p["input"] + rec["output_tokens"] * p["output"]
            + rec["cache_creation_input_tokens"] * p["cache_write"]
            + rec["cache_read_input_tokens"] * p["cache_read"]) / 1e6
    rows = auditor.aggregate(recs, PRICES)
    worst = max(abs(r["cost"] - expected[(r["template"], r["model"])]) for r in rows)
    total = sum(expected.values())
    print(f"== cost math: {len(rows)} groups, ${total:,.2f} total ==")
    print(f"  max abs diff vs independent re-computation: ${worst:.2e}")


def bench_grouping(n=20_000):
    r = random.Random(7)
    truth = []
    for i in range(n):
        t, raw = make_call(r, i)
        truth.append((t, auditor.template_key(raw)))
    keys_per_t = defaultdict(Counter)
    for t, k in truth:
        keys_per_t[t][k] += 1
    owners = defaultdict(set)
    for t, k in truth:
        owners[k].add(t)
    print(f"== grouping: {n:,} calls from {len(TEMPLATES)} templates, no template_id ==")
    print(f"  {'template':<10} {'groups':>7} {'calls in largest group':>23}  merged with another template?")
    ok_calls = 0
    for t in TEMPLATES:
        c = keys_per_t[t]
        top_key, top_n = c.most_common(1)[0]
        merged = len(owners[top_key]) > 1
        if len(c) == 1 and not merged:
            ok_calls += top_n
        print(f"  {t:<10} {len(c):>7,} {top_n / sum(c.values()):>22.1%}  {'yes' if merged else 'no'}")
    print(f"  -> {ok_calls / n:.1%} of calls grouped exactly right "
          f"({len(owners):,} groups found vs {len(TEMPLATES)} real)")


def bench_savings(calls=1000, prompt=5000):
    p = PRICES["claude-sonnet-5-5"]
    print(f"== savings estimate vs. reality: {calls} calls x {prompt} prompt tokens (sonnet) ==")
    print(f"  {'fixed prefix':>12} {'estimated':>10} {'actual':>9} {'error':>7}")
    for frac in (0.5, 0.8, 0.95):
        row = {"calls": calls, "avg_prompt": prompt, "avg_output": 300, "cache_hit_rate": 0.0,
               "cache_creation_input_tokens": 0, "input_tokens": calls * prompt,
               "model": "claude-sonnet-5-5", "cost": calls * prompt * p["input"] / 1e6}
        est = auditor.suggest(row, PRICES, {})[0][1]
        fixed = int(prompt * frac)
        before = calls * prompt * p["input"]
        # With caching: 1 cache write, then (calls-1) cache reads of the fixed prefix.
        after = (fixed * p["cache_write"] + (calls - 1) * fixed * p["cache_read"]
                 + calls * (prompt - fixed) * p["input"])
        actual = (before - after) / 1e6
        print(f"  {frac:>12.0%} {'$%.2f' % est:>10} {'$%.2f' % actual:>9} {(est - actual) / actual:>+7.0%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=1_000_000)
    a = ap.parse_args()
    print(f"python {sys.version.split()[0]} on {sys.platform}\n")
    recs = bench_throughput(a.calls)
    bench_cost(recs)
    print()
    bench_grouping()
    print()
    bench_savings()
