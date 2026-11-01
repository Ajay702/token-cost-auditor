"""Token Cost Auditor: turn LLM usage logs into a cost report with suggested fixes.

Usage:
    python auditor.py --demo                 # run on bundled sample logs
    python auditor.py --logs usage.jsonl     # run on your own logs
    python auditor.py --logs usage.jsonl --llm   # also ask Claude for a summary
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DEMO_LOGS = HERE / "sample_data" / "demo_logs.jsonl"
DEFAULT_PRICES = HERE / "prices.json"

# Prompts shorter than this (in tokens) generally can't be cached.
MIN_CACHEABLE_TOKENS = 1024


# ---------- config ----------

def load_dotenv(path=HERE / ".env"):
    """Minimal .env loader (KEY=VALUE lines). Real env vars win."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_prices(path):
    data = json.loads(Path(path).read_text())
    return data["models"], data.get("downgrade", {})


# ---------- parsing ----------

USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")


def parse_record(raw):
    """Accept either {"usage": {...}} (Anthropic API shape) or flat token fields."""
    usage = raw.get("usage", raw)
    rec = {f: int(usage.get(f) or 0) for f in USAGE_FIELDS}
    rec["model"] = raw.get("model", "unknown")
    rec["template"] = template_key(raw)
    return rec


def load_logs(path):
    records = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(parse_record(json.loads(line)))
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"warning: skipping line {n}: {e}", file=sys.stderr)
    return records


_NORMALIZERS = [
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<EMAIL>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\"[^\"]*\"|'[^']*'"), "<STR>"),
    (re.compile(r"\d+(?:[.:/-]\d+)*"), "<NUM>"),
    (re.compile(r"\s+"), " "),
]


def template_key(raw, length=60):
    """Group calls by prompt template.

    Uses an explicit `template_id` when the log has one. Otherwise strips
    the variable bits (numbers, emails, URLs, IDs, quoted text) from the
    start of the prompt so calls made from the same template share a key.
    """
    if raw.get("template_id"):
        return str(raw["template_id"])
    text = raw.get("system") or raw.get("prompt") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    text = text.strip()[:length]
    return text or "(no prompt)"


# ---------- cost math ----------

def price_for(model, prices):
    if model in prices:
        return prices[model]
    for name, p in prices.items():  # allow dated ids like claude-x-20260101
        if model.startswith(name):
            return p
    return prices["default"]


def call_cost(rec, p):
    return (rec["input_tokens"] * p["input"]
            + rec["output_tokens"] * p["output"]
            + rec["cache_creation_input_tokens"] * p["cache_write"]
            + rec["cache_read_input_tokens"] * p["cache_read"]) / 1_000_000


def aggregate(records, prices):
    """Sum tokens and cost per (template, model)."""
    groups = defaultdict(lambda: {f: 0 for f in USAGE_FIELDS} | {"calls": 0, "cost": 0.0})
    for rec in records:
        g = groups[(rec["template"], rec["model"])]
        g["calls"] += 1
        g["cost"] += call_cost(rec, price_for(rec["model"], prices))
        for f in USAGE_FIELDS:
            g[f] += rec[f]

    rows = []
    for (template, model), g in groups.items():
        prompt_tokens = (g["input_tokens"] + g["cache_read_input_tokens"]
                         + g["cache_creation_input_tokens"])
        rows.append({
            "template": template,
            "model": model,
            **g,
            "prompt_tokens": prompt_tokens,
            "avg_prompt": prompt_tokens / g["calls"],
            "avg_output": g["output_tokens"] / g["calls"],
            "cache_hit_rate": g["cache_read_input_tokens"] / prompt_tokens if prompt_tokens else 0.0,
        })
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows


# ---------- suggestions ----------

def suggest(row, prices, downgrade):
    """Return a list of (fix, estimated_monthly_savings_in_usd_or_None)."""
    p = price_for(row["model"], prices)
    tips = []

    # 1. Big, repeated prompts with caching not turned on at all.
    if (row["calls"] >= 3 and row["avg_prompt"] >= MIN_CACHEABLE_TOKENS
            and row["cache_hit_rate"] < 0.5 and row["cache_creation_input_tokens"] == 0):
        # Rough guess: 80% of uncached input could become cache reads.
        saved = 0.8 * row["input_tokens"] * (p["input"] - p["cache_read"]) / 1_000_000
        tips.append(("Turn on prompt caching: move the fixed part (system prompt, "
                     "docs, tool definitions) to the front and mark it with cache_control.", saved))

    # 2. Writing to the cache but rarely reading it back.
    if (row["cache_creation_input_tokens"] > 0
            and row["cache_read_input_tokens"] < row["cache_creation_input_tokens"]):
        wasted = row["cache_creation_input_tokens"] * (p["cache_write"] - p["input"]) / 1_000_000
        tips.append(("Cache is written more than it is read. Something early in the prompt "
                     "changes every call (timestamp, user id?) or calls are further apart "
                     "than the cache lifetime. Move variable text after the cache breakpoint.",
                     max(wasted, 0.0)))

    # 3. Expensive model doing small jobs.
    cheaper = downgrade.get(row["model"])
    if cheaper and row["avg_output"] < 400 and row["avg_prompt"] < 4000:
        alt_cost = call_cost(row, price_for(cheaper, prices))  # same tokens, cheaper prices
        tips.append((f"Short inputs and outputs: try {cheaper} for this template "
                     "and compare quality on a sample.", row["cost"] - alt_cost))

    # 4. Very long outputs.
    if row["avg_output"] > 2000:
        tips.append(("Outputs are long (avg {:,.0f} tokens). Set a lower max_tokens or ask "
                     "for a shorter format.".format(row["avg_output"]), None))
    return tips


