# -*- coding: utf-8 -*-
"""两级板块词表与分类器。

原版只有 8 个一级板块，做电子行研时太粗——「半导体」这一个筐里，
存储涨价、先进封装产能、设备国产化是完全不同的三条线，混在一起看不出结构。
这里把和电子最相关的三条线（半导体 / 消费电子 / AI 算力）拆到二级，
其余板块保留一级即可，避免为了对称而造出没人看的分类。

打分口径：关键词在「标题 + 正文前 240 字」中出现即计分，
长词权重高（长词更具体、误伤低），标题命中额外加权。
二级得分最高者决定一级，二级全不命中时退回一级词表。
"""
import re

TITLE_BOOST = 2.0          # 标题命中的权重倍数
BODY_CHARS = 240           # 正文取前多少字参与打分


# 跨板块的模糊词：公司名、太通用的名词。单独命中不足以定板块，压到 0.5。
# 「小米汽车副总裁」不该因为出现「小米」就被判成消费电子——这类误判全出在这批词上。
AMBIG = {
    "手机": .7, "新机": .7, "机型": .5,
    "小米": .5, "华为": .5, "苹果": .5, "三星电子": .6, "Meta": .5, "高通": .6,
    "英特尔": .6, "AMD": .6, "联想": .6, "京东方": .6, "电池": .4, "散热": .5,
    "供应链": .4, "终端": .3, "算法": .4, "AI": .4, "GPU": .8, "内存": .8,
    "颗粒": .4, "面板": .5, "快充": .5, "制裁": .3, "国产替代": .4, "出口管制": .5,
    "订阅": .4, "商业化": .4, "工作流": .4, "电力": .4, "机柜": .6, "储能配套": .6,
    "基金": .4, "分红": .4, "回购": .4, "增持": .4, "物流": .5, "货运": .5,
}
L1_FLOOR = 1.0            # 一级板块最低得分，达不到就进「综合」，不硬凑


def _w(kw):
    """长词更具体，权重更高；两字词容易误伤，压低；模糊词单列。"""
    if kw in AMBIG:
        return AMBIG[kw]
    n = len(kw)
    if n <= 2:
        return 0.6
    if n == 3:
        return 1.0
    return 1.4


