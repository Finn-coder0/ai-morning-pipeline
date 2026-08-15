from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import json, re

import feedparser
import yaml
from dateutil import parser as dtparser
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "sources.yaml"
OUT = ROOT / "output"

AI_TERMS = {"ai","artificial intelligence","llm","gpt","claude","gemini","model","agent","inference","training","reasoning","multimodal","openai","anthropic","deepmind","hugging face","transformer","chip","gpu","nvidia","machine learning"}
BUSINESS_TERMS = {"launch","release","pricing","revenue","enterprise","funding","acquisition","partnership","customer","business","market","sales","cloud","subscription","policy","regulation","copyright","antitrust","advertising","commerce"}
MARKETING_TERMS = {"marketing","brand","advertising","ads","search","seo","content","creator","customer","commerce","campaign","recommendation","discovery","social","retail","conversion","audience","media","publisher"}
HIGH_IMPACT_TERMS = {"launch","new model","frontier","open source","agent","regulation","lawsuit","acquisition","funding","chip","benchmark","safety","copyright","search"}
STOPWORDS = {"the","a","an","and","or","for","to","of","in","on","with","is","are","as","at","from","by","this","that","new","ai"}

def canonicalize_url(url):
    if not url: return ""
    try:
        p = urlsplit(url)
        query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"ref","source"}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), re.sub(r"/+$","",p.path or ""), urlencode(query), ""))
    except Exception:
        return url

def parse_date(entry):
    for key in ("published","updated","created"):
        value = entry.get(key)
        if value:
            try:
                d = dtparser.parse(value)
                if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
                return d.astimezone(timezone.utc)
            except Exception:
                pass
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        try: return datetime(*st[:6], tzinfo=timezone.utc)
        except Exception: pass
    return None

def clean_text(s):
    return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()

def title_tokens(title):
    toks = re.findall(r"[a-z0-9][a-z0-9\-\.\+]*",(title or "").lower())
    return {t for t in toks if t not in STOPWORDS and len(t)>1}

def jaccard(a,b):
    return len(a & b)/len(a | b) if a and b else 0.0

def source_credibility(tier):
    return {"A":25,"B":21,"C":16}.get(tier,14)

def freshness_score(age_hours):
    if age_hours is None: return 5
    if age_hours <= 6: return 15
    if age_hours <= 12: return 13
    if age_hours <= 24: return 11
    if age_hours <= 30: return 7
    if age_hours <= 48: return 3
    return 0

def keyword_count(text, terms):
    low = text.lower()
    return sum(1 for t in terms if t in low)

def score_item(item, now):
    text = f"{item['title']} {item.get('summary','')}"
    age_hours = None
    if item.get("published_at"):
        age_hours = max(0.0,(now-datetime.fromisoformat(item["published_at"])).total_seconds()/3600)
    credibility = source_credibility(item["source_tier"])
    freshness = freshness_score(age_hours)
    ai_hits = keyword_count(text,AI_TERMS)
    impact_hits = keyword_count(text,HIGH_IMPACT_TERMS)
    business_hits = keyword_count(text,BUSINESS_TERMS)
    marketing_hits = keyword_count(text,MARKETING_TERMS)
    industry_impact = min(20,5+ai_hits*2+impact_hits*3)
    business_impact = min(15,business_hits*2+impact_hits)
    marketing_relevance = min(15,marketing_hits*3)
    novelty = 6+min(4,impact_hits)
    total = credibility+freshness+industry_impact+business_impact+marketing_relevance+novelty
    route = "top_news" if total>=80 else "daily" if total>=65 else "weekly_pool" if total>=50 else "discard"
    return {**item,
        "scores":{"credibility":credibility,"freshness":freshness,"industry_impact":industry_impact,
                  "business_impact":business_impact,"marketing_relevance":marketing_relevance,
                  "novelty":novelty,"total":total},
        "route":route,
        "marketing_watch_candidate": marketing_relevance>=10 and credibility>=18,
        "age_hours": round(age_hours,1) if age_hours is not None else None}

def fetch_sources(cfg):
    now = datetime.now(timezone.utc)
    items, reports = [], []
    max_per_feed = int(cfg.get("max_items_per_feed",20))
    lookback = float(cfg.get("lookback_hours",30))
    for src in cfg["sources"]:
        report={"source_id":src["id"],"name":src["name"],"ok":False,"fetched":0,"kept":0,"error":None}
        try:
            feed=feedparser.parse(src["url"])
            if getattr(feed,"bozo",False) and not feed.entries:
                raise RuntimeError(str(getattr(feed,"bozo_exception","feed parse error")))
            report["fetched"]=len(feed.entries)
            for e in feed.entries[:max_per_feed]:
                published=parse_date(e)
                if published is not None:
                    age_h=(now-published).total_seconds()/3600
                    if age_h < -2 or age_h > lookback: continue
                title=clean_text(e.get("title",""))
                url=canonicalize_url(e.get("link",""))
                summary=clean_text(e.get("summary") or e.get("description") or "")
                if not title or not url: continue
                items.append({"source_id":src["id"],"source_name":src["name"],"source_tier":src["tier"],
                              "source_category":src.get("category",""),"title":title,"url":url,
                              "published_at":published.isoformat() if published else None,
                              "summary":summary[:900],"collected_at":now.isoformat()})
                report["kept"]+=1
            report["ok"]=True
        except Exception as ex:
            report["error"]=f"{type(ex).__name__}: {ex}"
        reports.append(report)
    return items,reports

