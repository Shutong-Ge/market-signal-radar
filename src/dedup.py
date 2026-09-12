# -*- coding: utf-8 -*-
"""
投研舆情雷达 · 三层去重
=======================
财经资讯的重复有三种形态，一层解决不了，所以分三层，每层解决一种：
  L1 精确去重：同一篇稿件被多个频道收录（URL 或标题完全相同）——哈希，O(1)
  L2 近似去重：同一通稿被不同媒体改写标题/删节（"央行降准0.5个百分点" vs
     "【快讯】央行宣布降准0.5个百分点"）——自研 64 位 SimHash，汉明距离 ≤3
  L3 语义去重：同一事件的不同表述（用词不同但说的是一件事）——bge 向量余弦 ≥0.90

工程细节（面试点）：
  · SimHash 用 jieba 分词 + 词频加权，比等权重更抗噪（高频虚词权重被稀释）
  · 分块索引（64位切4段16位）做候选召回，避免 O(n²) 两两比对
  · 指纹跨批次持久化（SQLite 表，7天TTL）——原方案用 Redis，此处降级为
    本地表，同样实现"今天采到昨天已处理过的稿件时直接跳过"
"""
import os, re, json, sqlite3, hashlib, time
from datetime import datetime, timedelta, timezone
import numpy as np
import jieba
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "radar.db")
CST = timezone(timedelta(hours=8))
FP_TTL_DAYS = 7
HAMMING_MAX = 3
COS_MIN = 0.90

STOP = set("""的 了 是 在 和 与 及 或 也 都 就 而 但 并 等 被 把 对 从 向 为 以 于 由 中 上 下 内 外
一 二 三 四 五 六 七 八 九 十 个 之 其 该 此 这 那 有 无 不 将 已 未 可 能 会 要 到 后 前 时 日 月 年
公司 股份 有限 集团 消息 报道 记者 表示 称 据 显示 发布 宣布 一个 进行 相关 方面 情况 问题 目前 今日""".split())