SECTORS = [
  dict(key="semi", label="半导体", subs=[
    dict(key="memory", label="存储", kw=[
      "存储芯片", "存储器", "DRAM", "NAND", "HBM", "内存", "闪存", "颗粒", "存储模组",
      "DDR", "LPDDR", "UFS", "NOR", "美光", "海力士", "铠侠", "长江存储", "长鑫",
      "兆易创新", "江波龙", "佰维", "德明利", "存储涨价", "合约价", "现货价"]),
    dict(key="aichip", label="AI芯片与算力", kw=[
      "AI芯片", "算力芯片", "GPU", "ASIC", "NPU", "加速卡", "英伟达", "NVIDIA",
      "Rubin", "Blackwell", "TPU", "推理芯片", "训练芯片", "昇腾", "寒武纪",
      "海光", "算力租赁", "智算中心", "AI服务器", "光模块", "液冷", "博通",
      "Marvell", "自研芯片", "算力需求"]),
    dict(key="foundry", label="晶圆制造", kw=[
      "晶圆", "代工", "稼动率", "制程", "纳米", "台积电", "中芯国际", "华虹",
      "联电", "格芯", "成熟制程", "先进制程", "产能利用率", "流片", "IDM", "晶圆厂"]),
    dict(key="pkg", label="封装测试", kw=[
      "封测", "先进封装", "CoWoS", "SoIC", "倒装", "键合", "长电科技", "通富微电",
      "华天科技", "晶圆级封装", "扇出", "玻璃基板", "TGV", "chiplet", "2.5D", "3D封装"]),
    dict(key="equip", label="设备与材料", kw=[
      "光刻机", "光刻胶", "刻蚀", "薄膜沉积", "CMP", "离子注入", "量测", "清洗设备",
      "硅片", "电子特气", "溅射靶材", "抛光液", "ASML", "应用材料", "泛林", "东京电子",
      "北方华创", "中微公司", "拓荆", "盛美", "沪硅产业", "半导体设备", "半导体材料"]),
    dict(key="analog", label="模拟与功率", kw=[
      "模拟芯片", "功率半导体", "IGBT", "MOSFET", "碳化硅", "SiC", "氮化镓", "GaN",
      "电源管理", "PMIC", "士兰微", "斯达半导", "新洁能", "捷捷微电", "射频芯片", "MCU"]),
  ], kw=["半导体", "芯片", "集成电路", "晶圆", "EDA", "IP核", "芯片出口管制", "晶圆产能"]),

  dict(key="ce", label="消费电子", subs=[
    dict(key="phone", label="手机产业链", kw=[
      "智能手机", "手机", "手机出货", "新机", "旗舰机", "机型", "换机",
      "苹果", "iPhone", "华为", "小米", "OPPO",
      "vivo", "荣耀", "三星电子", "折叠屏", "供应链", "立讯精密", "歌尔", "蓝思",
      "京东方", "闻泰", "领益", "鸿海", "富士康"]),
    dict(key="pc", label="PC与平板", kw=[
      "PC出货", "笔记本电脑", "AI PC", "联想", "戴尔", "惠普", "华硕", "平板电脑",
      "个人电脑", "Windows", "英特尔", "AMD", "高通"]),
    dict(key="xr", label="AI眼镜与XR", kw=[
      "AI眼镜", "智能眼镜", "XR", "AR", "VR", "MR", "头显", "光波导", "Micro LED",
      "Meta", "Ray-Ban", "雷鸟", "Rokid", "Vision Pro", "空间计算"]),
    dict(key="comp", label="零组件", kw=[
      "MLCC", "被动元件", "连接器", "摄像头模组", "CIS", "光学镜头", "声学",
      "结构件", "散热", "电池", "快充", "面板", "OLED", "LCD", "PCB", "覆铜板", "CCL"]),
  ], kw=["消费电子", "终端", "出货量", "BOM", "代工组装", "ODM", "EMS"]),

  dict(key="ai", label="AI与大模型", subs=[
    dict(key="model", label="大模型", kw=[
      "大模型", "GPT", "OpenAI", "Anthropic", "Claude", "Gemini", "DeepSeek",
      "通义", "文心", "豆包", "Llama", "参数量", "预训练", "多模态", "开源模型"]),
    dict(key="edge", label="端侧AI", kw=[
      "端侧", "边缘计算", "本地推理", "AI手机", "AI PC", "设备端", "离线推理",
      "模型量化", "蒸馏", "小模型"]),
    dict(key="app", label="AI应用与智能体", kw=[
      "智能体", "Agent", "AIGC", "AI应用", "Copilot", "RAG", "工作流", "AI编程",
      "商业化", "订阅", "token", "调用量"]),
    dict(key="infra", label="AI基建", kw=[
      "数据中心", "IDC", "机柜", "电力", "储能配套", "网络设备", "交换机",
      "AI基建", "资本开支", "capex", "超算"]),
  ], kw=["人工智能", "AI", "机器学习", "深度学习", "算法"]),

  dict(key="macro", label="宏观政策", subs=[], kw=[
    "央行", "降准", "降息", "LPR", "GDP", "PMI", "CPI", "PPI", "财政", "国债",
    "社融", "货币政策", "统计局", "海关", "进出口", "工信部", "发改委", "专项债"]),
  dict(key="global", label="国际市场", subs=[], kw=[
    "美联储", "美元", "汇率", "关税", "贸易", "地缘", "原油", "黄金", "非农",
    "欧央行", "加息", "降息预期", "美债", "纳斯达克", "标普"]),
  dict(key="energy", label="新能源", subs=[], kw=[
    "新能源", "锂电", "光伏", "储能", "氢能", "风电", "碳酸锂", "电池", "碳中和", "硅料"]),
  dict(key="auto", label="汽车出行", subs=[], kw=[
    "汽车", "智驾", "车企", "机器人", "低空", "新能源车", "无人机", "特斯拉",
    "比亚迪", "激光雷达", "座舱"]),
  dict(key="bio", label="生物医药", subs=[], kw=[
    "医药", "创新药", "疫苗", "医疗", "集采", "临床", "药企", "生物", "CXO", "器械"]),
  dict(key="fin", label="金融市场", subs=[], kw=[
    "券商", "保险", "银行", "A股", "港股", "基金", "证监会", "IPO", "北向",
    "南向", "沪深", "回购", "增持", "解禁", "恒生指数", "上证指数", "深证成指",
    "创业板指", "科创板", "可转债", "定增", "配股", "股东大会", "分红"]),
  dict(key="commodity", label="大宗与能化", subs=[], kw=[
    "期货", "原油", "螺纹钢", "铁矿", "煤焦", "动力煤", "有色", "铜价", "铝价",
    "贵金属", "白银", "化工", "农产品", "白糖", "棉花", "LPG", "玻璃", "纯碱",
    "甲醇", "尿素", "生猪", "豆粕", "天然气", "柴油", "汽油", "主力合约"]),
  dict(key="property", label="地产建筑", subs=[], kw=[
    "房地产", "楼市", "REITs", "基建", "建筑", "水泥", "房企", "土拍", "物业",
    "保交楼", "住房", "商品房", "工程机械", "钢结构"]),
  dict(key="retail", label="消费零售", subs=[], kw=[
    "零售", "白酒", "食品饮料", "家电", "纺织服装", "餐饮", "免税", "电商",
    "旅游", "酒店", "预制菜", "乳业", "啤酒", "商超", "消费券", "以旧换新"]),
  dict(key="transport", label="交运物流", subs=[], kw=[
    "航空", "机场", "航运", "港口", "快递", "铁路", "高速公路", "物流",
    "集运", "运价", "干散货", "民航", "货运"]),
]

