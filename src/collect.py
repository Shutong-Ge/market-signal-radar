# -*- coding: utf-8 -*-
"""
投研舆情雷达 · 多源采集模块
===========================
从 4 家媒体 7 个频道采集公开财经资讯，统一结构后落 SQLite。
设计要点：
  · 站点配置化（type: api_sina / api_em / api_wscn / rss），加源只改配置不改代码
  · 单站失败不影响整体（异常隔离 + 逐站计数），采集报告如实记录成功/失败
  · 保留原始载荷 raw（东财的 stockList 关联板块用于后续投研映射）
  · 全部为公开接口，无需登录态；微博热搜需 Cookie 且已风控，本项目不采（见热度模型的四因子归一化）
"""
import os, re, json, time, sqlite3, hashlib
from datetime import datetime, timezone, timedelta
import httpx
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "radar.db")
CST = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}

SOURCES = [
    dict(key="sina_finance", media="新浪财经", channel="财经滚动", type="api_sina",
         url="https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=50&page=1"),
    dict(key="sina_tech", media="新浪财经", channel="科技滚动", type="api_sina",
         url="https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2517&num=50&page=1"),
    dict(key="sina_world", media="新浪财经", channel="国际滚动", type="api_sina",
         url="https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2510&num=50&page=1"),
    dict(key="sina_stock", media="新浪财经", channel="股市滚动", type="api_sina",
         url="https://feed.mix.sina.com.cn/api/roll/get?pageid=155&lid=1686&num=50&page=1"),
    dict(key="em_fast", media="东方财富", channel="全球快讯", type="api_em",
         url="https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724"
             "&fastColumn=102&sortEnd=&pageSize=50&req_trace=1"),
    dict(key="wscn_live", media="华尔街见闻", channel="全球快讯", type="api_wscn",
         url="https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=50"),
    dict(key="cns_finance", media="中新网", channel="财经", type="rss",
         url="https://www.chinanews.com.cn/rss/finance.xml"),
]

# 滚动频道翻页：单页 50 条只覆盖几小时，做每日窗口不够用。
# 翻页不新增频道，站点统计里 *_p2/_p3 会并回主频道（见 build_data.py）。
def _paged(keys, pages=(2, 3, 4)):
    base = {s["key"]: s for s in SOURCES}
    out = []
    for k in keys:
        s = base[k]
        for p in pages:
            out.append(dict(s, key=f"{k}_p{p}", url=s["url"].replace("page=1", f"page={p}")))
    return out


SOURCES += _paged(["sina_finance", "sina_tech", "sina_world", "sina_stock"])

# ---------------- 存储 ----------------
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS raw_articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url_hash TEXT UNIQUE, title TEXT, content TEXT, url TEXT,
        media TEXT, channel TEXT, source_key TEXT,
        published_at TEXT, collected_at TEXT, raw TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS collect_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
        fetched INTEGER, inserted INTEGER, dup_url INTEGER, detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub ON raw_articles(published_at)")
    return c

def uhash(url, title):
    return hashlib.md5(f"{(url or '').split('?')[0]}|{(title or '')[:60]}".encode()).hexdigest()

def clean(s):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def ts2str(ts):
    try:
        return datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

# ---------------- 各类型解析器 ----------------
def parse_sina(j, src):
    out = []
    for d in (j.get("result", {}).get("data") or []):
        title = clean(d.get("title"))
        if len(title) < 6: continue
        out.append(dict(title=title, content=clean(d.get("intro") or d.get("wapsummary")),
                        url=d.get("url", ""), published_at=ts2str(d.get("ctime")),
                        raw=dict(media_name=d.get("media_name", ""), keywords=d.get("keywords", ""))))
    return out

def parse_em(j, src):
    out = []
    for d in (j.get("data", {}).get("fastNewsList") or []):
        title = clean(d.get("title"))
        if len(title) < 6: continue
        out.append(dict(title=title, content=clean(d.get("summary")),
                        url=f"https://finance.eastmoney.com/a/{d.get('code','')}.html",
                        published_at=str(d.get("showTime") or "")[:19],
                        raw=dict(stock_list=d.get("stockList") or [])))   # 关联板块/个股，投研映射用
    return out

