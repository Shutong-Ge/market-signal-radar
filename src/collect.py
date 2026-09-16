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


# ── 披露源：法定披露平台与监管数据库 ────────────────────────────────
# 与新闻源的区别：公告是「事实」，新闻是「对事实的转述」。
# 做投研，公告优先级更高——但公告量太大且噪声重，所以按板块关键词检索，
# 不做全市场灌入。关键词与 taxonomy.py 的二级板块对齐。
CNINFO_PROBES = ["存储芯片", "半导体", "算力", "消费电子", "晶圆"]
EDGAR_PROBES = ["HBM", "memory pricing", "advanced packaging", "wafer capacity"]


ANN_WINDOW_DAYS = 7          # 公告只取近一周，和分析窗口对齐


def _cninfo(kw, page_size=30):
    end = datetime.now(CST).date()
    start = end - timedelta(days=ANN_WINDOW_DAYS)
    return dict(
        key="cninfo_%s" % kw, media="巨潮资讯", channel="沪深公告", type="api_cninfo",
        method="POST", probe=kw,
        url="http://www.cninfo.com.cn/new/hisAnnouncement/query",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"},
        data={"pageNum": 1, "pageSize": page_size, "column": "szse", "tabName": "fulltext",
              "plate": "", "searchkey": kw, "secid": "", "category": "", "trade": "",
              "seDate": "%s~%s" % (start, end), "sortName": "", "sortType": "",
              "isHLtitle": "true"},
    )


def _edgar(kw, forms="8-K"):
    import urllib.parse as _u
    return dict(
        key="edgar_%s" % kw.replace(" ", "_"), media="SEC EDGAR", channel="美股披露",
        type="api_edgar", probe=kw,
        url="https://efts.sec.gov/LATEST/search-index?q=%s&forms=%s" % (_u.quote('"%s"' % kw), forms),
        # efts 的 dateRange 参数在本项目实测不生效，改为解析阶段按窗口过滤
        headers={"User-Agent": "market-signal-radar research shutong_kim@163.com"},
    )


SOURCES += [_cninfo(k) for k in CNINFO_PROBES]
SOURCES += [_edgar(k) for k in EDGAR_PROBES]

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

def parse_cninfo(j, src):
    """巨潮资讯：沪深两市的法定指定披露平台，公告原文都在这里。

    不做全市场灌入——那是 39 万条量级，且绝大多数是程式化公告。
    改为按板块关键词检索，谁在关心哪条线，就只把那条线的公告拉进来。"""
    def _tight(t):                       # <em> 高亮剥掉后会在中文之间留空格
        return re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", t)

    out = []
    for d in (j.get("announcements") or []):
        title = _tight(clean(d.get("announcementTitle")))
        sec = _tight(clean(d.get("secName")))
        if len(title) < 4:
            continue
        adj = d.get("adjunctUrl") or ""
        out.append(dict(
            title=("%s：%s" % (sec, title)) if sec else title,
            content=clean(d.get("announcementTypeName") or ""),
            url=("http://static.cninfo.com.cn/" + adj) if adj else "",
            published_at=ts2str(int(d.get("announcementTime", 0)) // 1000),
            raw=dict(sec_code=d.get("secCode"), sec_name=sec,
                     plate=d.get("orgId"), probe=src.get("probe"))))
    return out


EDGAR_WINDOW_DAYS = 45      # 美股披露密度低，窗口放宽到 45 天


def parse_edgar(j, src):
    """SEC EDGAR 全文检索：海外对标公司的 8-K / 10-Q 原文。

    A 股口径看不到的东西，往往先出现在美股的 8-K 里——
    电子这条链上，设备、存储、封装的对标公司几乎都在美股。"""
    out = []
    cut = (datetime.now(CST) - timedelta(days=EDGAR_WINDOW_DAYS)).strftime("%Y-%m-%d")
    for h in (j.get("hits", {}).get("hits") or []):
        so = h.get("_source", {})
        if (so.get("file_date") or "") < cut:      # 全文检索会一路翻到 2010 年，按窗口截断
            continue
        names = so.get("display_names") or []
        form = so.get("form", "")
        title = "%s %s：%s" % (names[0].split("  (")[0] if names else "SEC",
                              form, src.get("probe", ""))
        aid = (h.get("_id") or "").split(":")
        acc = aid[0].replace("-", "") if aid else ""
        cik = (so.get("ciks") or ["0"])[0].lstrip("0")
        url = ("https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (cik, acc, aid[1])
               if len(aid) > 1 and cik else "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")
        out.append(dict(title=clean(title), content=", ".join(names)[:200], url=url,
                        published_at=(so.get("file_date") or "")[:10] + " 00:00:00",
                        raw=dict(form=form, ciks=so.get("ciks"), probe=src.get("probe"))))
    return out


PARSERS = {"api_sina": parse_sina, "api_em": parse_em, "api_wscn": parse_wscn,
           "api_cninfo": parse_cninfo, "api_edgar": parse_edgar}

def fetch_one(client, src):
    """单站采集，异常隔离：失败只影响该站。支持 GET / POST 与自定义请求头。"""
    hdr = dict(HEADERS, **(src.get("headers") or {}))
    to = httpx.Timeout(25.0, connect=10.0)
    if src.get("method") == "POST":
        r = client.post(src["url"], data=src.get("data"), json=src.get("json"),
                        headers=hdr, timeout=to, follow_redirects=True)
    else:
        r = client.get(src["url"], headers=hdr, timeout=to, follow_redirects=True)
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
