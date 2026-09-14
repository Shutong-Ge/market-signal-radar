# -*- coding: utf-8 -*-
"""把一次完整运行的产物汇总成 output/ui_data.json（工作台页面的唯一数据源）。

在此之前 ui_data.json 是手工拼的，仓库里没有生成脚本——也就是说
克隆下来重跑一遍管道，是拼不出工作台页面的。这个脚本补上这一环，
让「跑管道 → 出页面」在 CI 里能一键完成。

读取：data/radar.db（采集/聚类/报告）+ output/{dedup,cluster,retrieval_eval}.json
输出：output/ui_data.json
"""
import json, os, sqlite3

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(B, "data", "radar.db")
OUT = os.path.join(B, "output")
LEVEL_OF = {5: "爆点", 4: "高热", 3: "升温中", 2: "一般"}


def jload(name, default=None):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        return default
    return json.load(open(p, encoding="utf-8"))


def build_collect(cur):
    """按站点 key 汇总所有采集批次——同一频道翻页抓的多次要合并，不能只截前几条。"""
    fetched = inserted = dup = 0
    sites = {}
    for f, i, d, detail in cur.execute(
            "select fetched, inserted, dup_url, detail from collect_runs order by id"):
        fetched += f or 0
        inserted += i or 0
        dup += d or 0
        for s in json.loads(detail or "[]"):
            # 翻页站点 sina_finance_p2/p3… 归并回主频道
            key = s["key"].split("_p")[0]
            cur_s = sites.setdefault(key, dict(key=key, media=s["media"], channel=s["channel"],
                                               fetched=0, inserted=0, status="ok"))
            cur_s["fetched"] += s.get("fetched", 0)
            cur_s["inserted"] += s.get("inserted", 0)
            if s.get("status") != "ok" and cur_s["status"] == "ok":
                cur_s["status"] = s["status"]
    total = cur.execute("select count(*) from raw_articles").fetchone()[0]
    media_n = cur.execute("select count(distinct media) from raw_articles").fetchone()[0]
    return dict(fetched=fetched, inserted=inserted, dup=dup, media_n=media_n,
                total=total, sites=list(sites.values()))


def build_clusters(cur):
    run_at = cur.execute("select max(run_at) from clusters").fetchone()[0]
    rows = cur.execute(
        "select label, category, n_articles, n_media, heat_score, importance, "
        "heat_factors, time_span, sectors from clusters where run_at=? "
        "order by heat_score desc", (run_at,)).fetchall()
    out = []
    for lb, cat, n, nm, heat, imp, fac, span, sec in rows:
        out.append(dict(label=lb, category=cat, n=n, media=nm,
                        heat=round(heat or 0, 3), level=LEVEL_OF.get(imp, "一般"),
                        factors=json.loads(fac or "{}"), span=span,
                        sectors=json.loads(sec or "[]")))
    return out, run_at


def main():
    cur = sqlite3.connect(DB).cursor()
    dd = jload("dedup_report.json", {}) or {}
    cl = jload("cluster_report.json", {}) or {}
    rg = jload("rag_reports.json", {}) or {}
    ev = jload("retrieval_eval.json", {}) or {}
    clusters, run_at = build_clusters(cur)

    data = {
        "collect": build_collect(cur),
        "dedup": dict(n_in=dd.get("n_in", 0), l1=dd.get("l1", 0), l2=dd.get("l2", 0),
                      l3=dd.get("l3", 0), n_out=dd.get("n_out", 0),
                      rate=dd.get("dedup_rate", 0), elapsed=dd.get("elapsed", 0),
                      examples=dd.get("examples", {})),
        "cluster": dict(components=cl.get("n_components", 0), edges=cl.get("edges", 0),
                        noise=cl.get("n_noise_filtered", 0),
                        single=cl.get("n_single_media_filtered", 0),
                        n=cl.get("n_clusters", len(clusters)),
                        calib=cl.get("calibration", {}), levels=cl.get("level_dist", {}),
                        elapsed=cl.get("elapsed", 0), mode=cl.get("mode", "vector")),
        "clusters": clusters,
        "reports": rg.get("reports", []),
        "rag": dict(engine=rg.get("engine", "—"), n=rg.get("n_reports", 0),
                    degraded=rg.get("n_degraded", 0),
                    cite_rate=rg.get("citation_marker_rate", 0),
                    elapsed=rg.get("elapsed", 0)),
        "eval": ev,
    }
    p = os.path.join(OUT, "ui_data.json")
    os.makedirs(OUT, exist_ok=True)
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    print("ui_data.json ← 聚类批次 %s：%d 个热点 / %d 篇深度报告" %
          (run_at, len(clusters), len(data["reports"])))


if __name__ == "__main__":
    main()