def parse_wscn(j, src):
    out = []
    for d in (j.get("data", {}).get("items") or []):
        body = clean(d.get("content_text") or d.get("content"))
        title = clean(d.get("title")) or body[:40]
        if len(title) < 6: continue
        out.append(dict(title=title, content=body[:600], url=d.get("uri", ""),
                        published_at=ts2str(d.get("display_time")),
                        raw=dict(fund_codes=d.get("fund_codes") or [])))
    return out

def parse_rss(text, src):
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    for it in root.findall(".//item"):
        g = lambda t: clean((it.findtext(t) or ""))
        title = g("title")
        if len(title) < 6: continue
        pub = it.findtext("pubDate") or ""
        try:
            from email.utils import parsedate_to_datetime
            pt = parsedate_to_datetime(pub).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pt = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        out.append(dict(title=title, content=g("description")[:600], url=g("link"),
                        published_at=pt, raw={}))
    return out

PARSERS = {"api_sina": parse_sina, "api_em": parse_em, "api_wscn": parse_wscn}

def fetch_one(client, src):
    """单站采集，异常隔离：失败只影响该站"""
    r = client.get(src["url"], headers=HEADERS, timeout=httpx.Timeout(20.0, connect=10.0),
                   follow_redirects=True)
    r.raise_for_status()
    if src["type"] == "rss":
        r.encoding = "utf-8"
        return parse_rss(r.text, src)
    return PARSERS[src["type"]](r.json(), src)

def collect(sources=None, verbose=True):
    sources = sources or SOURCES
    conn = db()
    started = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    detail, fetched, inserted, dup = [], 0, 0, 0
    with httpx.Client() as client:
        for src in sources:
            try:
                items = fetch_one(client, src)
                ok = 0
                for it in items:
                    h = uhash(it["url"], it["title"])
                    try:
                        conn.execute(
                            "INSERT INTO raw_articles(url_hash,title,content,url,media,channel,"
                            "source_key,published_at,collected_at,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (h, it["title"], it["content"], it["url"], src["media"], src["channel"],
                             src["key"], it["published_at"], started,
                             json.dumps(it.get("raw", {}), ensure_ascii=False)))
                        ok += 1
                    except sqlite3.IntegrityError:
                        dup += 1
                fetched += len(items); inserted += ok
                detail.append(dict(key=src["key"], media=src["media"], channel=src["channel"],
                                   fetched=len(items), inserted=ok, status="ok"))
                if verbose: print(f"  ✓ {src['media']}·{src['channel']:8s} 抓取{len(items):3d} 新增{ok:3d}")
            except Exception as e:
                detail.append(dict(key=src["key"], media=src["media"], channel=src["channel"],
                                   fetched=0, inserted=0, status=f"fail: {type(e).__name__}"))
                if verbose: print(f"  ✗ {src['media']}·{src['channel']:8s} 失败 {type(e).__name__}")
    conn.commit()
    finished = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO collect_runs(started_at,finished_at,fetched,inserted,dup_url,detail)"
                 " VALUES(?,?,?,?,?,?)",
                 (started, finished, fetched, inserted, dup, json.dumps(detail, ensure_ascii=False)))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()[0]
    conn.close()
    return dict(started=started, finished=finished, fetched=fetched, inserted=inserted,
                dup_url=dup, ok_sites=sum(1 for d in detail if d["status"] == "ok"),
                sites=len(sources), total_in_db=total, detail=detail)

if __name__ == "__main__":
    print(f"[采集] {len(SOURCES)} 个频道 / {len(set(s['media'] for s in SOURCES))} 家媒体")
    r = collect()
    print(f"\n抓取 {r['fetched']} 条，新增 {r['inserted']} 条，URL层重复 {r['dup_url']} 条 "
          f"| 站点成功 {r['ok_sites']}/{r['sites']} | 库内累计 {r['total_in_db']} 条")
