# -*- coding: utf-8 -*-
"""
检索对照评测：BM25 / 向量 / RRF 谁更好？
========================================
不做"看着挺准"的主观判断，用三个可量化口径回答"混合检索到底值不值":
  ① 召回集重合度（Jaccard）—— 两路是否在做重复的事
  ② 排名分歧（同一文档在两路的名次差）—— 重合≠等价，排名分歧才是融合的价值来源
  ③ 单路盲区（RRF Top-K 中被某一单路 Top-K 漏掉的文档数）—— 直接量化"只用一路会漏多少"
"""
import os, json, sqlite3
import numpy as np
import rag as R
from dedup import tokenize

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 评测集：覆盖财经检索的五种真实提问形态
QUERIES = [
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
    rows, agg = [], dict(jac=[], rankdiff=[], miss_bm=[], miss_vec=[])
    for name, q in QUERIES:
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
        n_queries=len(QUERIES), top_k=K,
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