def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS processed_articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, raw_id INTEGER UNIQUE,
        title TEXT, content TEXT, url TEXT, media TEXT, channel TEXT,
        published_at TEXT, simhash TEXT, tokens TEXT, raw TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS fingerprints(
        simhash TEXT PRIMARY KEY, raw_id INTEGER, title TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS dedup_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, window_start TEXT,
        n_in INTEGER, l1 INTEGER, l2 INTEGER, l3 INTEGER, n_out INTEGER, elapsed REAL)""")
    return c

# ---------------- 分词 ----------------
def tokenize(text):
    """语义/聚类用分词：剥离数字与英文，聚焦主题词"""
    text = re.sub(r"[a-zA-Z0-9]+", " ", str(text or ""))
    return [w for w in jieba.lcut(text)
            if len(w) >= 2 and w not in STOP and not re.match(r"^[\W_]+$", w)]

NUM_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|亿|万|千|百|个基点|点|美元|港元|元|年期|倍)?")

def tokenize_sim(text):
    """SimHash 专用分词：★保留数字★
    财经资讯里数字就是内容——"南向资金净卖出30亿" 与 "…50亿" 是两条不同的行情快讯，
    若沿用通用文本处理剥离数字，二者指纹完全相同会被误判为重复。
    实测该缺陷曾导致 8 条不同事实被误去重（国债期限、成交额里程碑、涨跌幅阈值）。"""
    t = str(text or "")
    nums = [m.group(0).strip() for m in NUM_PAT.finditer(t) if any(c.isdigit() for c in m.group(0))]
    words = [w for w in jieba.lcut(re.sub(r"[a-zA-Z]+", " ", t))
             if len(w) >= 2 and w not in STOP and not re.match(r"^[\W_]+$", w)]
    return words + nums * 2      # 数字 token 加倍计权，突出其区分度

# ---------------- L2: 自研 64 位 SimHash ----------------
def simhash64(tokens):
    """词频加权的 64 位 SimHash：每个词取 md5 前 64 位，按位投票（权重=词频），
    最终每位正得 1、负得 0。相似文本的指纹汉明距离很小。"""
    if not tokens:
        return 0
    v = np.zeros(64, dtype=np.float64)
    for w, tf in Counter(tokens).items():
        h = int(hashlib.md5(w.encode()).hexdigest()[:16], 16)
        weight = 1.0 + np.log(tf)                      # 词频加权（对数抑制长文本）
        bits = np.array([(h >> i) & 1 for i in range(64)], dtype=np.float64)
        v += weight * (bits * 2 - 1)                   # 1→+w, 0→-w
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    return out

def hamming(a, b):
    return bin(a ^ b).count("1")

def band_keys(h, bands=4, width=16):
    """分块索引：64位切4段×16位，任意一段相同才进入精确比对候选集。
    汉明距离≤3 时，鸽巢原理保证至少有一段完全相同 → 召回不漏。"""
    return [(i, (h >> (i * width)) & ((1 << width) - 1)) for i in range(bands)]

# ---------------- L3: 语义向量 ----------------
_ENC = None
def encoder():
    global _ENC
    if _ENC is None:
        from sentence_transformers import SentenceTransformer
        _ENC = SentenceTransformer("BAAI/bge-small-zh-v1.5",
                                   cache_folder=os.environ.get("HF_HOME", "/home/claude/hf_cache"))
    return _ENC

# ---------------- 主流程 ----------------
def norm_title(t):
    """标题归一化：去掉【】里的栏目标记、快讯前缀、空白与标点"""
    t = re.sub(r"^[【\[].{0,8}[】\]]\s*", "", str(t or ""))
    t = re.sub(r"^(快讯|独家|突发|要闻|财经早知道)[:：|]?\s*", "", t)
    return re.sub(r"[\s\W_]+", "", t).lower()

def run(window_days=2, use_semantic=True, verbose=True):
    t0 = time.time()
    conn = db()
    since = (datetime.now(CST) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id,title,content,url,media,channel,published_at,raw FROM raw_articles "
        "WHERE published_at >= ? ORDER BY published_at", (since,)).fetchall()
    n_in = len(rows)
    if verbose: print(f"[去重] 窗口 {since} 起，输入 {n_in} 条")

    # 清理过期指纹（7天TTL）
    exp = (datetime.now(CST) - timedelta(days=FP_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM fingerprints WHERE created_at < ?", (exp,))

    # ---- L1 精确去重 ----
    seen_title, l1_keep = {}, []
    l1, ex1 = 0, []
    for r in rows:
        k = norm_title(r[1])
        if k in seen_title:
            l1 += 1
            if len(ex1) < 8: ex1.append(dict(kept=seen_title[k], dropped=f"[{r[4]}] {r[1]}"))
            continue
        seen_title[k] = f"[{r[4]}] {r[1]}"; l1_keep.append(r)
    if verbose: print(f"  L1 精确（标题归一化哈希）: 拦截 {l1} 条 → 剩 {len(l1_keep)}")

    # ---- L2 SimHash 近似去重（含跨批次指纹） ----
    # 跨批次指纹：排除当前输入集自身写入的指纹，保证重跑幂等
    cur_ids = {r[0] for r in rows}
    prev_fp = {int(x[0]): x[1] for x in conn.execute(
        "SELECT simhash,title,raw_id FROM fingerprints") if x[2] not in cur_ids}
    index, l2_keep, l2, l2_cross = {}, [], 0, 0
    h2title, ex2 = {}, []
    for r in l1_keep:
        toks = tokenize(f"{r[1]} {(r[2] or '')[:200]}")
        h = simhash64(tokenize_sim(f"{r[1]} {(r[2] or '')[:200]}"))
        dup = False
        # 跨批次：与历史指纹比对
        for ph in prev_fp:
            if hamming(h, ph) <= HAMMING_MAX:
                dup = True; l2_cross += 1; break
        if not dup:
            cand = set()
            for bk in band_keys(h):
                cand |= index.get(bk, set())
            for ch in cand:
                d = hamming(h, ch)
                if d <= HAMMING_MAX:
                    dup = True
                    if len(ex2) < 8:
                        ex2.append(dict(kept=h2title.get(ch, ""), dropped=f"[{r[4]}] {r[1]}", hamming=d))
                    break
        if dup:
            l2 += 1; continue
        for bk in band_keys(h):
            index.setdefault(bk, set()).add(h)
        h2title[h] = f"[{r[4]}] {r[1]}"
        l2_keep.append((r, h, toks))
    if verbose:
        print(f"  L2 近似（SimHash 汉明距≤{HAMMING_MAX}）: 拦截 {l2} 条"
              f"（其中跨批次指纹命中 {l2_cross}）→ 剩 {len(l2_keep)}")

    # ---- L3 语义去重 ----
    l3 = 0
    final = l2_keep
    ex3 = []
    if use_semantic and len(l2_keep) > 1:
        texts = [f"{r[1]} {(r[2] or '')[:120]}" for r, _, _ in l2_keep]
        emb = encoder().encode(texts, batch_size=128, normalize_embeddings=True,
                               show_progress_bar=False).astype("float32")
        keep_idx, kept_vecs, ex3 = [], [], []
        for i in range(len(l2_keep)):
            if kept_vecs:
                sims = np.dot(np.array(kept_vecs), emb[i])
                j = int(sims.argmax())
                if sims[j] >= COS_MIN:
                    l3 += 1
                    if len(ex3) < 10:
                        ex3.append(dict(kept=f"[{l2_keep[keep_idx[j]][0][4]}] {l2_keep[keep_idx[j]][0][1]}",
                                        dropped=f"[{l2_keep[i][0][4]}] {l2_keep[i][0][1]}",
                                        cos=round(float(sims[j]), 4)))
                    continue
            keep_idx.append(i); kept_vecs.append(emb[i])
        final = [l2_keep[i] for i in keep_idx]
        emb_final = emb[keep_idx]
        if verbose: print(f"  L3 语义（bge 余弦≥{COS_MIN}）: 拦截 {l3} 条 → 剩 {len(final)}")
        np.save(os.path.join(BASE, "data", "emb.npy"), emb_final)

    # ---- 落库 ----
    conn.execute("DELETE FROM processed_articles")
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    for r, h, toks in final:
        conn.execute("INSERT OR REPLACE INTO processed_articles(raw_id,title,content,url,media,"
                     "channel,published_at,simhash,tokens,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (r[0], r[1], r[2], r[3], r[4], r[5], r[6], str(h),
                      json.dumps(toks, ensure_ascii=False), r[7]))
        conn.execute("INSERT OR REPLACE INTO fingerprints(simhash,raw_id,title,created_at)"
                     " VALUES(?,?,?,?)", (str(h), r[0], r[1], now))
    el = round(time.time() - t0, 1)
    conn.execute("INSERT INTO dedup_runs(run_at,window_start,n_in,l1,l2,l3,n_out,elapsed)"
                 " VALUES(?,?,?,?,?,?,?,?)", (now, since, n_in, l1, l2, l3, len(final), el))
    conn.commit(); conn.close()
    stat = dict(n_in=n_in, l1=l1, l2=l2, l2_cross=l2_cross, l3=l3, n_out=len(final),
                dedup_rate=round(1 - len(final) / max(n_in, 1), 3), elapsed=el,
                examples=dict(L1=ex1, L2=ex2, L3=ex3))
    json.dump(stat, open(os.path.join(BASE, "output", "dedup_report.json"), "w"),
              ensure_ascii=False, indent=1)
    if verbose:
        print(f"\n输入 {n_in} → 输出 {len(final)}，整体去重率 {stat['dedup_rate']:.1%}，耗时 {el}s")
    return stat

if __name__ == "__main__":
    import sys
    run(window_days=int(sys.argv[1]) if len(sys.argv) > 1 else 2)
