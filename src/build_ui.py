# -*- coding: utf-8 -*-
"""装配单文件工作台：把 output/ui_data.json 注入模板。

同时从 radar.db 读出这一批数据真实的时间窗，写进 D.meta。
页面是静态导出，不会自己更新——顶栏必须把「数据截止到哪一天」写清楚，
否则隔几天再打开，看到的是旧新闻却顶着「当日采集」的标签。
"""
import json, os, sqlite3

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tpl = open(os.path.join(B, "src", "ui_template.html"), encoding="utf-8").read()
data = json.load(open(os.path.join(B, "output", "ui_data.json"), encoding="utf-8"))


def read_window(db_path, window_days):
    """返回 (built_at, win_start, win_end)，读不到就返回 None。"""
    if not os.path.exists(db_path):
        return None
    try:
        cur = sqlite3.connect(db_path).cursor()
        built = cur.execute("select max(collected_at) from raw_articles").fetchone()[0]
        if not built:
            return None
        lo = cur.execute(
            "select min(published_at), max(published_at) from raw_articles "
            "where published_at >= datetime(?, ?)",
            (built, "-%d days" % int(window_days or 2)),
        ).fetchone()
        return built, lo[0], lo[1]
    except Exception:
        return None


w = read_window(os.path.join(B, "data", "radar.db"),
                data.get("cluster", {}).get("calib", {}).get("window_days", 2))
if w:
    built, lo, hi = w
    data["meta"] = {"built_at": built, "win_start": lo, "win_end": hi}

dst = os.path.join(B, "投研舆情雷达_热点监控工作台.html")
open(dst, "w", encoding="utf-8").write(
    tpl.replace("const D=__DATA__;", "const D=" + json.dumps(data, ensure_ascii=False) + ";"))
print("built %s %.0fKB" % (os.path.basename(dst), os.path.getsize(dst) / 1024))
if w:
    print("数据窗口 %s ~ %s，生成于 %s" % (w[1], w[2], w[0]))
