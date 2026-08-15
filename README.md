# AI Morning Pipeline v0.1

A zero-API-cost MVP for **AI Morning**.

## Goal
Validate the editorial value of an automated daily AI news pipeline before paying for n8n or an LLM API.

```text
RSS feeds
-> normalize
-> recent-window filter
-> URL/title dedupe
-> deterministic heuristic scoring
-> rank
-> Top 5 candidate digest
-> GitHub Actions artifact + job summary
```

No OpenAI key. No WeChat secret. No paid automation tool.

## MVP source set
- OpenAI News
- Hugging Face Blog
- MIT Technology Review
- arXiv CS.AI
- Hacker News AI filter

This is not the final source registry. It is a low-cost test of the acquisition pipeline.

## Local run
Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

Outputs:
- `output/latest.md`
- `output/latest.json`
- `output/latest.html`

## GitHub Actions
The included workflow supports manual runs and a daily schedule at about 05:30 Asia/Shanghai.
v0.1 uploads artifacts and writes the digest into the Actions job summary. It does not write back to the repo.

## Acceptance criteria
- one feed failure does not kill the whole run;
- obvious duplicates are removed;
- Top 5 are mostly relevant to AI Morning;
- every item keeps a source URL;
- review time is lower than manual searching.

## Roadmap
`v0.1 deterministic RSS MVP -> v0.2 source improvements -> v0.3 LLM editorial selection -> v0.4 AI Morning HTML -> v0.5 WeChat draft API`