# ---------- report ----------

def build_report(records, prices, downgrade, top=10):
    rows = aggregate(records, prices)
    total = sum(r["cost"] for r in rows)
    prompt = sum(r["prompt_tokens"] for r in rows)
    cached = sum(r["cache_read_input_tokens"] for r in rows)
    findings = []
    for r in rows[:top]:
        for fix, saving in suggest(r, prices, downgrade):
            findings.append({"template": r["template"], "model": r["model"],
                             "fix": fix, "savings": saving})
    findings.sort(key=lambda f: f["savings"] or 0, reverse=True)
    return {
        "calls": len(records),
        "total_cost": total,
        "overall_cache_hit_rate": cached / prompt if prompt else 0.0,
        "rows": rows[:top],
        "findings": findings,
    }


def render_markdown(report):
    out = ["# Token Cost Audit", ""]
    out.append(f"- Calls analysed: **{report['calls']:,}**")
    out.append(f"- Total cost: **${report['total_cost']:,.2f}**")
    out.append(f"- Overall cache hit rate: **{report['overall_cache_hit_rate']:.0%}**")
    out += ["", "## Spend by prompt template", "",
            "| # | Template | Model | Calls | Cost | Avg prompt tok | Avg output tok | Cache hit |",
            "|---|---|---|---:|---:|---:|---:|---:|"]
    for i, r in enumerate(report["rows"], 1):
        tpl = r["template"].replace("|", "\\|")
        out.append(f"| {i} | `{tpl}` | {r['model']} | {r['calls']:,} | ${r['cost']:,.2f} | "
                   f"{r['avg_prompt']:,.0f} | {r['avg_output']:,.0f} | {r['cache_hit_rate']:.0%} |")
    out += ["", "## Suggested fixes", ""]
    if not report["findings"]:
        out.append("Nothing obvious to fix.")
    for f in report["findings"]:
        save = f" — est. save **${f['savings']:,.2f}**" if f["savings"] else ""
        out.append(f"- `{f['template']}` ({f['model']}): {f['fix']}{save}")
    out.append("")
    return "\n".join(out)


# ---------- optional Claude summary ----------

def claude_summary(markdown, api_key, model):
    """Ask Claude for a short plain-English briefing. Only the aggregated report is sent."""
    body = json.dumps({
        "model": model,
        "max_tokens": 600,
        "messages": [{"role": "user", "content":
                      "You are a cost auditor for an LLM-powered product. Read this audit and "
                      "write a 5-bullet briefing for the engineering lead: biggest cost driver, "
                      "the top 3 fixes in priority order, and one risk to watch.\n\n" + markdown}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return "".join(b.get("text", "") for b in data.get("content", []))


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description="Audit LLM token spend from JSONL usage logs.")
    ap.add_argument("--logs", default=os.getenv("AUDIT_LOGS"), help="Path to JSONL usage log")
    ap.add_argument("--demo", action="store_true", help="Use bundled sample logs")
    ap.add_argument("--prices", default=os.getenv("AUDIT_PRICES", str(DEFAULT_PRICES)))
    ap.add_argument("--top", type=int, default=10, help="How many templates to show")
    ap.add_argument("--out", default=os.getenv("AUDIT_OUT", "output/cost_report.md"))
    ap.add_argument("--llm", action="store_true", help="Add a Claude-written briefing")
    args = ap.parse_args(argv)

    logs = DEMO_LOGS if args.demo else args.logs
    if not logs:
        ap.error("pass --logs PATH (or set AUDIT_LOGS), or use --demo")

    prices, downgrade = load_prices(args.prices)
    report = build_report(load_logs(logs), prices, downgrade, top=args.top)
    md = render_markdown(report)

    if args.llm:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            print("warning: --llm needs ANTHROPIC_API_KEY; skipping summary", file=sys.stderr)
        else:
            summary = claude_summary(md, key, os.getenv("AUDITOR_MODEL", "claude-opus-5-5"))
            md = md.replace("## Spend by prompt template",
                            "## Briefing (Claude)\n\n" + summary.strip()
                            + "\n\n## Spend by prompt template", 1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(md)
    print(f"Report saved to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
