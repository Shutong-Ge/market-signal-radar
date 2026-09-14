# -*- coding: utf-8 -*-
"""
投研舆情雷达 · 混合检索 + RAG 深度分析报告
==========================================
检索：BM25（词面精确，认得住"非农""BK0800"这类专名与代码）
      + 向量（语义泛化，认得出"就业数据超预期"和"非农大超预期"是一回事）
      → RRF 融合（Reciprocal Rank Fusion，1/(k+rank) 相加）
      RRF 只用排名不用分数，天然规避两路分数量纲不可比的问题——
      这也是它比"加权求和归一化分数"更稳的原因。

生成：簇内文章 + 混合检索补充的关联报道 → 结构化投研分析（五节）
      · JSON 容错链：围栏剥除 → 正则兜底 → 章节规范化 → 仍失败则模板降级
      · Top-N 限流 + 结果缓存（按簇指纹），控制 LLM 调用成本
      · LLM 不可用时降级为"检索+统计"模板报告，业务不中断

双引擎：本地 Qwen（演示）/ OpenAI 兼容 API（生产），ENGINE=api 切换。
"""
import os, re, json, sqlite3, time, hashlib
from datetime import datetime, timedelta, timezone
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "radar.db")
CST = timezone(timedelta(hours=8))
ENGINE = os.environ.get("ENGINE", "local")
RRF_K = 60
CACHE_DAYS = 7

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_key TEXT UNIQUE, cluster_label TEXT,
        created_at TEXT, engine TEXT, sections TEXT, citations TEXT, degraded INTEGER,
        retrieval TEXT, elapsed REAL)""")
    return c

# ================= 混合检索 =================
class HybridRetriever:
    """BM25 + 向量双路召回 → RRF 融合"""
    def __init__(self, docs, tokens, emb):
        from rank_bm25 import BM25Okapi
        import faiss
        self.docs, self.emb = docs, emb
        self.bm25 = BM25Okapi(tokens)
        self.index = faiss.IndexFlatIP(emb.shape[1]); self.index.add(emb)
        self._enc = None

    def encode(self, q):
        if self._enc is None:
            from sentence_transformers import SentenceTransformer
            self._enc = SentenceTransformer("BAAI/bge-small-zh-v1.5",
                        cache_folder=os.environ.get("HF_HOME", "/home/claude/hf_cache"))
        return self._enc.encode([q], normalize_embeddings=True).astype("float32")

    def search(self, query, query_tokens, k=8, pool=30):
        # 路1：BM25
        scores = self.bm25.get_scores(query_tokens)
        bm_rank = list(np.argsort(-scores)[:pool])
        # 路2：向量
        D, I = self.index.search(self.encode(query), min(pool, len(self.docs)))
        vec_rank = list(I[0])
        # RRF 融合：只用排名，不用分数（两路分数量纲不可比）
        fused = {}
        for r, i in enumerate(bm_rank):
            fused[int(i)] = fused.get(int(i), 0) + 1.0 / (RRF_K + r + 1)
        for r, i in enumerate(vec_rank):
            fused[int(i)] = fused.get(int(i), 0) + 1.0 / (RRF_K + r + 1)
        top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        return [dict(idx=i, rrf=round(s, 5),
                     bm25_rank=(bm_rank.index(i) + 1 if i in bm_rank else None),
                     vec_rank=(vec_rank.index(i) + 1 if i in vec_rank else None),
                     **self.docs[i]) for i, s in top]

# ================= LLM 引擎 =================
class LocalLLM:
    name = "Qwen2.5-1.5B-Instruct (本地)"
    def __init__(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        mid = os.environ.get("LOCAL_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
        cache = os.environ.get("HF_HOME", "/home/claude/hf_cache")
        self.tok = AutoTokenizer.from_pretrained(mid, cache_dir=cache)
        self.m = AutoModelForCausalLM.from_pretrained(mid, cache_dir=cache,
                    dtype=torch.bfloat16, low_cpu_mem_usage=True).eval()
    def chat(self, msgs, mx=420):
        text = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        inp = self.tok(text, return_tensors="pt")
        with self.torch.no_grad():
            out = self.m.generate(**inp, max_new_tokens=mx, do_sample=False,
                                  repetition_penalty=1.05, pad_token_id=self.tok.eos_token_id)
        return self.tok.decode(out[0][inp.input_ids.shape[1]:], skip_special_tokens=True).strip()

class APILLM:
    def __init__(self):
        import requests; self.rq = requests
        self.name = os.environ.get("API_MODEL", "deepseek-chat") + " (API)"
    def chat(self, msgs, mx=420):
        r = self.rq.post(os.environ["API_BASE"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['API_KEY']}"},
            json=dict(model=os.environ.get("API_MODEL", "deepseek-chat"), messages=msgs,
                      max_tokens=mx, temperature=0.3), timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

def get_llm():
    # ENGINE=template：显式跳过 LLM（CI 里没有 API Key 时用），
    # 让 run() 走模板降级并在页面上如实标出，而不是假装调用了模型
    if ENGINE == "template":
        raise RuntimeError("ENGINE=template：按配置跳过 LLM")
    if ENGINE == "api": return APILLM()
    return LocalLLM()

# ================= JSON 容错链 =================
SECTIONS = ["事件概述", "关键信息", "市场影响", "关注要点"]

def parse_json_chain(raw):
    """LLM JSON 容错：① 剥围栏 ② 抠花括号 ③ 修尾逗号 ④ 章节规范化"""
    s = re.sub(r"```(?:json)?|```", "", str(raw or "")).strip()
    obj = None
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
            try:
                obj = json.loads(cand); break
            except Exception:
                continue
    if not isinstance(obj, dict):
        return None
    def flatten(v):
        """值形态归一：LLM 会把字段写成 字符串/列表/嵌套字典 三种形态，
        不做扁平化就会把 dict 的 repr 直接打进报告（实测踩过）。"""
        if isinstance(v, dict):
            return "；".join(f"{k}：{flatten(x)}" for k, x in v.items() if x)
        if isinstance(v, (list, tuple)):
            return "；".join(flatten(x) for x in v if x)
        return str(v).strip()

    out = {}
    for k in SECTIONS:                       # 章节规范化：缺章补空、别名归一、值扁平化
        v = obj.get(k) or obj.get(k.replace("与", "")) or ""
        out[k] = flatten(v)
    return out if any(out.values()) else None

def template_report(cluster, hits):
    """LLM 不可用时的降级：纯检索+统计模板，保证业务不中断"""
    titles = [h["title"] for h in hits[:5]]
    return {
        "事件概述": f"「{cluster['label']}」由 {cluster['n_articles']} 篇报道聚合而成，"
                    f"覆盖 {cluster['n_media']} 家媒体，热度 {cluster['heat_score']}（{cluster['level']}），"
                    f"所属板块类目：{cluster['category']}。",
        "关键信息": "；".join(t[:40] for t in titles[:3]),
        "市场影响": f"关联板块代码：{'、'.join(cluster.get('sectors') or []) or '本簇未携带板块标签'}。"
                    f"（LLM 不可用，未生成研判，以上为检索与统计事实）",
        "关注要点": "建议人工复核该事件的后续进展与板块联动。",
    }

# ================= 报告生成 =================
def cluster_key(cluster):
    return hashlib.md5(json.dumps(sorted(cluster["article_ids"])).encode()).hexdigest()[:16]

PROMPT_SYS = ("你是基金公司的投研分析助手。基于新闻材料写结构化分析。\n"
              "严格只输出一个JSON对象，四个字段的值都必须是【纯字符串】，不能是列表或嵌套对象。\n"
              "格式示例：\n"
              '{"事件概述":"央行宣布降准0.5个百分点，释放长期资金约1万亿元[1]。",'
              '"关键信息":"本次为年内第二次降准，覆盖除已执行5%存款准备金率的机构[2][3]。",'
              '"市场影响":"银行板块与地产链直接受益，短端利率下行[1][4]。",'
              '"关注要点":"后续MLF续作力度与信贷投放节奏[2]。"}\n'
              "每字段40-90字，只依据材料事实、不得编造数据，句末标引用编号。")

def build_report(llm, cluster, hits, retr_meta):
    ctx = "\n".join(f"[{i+1}]（{h['media']}·{h['published_at'][5:16]}）{h['title']}。"
                    f"{(h.get('content') or '')[:110]}" for i, h in enumerate(hits))
    user = (f"热点事件：{cluster['label']}\n"
            f"聚合情况：{cluster['n_articles']} 篇报道 / {cluster['n_media']} 家媒体 / "
            f"热度 {cluster['heat_score']}（{cluster['level']}）/ 类目 {cluster['category']}"
            f"{' / 关联板块 ' + '、'.join(cluster['sectors']) if cluster.get('sectors') else ''}\n\n"
            f"材料：\n{ctx}\n\n请输出JSON。")
    degraded, sections = 0, None
    try:
        for attempt in range(2):
            raw = llm.chat([{"role": "system", "content": PROMPT_SYS},
                            {"role": "user", "content": user}], 460)
            sections = parse_json_chain(raw)
            if sections: break
    except Exception:
        sections = None
    if not sections:
        sections, degraded = template_report(cluster, hits), 1
    cites = [dict(n=i + 1, title=h["title"][:60], media=h["media"], url=h.get("url", ""),
                  rrf=h["rrf"], bm25_rank=h["bm25_rank"], vec_rank=h["vec_rank"])
             for i, h in enumerate(hits)]
    return sections, cites, degraded

def run(top_n=5, verbose=True):
    t0 = time.time()
    conn = db()
    arts = conn.execute("SELECT id,title,content,url,media,channel,published_at,tokens "
                        "FROM processed_articles ORDER BY published_at").fetchall()
    cols = ["id", "title", "content", "url", "media", "channel", "published_at", "tokens"]
    docs = [dict(zip(cols, r)) for r in arts]
    tokens = [json.loads(d["tokens"] or "[]") for d in docs]
    emb = np.load(os.path.join(BASE, "data", "emb.npy"))
    assert len(emb) == len(docs), f"向量{len(emb)}≠文章{len(docs)}"
    retr = HybridRetriever(docs, tokens, emb)
    if verbose: print(f"[RAG] 索引 {len(docs)} 篇（BM25 + 向量双路）")

    cl_rows = conn.execute("SELECT label,category,n_articles,n_media,heat_score,importance,"
                           "sectors,article_ids FROM clusters ORDER BY heat_score DESC").fetchall()
    LEVELN = {5: "爆点", 4: "高热", 3: "升温中", 2: "一般"}
    clusters = [dict(label=r[0], category=r[1], n_articles=r[2], n_media=r[3], heat_score=r[4],
                     level=LEVELN.get(r[5], "一般"), sectors=json.loads(r[6] or "[]"),
                     article_ids=json.loads(r[7] or "[]")) for r in cl_rows]
    todo = clusters[:top_n]                      # Top-N 限流：LLM 成本控制
    if verbose: print(f"  热点 {len(clusters)} 个 → Top{len(todo)} 生成深度报告（限流）")

    llm, engine_name = None, "template(降级)"
    try:
        llm = get_llm(); engine_name = llm.name
    except Exception as e:
        if verbose: print(f"  ! LLM 不可用（{type(e).__name__}），全部走模板降级")

    exp = (datetime.now(CST) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM reports WHERE created_at < ?", (exp,))
    out, n_cache, n_degraded = [], 0, 0
    for i, c in enumerate(todo, 1):
        ck = cluster_key(c)
        hit = conn.execute("SELECT sections,citations,degraded,retrieval FROM reports "
                           "WHERE cluster_key=?", (ck,)).fetchone()
        if hit:                                   # 7 天缓存命中
            n_cache += 1
            out.append(dict(key=ck, label=c["label"], cached=True,
                            sections=json.loads(hit[0]), citations=json.loads(hit[1]),
                            degraded=hit[2], retrieval=json.loads(hit[3])))
            if verbose: print(f"  [{i}/{len(todo)}] 缓存命中 {c['label'][:26]}")
            continue
        # 混合检索：以簇标签为 query 召回关联报道（含簇外的补充材料）
        from dedup import tokenize
        q = c["label"]
        hits = retr.search(q, tokenize(q), k=8)
        # 归因口径：按各单路 Top-K 是否召回（而非候选池），否则会把"两路都进过池"
        # 误报成"两路等价"，掩盖排名分歧这一融合价值来源
        K = 8
        inb = lambda h: h["bm25_rank"] is not None and h["bm25_rank"] <= K
        inv = lambda h: h["vec_rank"] is not None and h["vec_rank"] <= K
        rmeta = dict(query=q[:40],
                     bm25_only=sum(1 for h in hits if inb(h) and not inv(h)),
                     vec_only=sum(1 for h in hits if inv(h) and not inb(h)),
                     both=sum(1 for h in hits if inb(h) and inv(h)),
                     neither_topk=sum(1 for h in hits if not inb(h) and not inv(h)))
        ts = time.time()
        if llm:
            sections, cites, deg = build_report(llm, c, hits, rmeta)
        else:
            sections, cites, deg = template_report(c, hits), [
                dict(n=j + 1, title=h["title"][:60], media=h["media"], url=h.get("url", ""),
                     rrf=h["rrf"], bm25_rank=h["bm25_rank"], vec_rank=h["vec_rank"])
                for j, h in enumerate(hits)], 1
        n_degraded += deg
        el = round(time.time() - ts, 1)
        conn.execute("INSERT OR REPLACE INTO reports(cluster_key,cluster_label,created_at,engine,"
                     "sections,citations,degraded,retrieval,elapsed) VALUES(?,?,?,?,?,?,?,?,?)",
                     (ck, c["label"], datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), engine_name,
                      json.dumps(sections, ensure_ascii=False), json.dumps(cites, ensure_ascii=False),
                      deg, json.dumps(rmeta, ensure_ascii=False), el))
        conn.commit()
        out.append(dict(key=ck, label=c["label"], cached=False, sections=sections,
                        citations=cites, degraded=deg, retrieval=rmeta, elapsed=el))
        if verbose:
            print(f"  [{i}/{len(todo)}] {c['label'][:24]} | 召回归因 仅BM25:{rmeta['bm25_only']}"
                  f" 仅向量:{rmeta['vec_only']} 双路:{rmeta['both']} 融合捞回:{rmeta['neither_topk']} | {el}s"
                  f"{' ⚠降级模板' if deg else ''}")
    # 治理指标：引用标注覆盖率——提示词要求句末标[n]，小模型常省略，
    # 这是"生成可溯源性"的量化护栏，低于阈值应触发人工复核而非直接发布
    def cite_cov(rep):
        txt = " ".join(rep["sections"].values())
        return 1 if re.search(r"\[\d+\]", txt) else 0
    n_cited = sum(cite_cov(r) for r in out if not r["degraded"])
    n_llm = sum(1 for r in out if not r["degraded"])
    stat = dict(engine=engine_name, n_clusters=len(clusters), n_reports=len(out),
                n_cached=n_cache, n_degraded=n_degraded,
                citation_marker_rate=round(n_cited / n_llm, 3) if n_llm else None,
                elapsed=round(time.time() - t0, 1), reports=out)
    json.dump(stat, open(os.path.join(BASE, "output", "rag_reports.json"), "w"),
              ensure_ascii=False, indent=1)
    conn.close()
    if verbose:
        print(f"\n生成 {len(out)} 份（缓存 {n_cache}、降级 {n_degraded}），引擎 {engine_name}，"
              f"总耗时 {stat['elapsed']}s")
        print(f"治理指标：引用标注覆盖率 {stat['citation_marker_rate']} "
              f"（提示词要求标注[n]，小模型常省略；低覆盖率应触发人工复核）")
    return stat

if __name__ == "__main__":
    import sys
    run(top_n=int(sys.argv[1]) if len(sys.argv) > 1 else 5)