def dedupe(items):
    seen, unique = set(), []
    for item in items:
        if item["url"] in seen: continue
        seen.add(item["url"]); unique.append(item)
    rank={"A":3,"B":2,"C":1}
    unique.sort(key=lambda x:(rank.get(x["source_tier"],0),x.get("published_at") or ""),reverse=True)
    kept, token_sets = [], []
    for item in unique:
        toks=title_tokens(item["title"])
        if any(jaccard(toks,kt)>=0.58 for kt in token_sets): continue
        kept.append(item); token_sets.append(toks)
    return kept

def render(selected,reports,now):
    OUT.mkdir(exist_ok=True)
    date_label=now.astimezone(timezone(timedelta(hours=8))).strftime("%Y.%m.%d")
    md=[f"# AI Morning｜{date_label} 候选清单","","> v0.1 deterministic MVP — 无 LLM / 无 API Key",""]
    for i,item in enumerate(selected,1):
        s=item["scores"]
        md += [f"## {i:02d}｜{item['title']}",
               f"- 来源：{item['source_name']}（Tier {item['source_tier']}）",
               f"- 总分：**{s['total']}** ｜可信 {s['credibility']} / 新鲜 {s['freshness']} / 行业 {s['industry_impact']} / 商业 {s['business_impact']} / 营销 {s['marketing_relevance']} / 新意 {s['novelty']}",
               f"- Marketing Watch 候选：{'是' if item['marketing_watch_candidate'] else '否'}",
               f"- 链接：{item['url']}",""]
    md += ["---","## Source health",""]
    for r in reports:
        md.append(f"- {'✅' if r['ok'] else '⚠️'} {r['name']}: fetched={r['fetched']}, kept={r['kept']}" + (f", error={r['error']}" if r['error'] else ""))
    (OUT/"latest.md").write_text("\n".join(md),encoding="utf-8")
    (OUT/"latest.json").write_text(json.dumps({"generated_at":now.isoformat(),"selected":selected,"source_reports":reports},ensure_ascii=False,indent=2),encoding="utf-8")
    tpl=Template("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>AI Morning MVP</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;margin:0;color:#1f2937;line-height:1.75}main{max-width:720px;margin:auto;padding:28px 20px 48px}.brand{color:#0d1d3a;font-size:30px;font-weight:800}.kicker{color:#2563eb;font-size:12px;letter-spacing:1.6px;font-weight:700}.item{padding:22px 0;border-bottom:1px solid #e5e7eb}h2{font-size:20px;color:#0d1d3a;margin:0 0 10px}.meta{font-size:13px;color:#667085}.score{color:#2563eb;font-weight:700}.watch{color:#00a6ad;font-weight:700}.foot{margin-top:30px;color:#667085;font-size:13px}</style></head>
<body><main><div class="brand">AI MORNING</div><div class="kicker">ZERO-COST MVP / {{date}}</div><p>自动采集与启发式筛选后的候选清单，不是正式发布稿。</p>
{% for x in items %}<section class="item"><h2>{{"%02d"|format(loop.index)}}｜{{x.title}}</h2><div class="meta">{{x.source_name}} · Tier {{x.source_tier}} · <span class="score">{{x.scores.total}}分</span>{% if x.marketing_watch_candidate %} · <span class="watch">Marketing Watch</span>{% endif %}</div><p><a href="{{x.url}}">{{x.url}}</a></p></section>{% endfor %}
<div class="foot">AI Morning · 每天早上 5–8 分钟，读懂 AI 与市场。</div></main></body></html>""")
    (OUT/"latest.html").write_text(tpl.render(items=selected,date=date_label),encoding="utf-8")

def main():
    cfg=yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    now=datetime.now(timezone.utc)
    items,reports=fetch_sources(cfg)
    items=dedupe(items)
    scored=[score_item(x,now) for x in items]
    scored=[x for x in scored if x["scores"]["total"]>=45]
    scored.sort(key=lambda x:x["scores"]["total"],reverse=True)
    selected=scored[:int(cfg.get("max_selected",5))]
    render(selected,reports,now)
    print(f"Collected: {len(items)} unique candidates")
    print(f"Selected: {len(selected)}")
    for i,x in enumerate(selected,1): print(f"{i}. {x['scores']['total']:>3} {x['title'][:90]}")
    print(f"Output: {OUT/'latest.md'}")

if __name__=="__main__":
    main()
