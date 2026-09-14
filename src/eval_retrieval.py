# -*- coding: utf-8 -*-
"""
检索对照评测：BM25 / 向量 / RRF 谁更好？
========================================
不做"看着挺准"的主观判断，用三个可量化口径回答"混合检索到底值不值":
  ① 召回集重合度（Jaccard）—— 两路是否在做重复的事
  ② 排名分歧（同一文档在两路的名次差）—— 重合≠等价，排名分歧才是融合的价值来源
  ③ 单路盲区（RRF Top-K 中被某一单路 Top-K 漏掉的文档数）—— 直接量化"只用一路会漏多少"
"""
import os, re, json, sqlite3
import numpy as np
import rag as R
from dedup import tokenize

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 固定评测集：手工写的八种提问形态。
# 它绑定的是 2026-09-05 那批语料——每天自动重跑时，这些问题在当天的库里
# 根本没有对应文档，重合度会退化成噪声。所以默认改用 auto_queries()
# 从当天热点现场生成，只有生成不出来时才回落到这一组。
FALLBACK_QUERIES = [
    ("原标题式", "美国8月非农新增16.2万 失业率维持4.1%"),
    ("口语改写", "美国就业数据比预期好很多，市场怎么反应"),
    ("专名代码", "恒生科技指数 ETF"),
    ("概念泛化", "地缘冲突推高能源价格"),
    ("政策类",   "国家支持人工智能中小企业的新政策"),
    ("数值定位", "柴油价格创历史新高 上涨53%"),
    ("跨主题",   "主权基金调整债券配置"),
    ("行业术语", "硅铁 钢招 成本支撑"),
]
K = 8
N_EVENTS = 3          # 取当天热度前 3 个事件
DIGITS = re.compile(r"[0-9０-９.．%％]+")


def auto_queries(n_events=N_EVENTS):
    """从当天热点现场生成评测集：每个事件出三种问法。

    · 原标题式——标题原样，考的是字面匹配
    · 关键词式——只留实词，考的是专名/术语检索
    · 去数字泛化——抹掉所有数字，考的是"记得发生了什么、记不清数字"这种问法

    这样每个 query 在当天语料里一定有对应文档，指标才有意义。"""
    db = os.path.join(BASE, "data", "radar.db")
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    run_at = conn.execute("SELECT max(run_at) FROM clusters").fetchone()[0]
    if not run_at:
        return []
    labels = [r[0] for r in conn.execute(
        "SELECT label FROM clusters WHERE run_at=? ORDER BY heat_score DESC LIMIT ?",
        (run_at, n_events))]
    out = []
    for lb in labels:
        title = lb[:30]
        kw = " ".join([w for w in tokenize(lb) if len(w) > 1][:6])
        plain = DIGITS.sub("", lb)[:30].strip()
        out.append(("原标题式", title))
        if kw:
            out.append(("关键词式", kw))
        if plain and plain != title:
            out.append(("去数字泛化", plain))
    return out

def load():
    conn = sqlite3.connect(os.path.join(BASE, "data", "radar.db"))
    rows = conn.execute("SELECT id,title,content,url,media,channel,published_at,tokens "
                        "FROM processed_articles ORDER BY published_at").fetchall()
    cols = ["id", "title", "content", "url", "media", "channel", "published_at", "tokens"]
    docs = [dict(zip(cols, r)) for r in rows]
    toks = [json.loads(d["tokens"] or "[]") for d in docs]
    emb = np.load(os.path.join(BASE, "data", "emb.npy"))
    return R.HybridRetriever(docs, toks, emb), docs

def single_path(ret, query, qtok, k=K, pool=30):
    """分别取两路各自的 Top-K（不融合），用于对照"""
    scores = ret.bm25.get_scores(qtok)
    bm = [int(i) for i in np.argsort(-scores)[:k]]
    D, I = ret.index.search(ret.encode(query), min(pool, len(ret.docs)))
    vec = [int(i) for i in I[0][:k]]
    return bm, vec

def run(verbose=True):
    ret, docs = load()
    queries = auto_queries() or FALLBACK_QUERIES
    if verbose:
        print(f"[评测] {len(queries)} 个 query"
              f"（{'当天热点现场生成' if queries is not FALLBACK_QUERIES else '固定集回落'}）")
    rows, agg = [], dict(jac=[], rankdiff=[], miss_bm=[], miss_vec=[])
    for name, q in queries:
        qt = tokenize(q)
        bm, vec = single_path(ret, q, qt)
        fused = [h["idx"] for h in ret.search(q, qt, k=K)]
        jac = len(set(bm) & set(vec)) / len(set(bm) | set(vec))
        # 排名分歧：两路都进 Top-K 的文档，名次差的均值
        diffs = [abs(bm.index(i) - vec.index(i)) for i in set(bm) & set(vec)]
        rd = round(float(np.mean(diffs)), 2) if diffs else 0.0
        # 单路盲区：RRF 结果中，某一路 Top-K 没召回的
        miss_bm = len([i for i in fused if i not in bm])
        miss_vec = len([i for i in fused if i not in vec])
        rows.append(dict(type=name, query=q, jaccard=round(jac, 3), rank_diff=rd,
                         missed_by_bm25=miss_bm, missed_by_vector=miss_vec,
                         example_top1=docs[fused[0]]["title"][:46]))
        agg["jac"].append(jac); agg["rankdiff"].append(rd)
        agg["miss_bm"].append(miss_bm); agg["miss_vec"].append(miss_vec)
        if verbose:
            print(f"{name:<8} Jaccard={jac:.2f} 平均名次差={rd:>5.1f} "
                  f"仅向量能召回={miss_bm} 仅BM25能召回={miss_vec}  {q[:22]}")
    summary = dict(
        n_queries=len(queries), top_k=K,
        avg_jaccard=round(float(np.mean(agg["jac"])), 3),
        avg_rank_diff=round(float(np.mean(agg["rankdiff"])), 2),
        avg_missed_by_bm25=round(float(np.mean(agg["miss_bm"])), 2),
        avg_missed_by_vector=round(float(np.mean(agg["miss_vec"])), 2),
        conclusion="")
    summary["conclusion"] = (
        f"两路召回集平均重合度仅 {summary['avg_jaccard']:.0%}；即便重合的文档，"
        f"两路名次平均相差 {summary['avg_rank_diff']} 位——重合≠等价，排名分歧正是融合的价值来源。"
        f"若只用 BM25，平均每个 query 漏掉 {summary['avg_missed_by_bm25']} 篇最终进入 Top{K} 的文档；"
        f"只用向量则漏掉 {summary['avg_missed_by_vector']} 篇。RRF 只用排名不用分数，"
        f"规避了两路分数量纲不可比的问题。")
    out = dict(summary=summary, per_query=rows)
    json.dump(out, open(os.path.join(BASE, "output", "retrieval_eval.json"), "w"),
              ensure_ascii=False, indent=1)
    if verbose:
        print(f"\n{summary['conclusion']}")
    return out

if __name__ == "__main__":
    run()
