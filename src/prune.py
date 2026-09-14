# -*- coding: utf-8 -*-
"""清理过期数据，让每日自动运行的库不会无限膨胀。

保留窗口默认 14 天：够 2 天的分析窗口用，也够 7 天的指纹 TTL 和 RAG 缓存用。
"""
import os, sqlite3, sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(B, "data", "radar.db")


def run(keep_days=14):
    if not os.path.exists(DB):
        print("无数据库，跳过")
        return
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    cut = c.execute("select datetime(max(collected_at), ?) from raw_articles",
                    ("-%d days" % keep_days,)).fetchone()[0]
    if not cut:
        print("库为空，跳过")
        return
    before = c.execute("select count(*) from raw_articles").fetchone()[0]

    def wipe(table, col):
        try:
            c.execute("delete from %s where %s < ?" % (table, col), (cut,))
            return c.rowcount
        except sqlite3.OperationalError:
            return 0

    n = [wipe("raw_articles", "collected_at"),
         wipe("processed_articles", "published_at"),
         wipe("fingerprints", "created_at"),
         wipe("clusters", "run_at"),
         wipe("reports", "created_at"),
         wipe("collect_runs", "started_at"),
         wipe("dedup_runs", "run_at")]
    conn.commit()
    c.execute("vacuum")
    conn.commit()
    after = c.execute("select count(*) from raw_articles").fetchone()[0]
    print("保留 %s 之后的数据：资讯 %d → %d 条，其余表共清理 %d 行，库大小 %.1fMB"
          % (cut[:10], before, after, sum(n[1:]), os.path.getsize(DB) / 1e6))


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
