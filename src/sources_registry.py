# -*- coding: utf-8 -*-
"""数据源登记表：一份诚实的「接了什么 / 还能接什么 / 接不了什么」。

做投研工具最容易犯的错，是把一张听起来很全的数据源清单写进介绍里，
但其中一半根本拿不到权限。这张表把三种状态分开写，页面上原样展示：

  live      已接入，当期真实跑通（站点状态表里能看到抓取量）
  public    公开可接入，本版未启用（给出未启用的具体原因）
  licensed  商业终端 / 非公开库，个人项目无法合法接入——只留适配器接口位

最后一类不是「没做」，是「不能做」。架构上按同一个 adapter 协议预留，
生产环境拿到机构席位后补上实现即可；在此之前页面如实标注未接入。
"""

REGISTRY = [
  # ── 已接入 ────────────────────────────────────────────────────────
  dict(g="新闻资讯", name="新浪财经（财经/科技/国际/股市滚动）", s="live",
       note="4 个频道 × 翻页，单次约 900 条"),
  dict(g="新闻资讯", name="东方财富 全球快讯", s="live",
       note="自带 stockList 关联板块代码，热点可直接落到板块"),
  dict(g="新闻资讯", name="华尔街见闻 全球快讯", s="live", note="—"),
  dict(g="新闻资讯", name="中新网 财经", s="live", note="RSS"),
  dict(g="法定披露", name="巨潮资讯 公告全文检索", s="live",
       note="沪深两市法定指定披露平台；按板块关键词检索，不做全市场灌入"),
  dict(g="境外披露", name="SEC EDGAR 全文检索", s="live",
       note="8-K 等表单；电子链上的设备、存储、封装对标公司多在美股"),

  # ── 公开可接入，本版未启用 ────────────────────────────────────────
  dict(g="法定披露", name="上交所 / 深交所 公告", s="public",
       note="与巨潮同源——巨潮是两所的法定指定披露平台，重复接入只会放大去重压力"),
  dict(g="法定披露", name="港交所 披露易", s="public",
       note="页面可达；检索需表单会话，适配器待写"),
  dict(g="互动问答", name="上证 e 互动 / 深交所互动易", s="public",
       note="页面可达；问答是「公司表述」而非披露事实，需单独标注来源层级后再入库"),
  dict(g="宏观数据", name="工信部 / 财政部 / 央行", s="public",
       note="站点可达；发布节奏低频，按日跑的舆情窗口里意义有限，更适合单独的数据日历"),
  dict(g="宏观数据", name="国家统计局 / 海关总署", s="public",
       note="实测有反爬（403 / 412），需要更完整的会话与频率控制"),
  dict(g="新闻资讯", name="财联社 / 证券时报 / 中国证券报 / 上海证券报", s="public",
       note="站点可达；财联社接口需签名，其余待写适配器"),
  dict(g="法定披露", name="业绩说明会 / 投资者关系活动记录表", s="public",
       note="本身就是巨潮上的公告类型，已在公告源的覆盖范围内，可按 category 单独取"),

  # ── 需机构授权，不可接入 ──────────────────────────────────────────
  dict(g="商业终端", name="Wind 金融终端", s="licensed", note="按机构席位授权，无公开 API"),
  dict(g="商业终端", name="同花顺 iFinD", s="licensed", note="同上"),
  dict(g="商业终端", name="东方财富 Choice", s="licensed", note="同上"),
  dict(g="商业终端", name="Bloomberg Terminal", s="licensed", note="席位授权，数据再分发受限"),
  dict(g="商业终端", name="LSEG Workspace（原 Refinitiv）", s="licensed", note="同上"),
  dict(g="商业终端", name="FactSet", s="licensed", note="同上"),
  dict(g="商业终端", name="S&P Capital IQ Pro", s="licensed", note="同上"),
  dict(g="内部库", name="券商研究报告 / 研究所内部报告库", s="licensed",
       note="非公开，且多数带合规分发限制"),
]

LABEL = {"live": "已接入", "public": "公开可接入 · 本版未启用", "licensed": "需机构授权 · 未接入"}


def summary():
    n = {k: 0 for k in LABEL}
    for r in REGISTRY:
        n[r["s"]] += 1
    return n


if __name__ == "__main__":
    n = summary()
    print("已接入 %d · 公开可接入 %d · 需授权 %d" % (n["live"], n["public"], n["licensed"]))
    for st in ("live", "public", "licensed"):
        print("\n== %s ==" % LABEL[st])
        for r in REGISTRY:
            if r["s"] == st:
                print("  [%s] %s —— %s" % (r["g"], r["name"], r["note"]))
