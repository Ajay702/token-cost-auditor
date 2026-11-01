# Decision

**Reel 1 (DfAO08uPRPc) — "Reverse engineer any app with Claude + RIA".** Uses an AI tool to trace how a feature in someone else's app works, then rebuilds it from a spec. The reel frames it as a way to "get paid apps for free", which is legally and ethically shaky. The steps are partial (the real guide is gated behind a comment), and the core tool is a third-party CLI rather than something you'd build. Usefulness 4/10, Buildability 3/10, Clarity 3/10.

**Reel 2 (DezWwy7P5hp) — "Claude Opus 5.5 agents" (Chief of Staff, Competitor Watch, Hook Writer, Cost Auditor).** A list of agent ideas. The **Cost Auditor** is the most concrete one: take token-spend logs, group them by prompt template, check cache hit rates, and write a cost report with suggested fixes. Anyone running LLM calls in production needs this every week. It runs fully offline on a log file, and Claude can optionally write the summary. The reel has no transcript, but the summary gives enough detail to build it. Usefulness 8/10, Buildability 9/10, Clarity 7/10.

**Reel 3 (DeubYBWT71R) — "Daily viral-video recreation with an AI character".** Scrapes a viral TikTok every day and swaps an AI character into it with Higgsfield over MCP. It needs paid Apify and Higgsfield accounts. Reposting other people's viral videos with a swapped face raises copyright and platform-terms problems. Usefulness 5/10, Buildability 3/10, Clarity 5/10.

**Pick: Reel 2, the Cost Auditor agent.** It saves real time for a common, recurring job (the weekly "why is our LLM bill so high?" check). It needs no paid accounts and no scraping, runs on a laptop with only the Python standard library, and has a clear input, process and output that I can build faithfully.
