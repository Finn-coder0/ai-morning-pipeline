from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import json
import re

import feedparser
import yaml
from dateutil import parser as dtparser
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "sources.yaml"
OUT = ROOT / "output"

AI_TERMS = {
    "ai", "artificial intelligence", "llm", "gpt", "claude", "gemini",
    "model", "agent", "agents", "inference", "training", "reasoning",
    "multimodal", "openai", "anthropic", "deepmind", "hugging face",
    "transformer", "chip", "gpu", "nvidia", "machine learning",
    "generative", "genai", "copilot", "foundation model",
}
BUSINESS_TERMS = {
    "launch", "release", "pricing", "revenue", "enterprise", "funding",
    "acquisition", "partnership", "customer", "business", "market", "sales",
    "cloud", "subscription", "policy", "regulation", "copyright", "antitrust",
    "advertising", "commerce", "product", "platform", "developer", "api",
}
MARKETING_TERMS = {
    "marketing", "brand", "advertising", "ads", "search", "seo", "content",
    "creator", "customer", "commerce", "campaign", "recommendation",
    "discovery", "social", "retail", "conversion", "audience", "media",
    "publisher", "shopping", "merchant", "performance", "creative",
}
HIGH_IMPACT_TERMS = {
    "launch", "introducing", "release", "new model", "frontier",
    "open source", "agent", "agents", "regulation", "lawsuit",
    "acquisition", "funding", "chip", "benchmark", "safety",
    "copyright", "search", "partnership", "enterprise", "api",
}
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "is", "are", "as", "at", "from", "by", "this", "that", "new", "ai",
}


def canonicalize_url(url):
    if not url:
        return ""
    try:
        p = urlsplit(url)
        query = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
            and k.lower() not in {"ref", "source"}
        ]
        return urlunsplit(
            (
                p.scheme.lower(),
                p.netloc.lower(),
                re.sub(r"/+$", "", p.path or ""),
                urlencode(query),
                "",
            )
        )
    except Exception:
        return url


