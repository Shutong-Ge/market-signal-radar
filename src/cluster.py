# -*- coding: utf-8 -*-
"""
投研舆情雷达 · 无K值聚类 + 热度模型
===================================
聚类：不预设簇数。向量近邻（余弦≥0.80）建边 + 并查集连通合并，
      让"多少个热点"由数据自己决定——资讯流场景下每天热点个数本就不固定，
      预设 K 的 KMeans 在这里是错误的建模假设。
降级：向量链路不可用时回退关键词规则聚类（8 大投研板块词表），业务不中断。

热度模型（四因子）：
      覆盖度 coverage  = 簇内文章数 / 50
      爆发速度 burst   = 首日文章数 / 20
      持续时长 duration= 活跃天数 / 7
      媒体覆盖 media   = 去重媒体数 / 4
  原设计含第五因子"社交热度"（微博热搜排名），因微博接口需登录态且已风控，
  本项目不采集 → 触发原设计的【无社交因子权重归一化】分支，四因子权重重新归一。
  这不是砍功能，是容错分支的真实生效。

投研增强（原项目没有）：
      利用东方财富快讯自带的 stockList（关联板块/个股代码），把热点映射到板块，
      让"热点"直接落到投研可用的维度上。
"""
import os, re, json, sqlite3, time
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "radar.db")
CST = timezone(timedelta(hours=8))
SIM_MIN = 0.80
HEAT_W = {"coverage": 0.25, "burst": 0.20, "duration": 0.20, "media": 0.20, "social": 0.15}

# 8 大投研板块词表（兼作关键词降级聚类的类目）
CATEGORIES = [
    dict(key="ai",     label="AI与大模型", kw=["AI", "人工智能", "大模型", "AIGC", "算力", "智能体", "GPT", "OpenAI", "英伟达", "芯片算力"]),
    dict(key="semi",   label="半导体",     kw=["半导体", "晶圆", "制程", "EDA", "存储芯片", "光刻", "封测", "芯片"]),
    dict(key="energy", label="新能源",     kw=["新能源", "锂电", "光伏", "储能", "氢能", "风电", "碳酸锂", "电池", "碳中和"]),
    dict(key="macro",  label="宏观政策",   kw=["央行", "降准", "降息", "GDP", "PMI", "财政", "货币政策", "国债", "CPI", "社融", "房地产"]),
    dict(key="global", label="国际市场",   kw=["美联储", "美元", "汇率", "关税", "贸易", "地缘", "原油", "黄金", "非农", "欧央行"]),
    dict(key="auto",   label="汽车出行",   kw=["汽车", "智驾", "车企", "机器人", "低空", "新能源车", "无人机", "特斯拉"]),
    dict(key="bio",    label="生物医药",   kw=["医药", "创新药", "疫苗", "医疗", "集采", "临床", "药企", "生物"]),
    dict(key="fin",    label="金融市场",   kw=["券商", "保险", "银行", "A股", "港股", "基金", "证监会", "IPO", "北向", "南向", "沪深"]),
]
FALLBACK_CAT = dict(key="misc", label="综合", kw=[])

# 程式化公告噪声：单家媒体批量发布的模板文本，不构成"热点"
NOISE_PAT = re.compile(r"(临时股东会|股东大会|业绩说明会|业绩发布会|获.{0,8}增持|遭.{0,8}减持|"
                       r"授出.{0,6}股份|发行.{0,6}新股|回购.{0,4}股份|股份质押|限售股|解除质押|"
                       r"董事会决议|监事会决议|募集资金|龙虎榜|涨停板|资金流向|每股作价)")