MISC = dict(key="misc", label="综合", subs=[], kw=[])

# 供外部直接使用的扁平索引
L1 = {s["key"]: s["label"] for s in SECTORS}
L1[MISC["key"]] = MISC["label"]
L2 = {(s["key"], b["key"]): b["label"] for s in SECTORS for b in s["subs"]}
SUB_OF = {s["key"]: [(b["key"], b["label"]) for b in s["subs"]] for s in SECTORS}


def _score(text, title, kws):
    sc = 0.0
    for k in kws:
        if k in text:
            sc += _w(k)
            if k in title:
                sc += _w(k) * (TITLE_BOOST - 1)
    return sc


def classify(article):
    """返回 dict(l1, l1_label, l2, l2_label, score)。l2 可能为 None。"""
    title = str(article.get("title") or "")
    text = title + " " + str(article.get("content") or "")[:BODY_CHARS]

    best = (0.0, None, None)          # (score, sector, sub)
    for s in SECTORS:
        for b in s["subs"]:
            sc = _score(text, title, b["kw"])
            if sc > best[0]:
                best = (sc, s, b)

    if best[0] >= 1.4:                # 二级需要至少一个具体词命中
        s, b = best[1], best[2]
        return dict(l1=s["key"], l1_label=s["label"], l2=b["key"],
                    l2_label=b["label"], score=round(best[0], 2))

    # 二级不成立 → 退回一级
    bl1 = (0.0, MISC)
    for s in SECTORS:
        sc = _score(text, title, s["kw"])
        # 一级也吃二级词的一半分，避免「存储涨价」这种只写了二级词的稿子掉进综合
        for b in s["subs"]:
            sc += 0.5 * _score(text, title, b["kw"])
        if sc > bl1[0]:
            bl1 = (sc, s)
    s = bl1[1] if bl1[0] >= L1_FLOOR else MISC
    return dict(l1=s["key"], l1_label=s["label"], l2=None, l2_label=None,
                score=round(bl1[0], 2))


if __name__ == "__main__":
    for t in ["存储芯片合约价环比再涨，DRAM 供需紧张延续至 2027",
              "台积电 CoWoS 产能被英伟达博通锁定八成以上",
              "北方华创中标某 12 寸产线刻蚀设备订单",
              "苹果首款折叠屏 iPhone 发布，起售价约 2000 美元",
              "Meta 智能眼镜出货同比翻倍，光波导方案成本下降",
              "央行等量续作 5000 亿元买断式逆回购",
              "某券商 8 月净利润同比增长"]:
        r = classify(dict(title=t, content=""))
        print("%-8s / %-8s  %5.1f  %s" % (r["l1_label"], r["l2_label"] or "—", r["score"], t[:30]))