def parse_date(entry):
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                d = dtparser.parse(value)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return d.astimezone(timezone.utc)
            except Exception:
                pass

    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        try:
            return datetime(*st[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def clean_text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def keyword_count(text, terms):
    low = text.lower()
    return sum(1 for term in terms if term in low)


def has_ai_signal(text):
    return keyword_count(text, AI_TERMS) > 0


def title_tokens(title):
    toks = re.findall(r"[a-z0-9][a-z0-9\-\.\+]*", (title or "").lower())
    return {t for t in toks if t not in STOPWORDS and len(t) > 1}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def source_credibility(tier):
    return {"A": 25, "B": 21, "C": 16}.get(tier, 14)


def freshness_score(age_hours):
    if age_hours is None:
        return 0
    if age_hours <= 6:
        return 15
    if age_hours <= 12:
        return 13
    if age_hours <= 24:
        return 11
    if age_hours <= 36:
        return 7
    if age_hours <= 48:
        return 3
    return 0


def score_item(item, now):
    text = f"{item['title']} {item.get('summary', '')}"
    age_hours = (
        max(
            0.0,
            (now - datetime.fromisoformat(item["published_at"])).total_seconds()
            / 3600,
        )
        if item.get("published_at")
        else None
    )

    credibility = source_credibility(item["source_tier"])
    freshness = freshness_score(age_hours)

    ai_hits = keyword_count(text, AI_TERMS)
    impact_hits = keyword_count(text, HIGH_IMPACT_TERMS)
    business_hits = keyword_count(text, BUSINESS_TERMS)
    marketing_hits = keyword_count(text, MARKETING_TERMS)

    industry_impact = min(20, 4 + min(ai_hits, 4) * 2 + impact_hits * 3)
    business_impact = min(15, business_hits * 3 + min(impact_hits, 3))
    marketing_relevance = min(15, marketing_hits * 3)
    novelty = min(10, 4 + impact_hits * 2)

    # Small editorial boost for first-party news and marketing-platform news.
    if item["source_category"] == "official":
        industry_impact = min(20, industry_impact + 2)
    if item["source_category"] == "marketing":
        marketing_relevance = min(15, marketing_relevance + 3)

    total = (
        credibility
        + freshness
        + industry_impact
        + business_impact
        + marketing_relevance
        + novelty
    )
    editorial_signal = (
        industry_impact + business_impact + marketing_relevance + novelty
    )

    route = (
        "top_news"
        if total >= 80
        else "daily"
        if total >= 65
        else "weekly_pool"
        if total >= 47
        else "discard"
    )

    return {
        **item,
        "scores": {
            "credibility": credibility,
            "freshness": freshness,
            "industry_impact": industry_impact,
            "business_impact": business_impact,
            "marketing_relevance": marketing_relevance,
            "novelty": novelty,
            "total": total,
        },
        "editorial_signal": editorial_signal,
        "route": route,
        "marketing_watch_candidate": (
            marketing_relevance >= 9 and credibility >= 18
        ),
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
    }


def fetch_sources(cfg):
    now = datetime.now(timezone.utc)
    items, reports = [], []
    max_per_feed = int(cfg.get("max_items_per_feed", 30))
    lookback = float(cfg.get("lookback_hours", 36))

    for src in cfg["sources"]:
        report = {
            "source_id": src["id"],
            "name": src["name"],
            "ok": False,
            "fetched": 0,
            "recent": 0,
            "kept": 0,
            "undated": 0,
            "topic_filtered": 0,
            "error": None,
        }

        try:
            feed = feedparser.parse(src["url"])
            if getattr(feed, "bozo", False) and not feed.entries:
                raise RuntimeError(
                    str(getattr(feed, "bozo_exception", "feed parse error"))
                )

            report["fetched"] = len(feed.entries)
            recent_entries = []

            for entry in feed.entries:
                published = parse_date(entry)

                if published is None:
                    report["undated"] += 1
                    if not bool(src.get("allow_undated", False)):
                        continue
                else:
                    age_h = (now - published).total_seconds() / 3600
                    if age_h < -2 or age_h > lookback:
                        continue

                title = clean_text(entry.get("title", ""))
                url = canonicalize_url(entry.get("link", ""))
                summary = clean_text(
                    entry.get("summary") or entry.get("description") or ""
                )
                if not title or not url:
                    continue

                text = f"{title} {summary}"
                if bool(src.get("require_ai_signal", False)) and not has_ai_signal(text):
                    report["topic_filtered"] += 1
                    continue

                recent_entries.append(
                    {
                        "published_sort": (
                            published.timestamp() if published else -1
                        ),
                        "published": published,
                        "title": title,
                        "url": url,
                        "summary": summary,
                    }
                )

            recent_entries.sort(
                key=lambda x: x["published_sort"],
                reverse=True,
            )
            report["recent"] = len(recent_entries)

            for row in recent_entries[:max_per_feed]:
                published = row["published"]
                items.append(
                    {
                        "source_id": src["id"],
                        "source_name": src["name"],
                        "source_tier": src["tier"],
                        "source_category": src.get("category", ""),
                        "selection_cap": int(
                            src.get(
                                "selection_cap",
                                cfg.get("default_source_cap", 2),
                            )
                        ),
                        "title": row["title"],
                        "url": row["url"],
                        "published_at": (
                            published.isoformat() if published else None
                        ),
                        "summary": row["summary"][:1000],
                        "collected_at": now.isoformat(),
                    }
                )
                report["kept"] += 1

            report["ok"] = True

        except Exception as ex:
            report["error"] = f"{type(ex).__name__}: {ex}"

        reports.append(report)

    return items, reports


def dedupe(items):
    seen, unique = set(), []
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)

    rank = {"A": 3, "B": 2, "C": 1}
    unique.sort(
        key=lambda x: (
            rank.get(x["source_tier"], 0),
            x.get("published_at") or "",
        ),
        reverse=True,
    )

    kept, token_sets = [], []
    for item in unique:
        toks = title_tokens(item["title"])
        if any(jaccard(toks, old) >= 0.58 for old in token_sets):
            continue
        kept.append(item)
        token_sets.append(toks)
    return kept


def select_diverse(scored, cfg):
    max_selected = int(cfg.get("max_selected", 5))
    max_reserve = int(cfg.get("max_reserve", 8))
    min_total = int(cfg.get("min_total", 47))
    min_editorial_signal = int(cfg.get("min_editorial_signal", 14))
    default_source_cap = int(cfg.get("default_source_cap", 2))
    category_caps = cfg.get("category_caps", {})

    source_counts = defaultdict(int)
    category_counts = defaultdict(int)
    selected, reserve, audit = [], [], []

    ranked = sorted(
        scored,
        key=lambda x: (
            x["scores"]["total"],
            x.get("published_at") or "",
        ),
        reverse=True,
    )

    for item in ranked:
        reason = None
        source_id = item["source_id"]
        category = item.get("source_category", "")
        source_cap = int(item.get("selection_cap", default_source_cap))
        category_cap = int(category_caps.get(category, max_selected))

        if item["scores"]["total"] < min_total:
            reason = "below_total"
        elif item["editorial_signal"] < min_editorial_signal:
            reason = "below_editorial_signal"
        elif category in {"research", "community"} and item["scores"]["total"] < 58:
            reason = "weak_research_or_community"
        elif source_counts[source_id] >= source_cap:
            reason = "source_cap"
        elif category_counts[category] >= category_cap:
            reason = "category_cap"

        if reason is None and len(selected) < max_selected:
            selected.append(item)
            source_counts[source_id] += 1
            category_counts[category] += 1
            decision = "selected"
        else:
            decision = reason or "max_selected"
            if (
                len(reserve) < max_reserve
                and item["scores"]["total"] >= 42
                and item["source_category"] not in {"research", "community"}
            ):
                reserve.append(item)

        audit.append(
            {
                "title": item["title"],
                "source": item["source_name"],
                "category": category,
                "age_hours": item.get("age_hours"),
                "total": item["scores"]["total"],
                "editorial_signal": item["editorial_signal"],
                "decision": decision,
            }
        )

    return selected, reserve, audit[:40]


def render(selected, reserve, audit, reports, now):
    OUT.mkdir(exist_ok=True)
    date_label = now.astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y.%m.%d")

    md = [
        f"# AI Morning｜{date_label} 候选清单",
        "",
        "> v0.4 source-pool MVP — 无 LLM / 无 API Key",
        "> 新增官方 AI / 营销平台来源；无日期内容不进入 Daily。",
        "",
    ]

    if not selected:
        md += [
            "今天没有达到 V0.4 门槛的正式候选项。",
            "",
        ]

    for i, item in enumerate(selected, 1):
        s = item["scores"]
        md += [
            f"## {i:02d}｜{item['title']}",
            f"- 来源：{item['source_name']}（Tier {item['source_tier']} / {item['source_category']}）",
            f"- 发布时间距今：{item.get('age_hours')} 小时",
            f"- 总分：**{s['total']}** ｜可信 {s['credibility']} / 新鲜 {s['freshness']} / 行业 {s['industry_impact']} / 商业 {s['business_impact']} / 营销 {s['marketing_relevance']} / 新意 {s['novelty']}",
            f"- Marketing Watch 候选：{'是' if item['marketing_watch_candidate'] else '否'}",
            f"- 链接：{item['url']}",
            "",
        ]

    if reserve:
        md += ["---", "## Reserve / 编辑备选", ""]
        for item in reserve[:5]:
            md += [
                f"- **{item['title']}**",
                f"  - {item['source_name']} / {item['scores']['total']} 分 / {item['url']}",
            ]
        md.append("")

    md += ["---", "## Source health", ""]
    for report in reports:
        md.append(
            f"- {'✅' if report['ok'] else '⚠️'} {report['name']}: "
            f"fetched={report['fetched']}, recent={report['recent']}, "
            f"kept={report['kept']}, undated={report['undated']}, "
            f"topic_filtered={report['topic_filtered']}"
            + (f", error={report['error']}" if report["error"] else "")
        )

    (OUT / "latest.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "version": "0.4",
                "selected": selected,
                "reserve": reserve,
                "source_reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "audit.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "version": "0.4",
                "top_candidates": audit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tpl = Template(
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>AI Morning MVP</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;margin:0;color:#1f2937;line-height:1.75}main{max-width:720px;margin:auto;padding:28px 20px 48px}.brand{color:#0d1d3a;font-size:30px;font-weight:800}.kicker{color:#2563eb;font-size:12px;letter-spacing:1.6px;font-weight:700}.item{padding:22px 0;border-bottom:1px solid #e5e7eb}h2{font-size:20px;color:#0d1d3a;margin:0 0 10px}.meta{font-size:13px;color:#667085}.score{color:#2563eb;font-weight:700}.watch{color:#00a6ad;font-weight:700}.foot{margin-top:30px;color:#667085;font-size:13px}</style></head>
<body><main><div class="brand">AI MORNING</div><div class="kicker">ZERO-COST MVP V0.4 / {{date}}</div><p>自动采集与来源平衡后的候选清单，不是正式发布稿。</p>
{% for x in items %}<section class="item"><h2>{{"%02d"|format(loop.index)}}｜{{x.title}}</h2><div class="meta">{{x.source_name}} · {{x.source_category}} · <span class="score">{{x.scores.total}}分</span>{% if x.marketing_watch_candidate %} · <span class="watch">Marketing Watch</span>{% endif %}</div><p><a href="{{x.url}}">{{x.url}}</a></p></section>{% endfor %}
<div class="foot">AI Morning · 每天早上 5–8 分钟，读懂 AI 与市场。</div></main></body></html>"""
    )
    (OUT / "latest.html").write_text(
        tpl.render(items=selected, date=date_label),
        encoding="utf-8",
    )


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)

    items, reports = fetch_sources(cfg)
    items = dedupe(items)
    scored = [score_item(item, now) for item in items]
    selected, reserve, audit = select_diverse(scored, cfg)

    render(selected, reserve, audit, reports, now)

    print(f"Collected: {len(items)} unique candidates")
    print(f"Selected: {len(selected)}")
    for i, item in enumerate(selected, 1):
        print(
            f"{i}. {item['scores']['total']:>3} "
            f"[{item['source_category']}] "
            f"{item['title'][:90]}"
        )

    print("Source health:")
    for report in reports:
        print(
            f"- {report['name']}: fetched={report['fetched']}, "
            f"recent={report['recent']}, kept={report['kept']}, "
            f"undated={report['undated']}, "
            f"topic_filtered={report['topic_filtered']}"
        )

    print(f"Output: {OUT / 'latest.md'}")
    print(f"Audit: {OUT / 'audit.json'}")


if __name__ == "__main__":
    main()