def is_noise(title):
    return bool(NOISE_PAT.search(str(title or "")))

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS clusters(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, label TEXT, category TEXT,
        n_articles INTEGER, n_media INTEGER, heat_score REAL, importance INTEGER,
        heat_factors TEXT, time_span TEXT, sectors TEXT, article_ids TEXT, rep_title TEXT)""")
    return c

# ---------------- 并查集 ----------------
class UF:
    def __init__(self, n): self.p = list(range(n))
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[rb] = ra

# ---------------- 聚类（主方案：向量近邻 + 连通合并） ----------------
def cluster_by_vector(emb, sim_min=SIM_MIN, topk=25):
    import faiss
    n, d = emb.shape
    index = faiss.IndexFlatIP(d); index.add(emb)
    D, I = index.search(emb, min(topk, n))
    uf = UF(n)
    edges = 0
    for i in range(n):
        for s, j in zip(D[i], I[i]):
            if i != j and s >= sim_min:
                uf.union(i, int(j)); edges += 1
    groups = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)
    return list(groups.values()), edges

def cluster_by_keywords(arts):
    """降级方案：按板块词表分组（向量链路不可用时使用）"""
    groups = defaultdict(list)
    for i, a in enumerate(arts):
        groups[category_of(a)["key"]].append(i)
    return list(groups.values()), 0

def category_of(a):
    text = f"{a['title']} {(a.get('content') or '')[:200]}"
    best, hits = FALLBACK_CAT, 0
    for c in CATEGORIES:
        h = sum(1 for k in c["kw"] if k.lower() in text.lower())
        if h > hits: best, hits = c, h
    return best

# ---------------- 热度模型 ----------------
def compute_heat(members, active=None, calib=None):
    """active: 本轮有区分度的因子集合。单日窗口内 duration 恒为1天，
    对排序零贡献却占20%权重——与"无社交因子"同理，退化因子应剔除并重新归一。"""
    n = len(members)
    dts = []
    for a in members:
        try: dts.append(datetime.strptime(a["published_at"][:19], "%Y-%m-%d %H:%M:%S"))
        except Exception: pass
    if dts:
        span_days = max((max(dts) - min(dts)).days + 1, 1)
        d0 = min(d.date() for d in dts)
        first_day = sum(1 for d in dts if d.date() == d0)
    else:
        span_days, first_day = 1, n
    medias = {a["media"] for a in members}

    # ★ 窗口内对数归一：固定分母（原设计 /50、/20）按特定数据规模拍定，换规模即失效；
    #   改用分位数硬截断又会因 P90 偏小而大量并列。最终采用"对数缩放 + 窗口内极值归一"：
    #   log1p(n)/log1p(n_max) 既自适应数据规模、又严格保序不产生并列，
    #   对数还能抑制超大簇的碾压效应（10篇 vs 4篇有差距但不悬殊）。
    cal = calib or {}
    lg = lambda x, m: min(1.0, np.log1p(max(x, 0)) / np.log1p(max(m, 1))) if m > 1 else min(1.0, x / max(m, 1))
    coverage = lg(n, cal.get("max_size", 50))
    burst    = lg(first_day, cal.get("max_first", 20))
    duration = min(1.0, span_days / max(cal.get("window_days", 7), 1))
    media    = min(1.0, len(medias) / max(cal.get("n_media_total", 4), 1))

    vals = dict(coverage=coverage, burst=burst, duration=duration, media=media, social=None)
    # ★ 权重动态归一：只保留"可采集且有区分度"的因子（社交因子无数据、单日窗口内duration退化）
    act = active if active is not None else {"coverage", "burst", "duration", "media"}
    used = [k for k in act if vals.get(k) is not None]
    tot_w = sum(HEAT_W[k] for k in used) or 1.0
    heat = sum(HEAT_W[k] * vals[k] for k in used) / tot_w
    heat = round(min(0.97, max(0.05, heat)), 3)
    importance = None   # 分级在窗口内按分位标定（见 assign_levels）
    return dict(heat_score=heat, importance=importance, n_media=len(medias),
                time_span=f"{span_days}天", active_factors=sorted(used),
                factors=dict(coverage=round(coverage, 3), burst=round(burst, 3),
                             duration=round(duration, 3), media=round(media, 3), social=None))

def sectors_of(members):
    """从东财 stockList 提取关联板块代码（BK开头为板块）"""
    codes = Counter()
    for a in members:
        try: raw = json.loads(a.get("raw") or "{}")
        except Exception: raw = {}
        for s in (raw.get("stock_list") or []):
            if ".BK" in str(s): codes[str(s).split(".")[-1]] += 1
    return [c for c, _ in codes.most_common(5)]

def label_of(members):
    """簇标签：取簇内最高媒体覆盖/最早的代表标题（LLM 标签在 report 阶段生成）"""
    return sorted(members, key=lambda a: (-len(a["title"]), a["published_at"]))[0]["title"][:48]

LEVELS = [(0.10, "爆点", 5), (0.30, "高热", 4), (0.60, "升温中", 3), (1.01, "一般", 2)]

def assign_levels(clusters):
    """窗口内分位分级：热度分已改为窗口内相对值，分级也须相对标定——
    沿用原设计的绝对阈值(0.75/0.5/0.3)会随采集规模漂移，失去业务含义。
    口径：热度排名前10%=爆点、前30%=高热、前60%=升温中、其余=一般。"""
    n = len(clusters)
    for i, c in enumerate(clusters):
        q = (i + 1) / max(n, 1)
        for cut, name, imp in LEVELS:
            if q <= cut:
                c["level"], c["importance"] = name, imp
                break
    return clusters

# ---------------- 主流程 ----------------
def run(min_size=2, min_media=2, use_vector=True, verbose=True):
    t0 = time.time()
    conn = db()
    rows = conn.execute("SELECT id,raw_id,title,content,url,media,channel,published_at,tokens,raw "
                        "FROM processed_articles ORDER BY published_at").fetchall()
    cols = ["id", "raw_id", "title", "content", "url", "media", "channel", "published_at", "tokens", "raw"]
    arts = [dict(zip(cols, r)) for r in rows]
    if verbose: print(f"[聚类] 输入去重后文章 {len(arts)} 条")

    mode, edges = "vector", 0
    try:
        if not use_vector: raise RuntimeError("forced fallback")
        emb = np.load(os.path.join(BASE, "data", "emb.npy"))
        assert len(emb) == len(arts), f"向量数 {len(emb)} 与文章数 {len(arts)} 不一致"
        groups, edges = cluster_by_vector(emb)
    except Exception as e:
        mode = "keyword_fallback"
        if verbose: print(f"  ! 向量链路不可用（{e}），降级关键词规则聚类")
        groups, _ = cluster_by_keywords(arts)

    multi = [g for g in groups if len(g) >= min_size]
    # 噪声过滤：簇内多数为程式化公告 → 剔除
    def noisy(g):
        return sum(1 for i in g if is_noise(arts[i]["title"])) / len(g) >= 0.6
    n_before = len(multi)
    multi = [g for g in multi if not noisy(g)]
    n_noise = n_before - len(multi)
    # 单一媒体门槛：热点须至少 2 家媒体报道（单媒体批量稿≠热点）
    multi = [g for g in multi if len({arts[i]["media"] for i in g}) >= min_media]
    n_single = n_before - n_noise - len(multi)
    if verbose:
        print(f"  过滤：程式化公告簇 {n_noise} 个、单一媒体簇 {n_single} 个 → 有效热点 {len(multi)}")

    # 因子退化检测：窗口内所有簇跨度相同 → duration 无区分度，剔除该因子
    spans = set()
    for g in multi:
        ds = []
        for i in g:
            try: ds.append(datetime.strptime(arts[i]["published_at"][:19], "%Y-%m-%d %H:%M:%S").date())
            except Exception: pass
        spans.add((max(ds) - min(ds)).days + 1 if ds else 1)
    # 标定参数：按本轮簇分布计算
    sizes, firsts = [], []
    for g in multi:
        sizes.append(len(g))
        ds = []
        for i in g:
            try: ds.append(datetime.strptime(arts[i]["published_at"][:19], "%Y-%m-%d %H:%M:%S").date())
            except Exception: pass
        firsts.append(sum(1 for d in ds if d == min(ds)) if ds else len(g))
    calib = dict(max_size=float(max(sizes)) if sizes else 50,
                 max_first=float(max(firsts)) if firsts else 20,
                 window_days=max(spans) if spans else 7,
                 n_media_total=len({a["media"] for a in arts}))
    if verbose: print(f"  热度标定(窗口内对数归一): 最大簇={calib['max_size']:.0f}篇 "
                      f"最大首日={calib['max_first']:.0f}篇 窗口={calib['window_days']}天 "
                      f"媒体数={calib['n_media_total']}")
    active = {"coverage", "burst", "duration", "media"}
    if len(spans) <= 1:
        active.discard("duration")
        if verbose: print(f"  因子退化：窗口内簇跨度均为 {spans} → 剔除 duration 因子，权重重新归一")
    if verbose:
        print(f"  模式={mode} 连通边={edges} 连通分量={len(groups)} "
              f"→ 多篇簇 {len(multi)}（单篇{len(groups)-len(multi)}条不计入热点）")

    run_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM clusters")
    out = []
    for g in multi:
        members = [arts[i] for i in g]
        h = compute_heat(members, active, calib)
        cat = Counter(category_of(a)["label"] for a in members).most_common(1)[0][0]
        rec = dict(label=label_of(members), category=cat, n_articles=len(members),
                   sectors=sectors_of(members), rep_title=label_of(members),
                   article_ids=[a["id"] for a in members], **h)
        out.append(rec)
        conn.execute("INSERT INTO clusters(run_at,label,category,n_articles,n_media,heat_score,"
                     "importance,heat_factors,time_span,sectors,article_ids,rep_title)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_at, rec["label"], cat, len(members), h["n_media"], h["heat_score"],
                      h["importance"], json.dumps(h["factors"], ensure_ascii=False), h["time_span"],
                      json.dumps(rec["sectors"], ensure_ascii=False),
                      json.dumps(rec["article_ids"]), rec["rep_title"]))
    conn.commit()
    out.sort(key=lambda x: -x["heat_score"])
    assign_levels(out)
    # 分级落库（compute_heat 阶段无法知道窗口内相对位置）
    for c in out:
        conn.execute("UPDATE clusters SET importance=? WHERE run_at=? AND label=?",
                     (c["importance"], run_at, c["label"]))
    conn.commit()
    el = round(time.time() - t0, 1)
    stat = dict(n_articles=len(arts), mode=mode, edges=edges, n_components=len(groups),
                n_clusters=len(multi), n_noise_filtered=n_noise, n_single_media_filtered=n_single,
                active_factors=sorted(active), calibration=calib, elapsed=el,
                level_dist=dict(Counter(c["level"] for c in out)),
                top=[dict(label=c["label"], heat=c["heat_score"], level=c["level"],
                          n=c["n_articles"], media=c["n_media"], cat=c["category"],
                          sectors=c["sectors"], factors=c["factors"],
                          article_ids=c["article_ids"]) for c in out[:10]])
    json.dump(stat, open(os.path.join(BASE, "output", "cluster_report.json"), "w"),
              ensure_ascii=False, indent=1)
    conn.close()
    if verbose:
        print(f"\n热点簇 {len(multi)} 个，耗时 {el}s，分级：{stat['level_dist']}")
        print("\nTop10 热点：")
        for i, c in enumerate(out[:10], 1):
            sec = f" 板块{c['sectors'][:2]}" if c["sectors"] else ""
            print(f"  {i:2d}. [{c['heat_score']:.3f}|{c['level']}|{c['category']:6s}] {c['label'][:38]} "
                  f"({c['n_articles']}篇/{c['n_media']}家媒体{sec})")
    return stat

if __name__ == "__main__":
    import sys
    run(use_vector="--fallback" not in sys.argv)
