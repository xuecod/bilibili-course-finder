#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili 网课检索与内容体检。

三层信号：
  1 元数据层  播放/收藏/分P/发布日        —— 判断"有多少人认可"
  2 结构层    分P标题列表 = 课程大纲        —— 判断"讲得全不全"
  3 内容层    弹幕 + 评论 + (可选)字幕      —— 判断"讲得怎么样、适合谁"

只用标准库。接口细节见 ../references/bili-api.md，打分口径见 ../references/scoring.md。

常用：
  python bili.py find  --config job.json -o pool.json     # 召回+粗筛（元数据层）
  python bili.py probe --bvid BV1Rs411c7HG                # 单课深度体检（结构+内容层）
  python bili.py deep  --config job.json -o final.json    # 一条龙：粗筛 Top N 再逐个体检
  python bili.py search --keyword 线性代数

可选解锁字幕（需登录态）：
  set BILI_SESSDATA=xxxx   （浏览器 F12 -> Application -> Cookies -> SESSDATA）
"""

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 缺 Origin / Sec-Fetch-* 会直接 412，这几个头不是可选项
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "Accept-Encoding": "identity",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]

CACHE = os.path.join(tempfile.gettempdir(), "bili_course_cache.json")
CACHE_TTL = 600

TIMELESS_HINTS = ["数学", "线性代数", "高等数学", "概率", "离散", "算法", "数据结构",
                  "组成原理", "计算机组成", "操作系统原理", "计算机网络", "密码学", "汇编",
                  "编译原理", "体系结构", "微积分", "物理", "电路"]

GOOD_WORDS = ["完整", "全集", "全", "系统", "从零", "精讲", "详解", "原理", "入门", "教程",
              "第", "讲", "课", "全套", "零基础", "深入", "进阶", "实战", "公开课"]
SEO_WORDS = ["零基础", "精通", "从浅入深", "由浅入深", "适合小白", "小白", "迅速", "快速掌握",
             "保姆级", "全套", "必备", "干货", "从入门到", "看这一个就够", "强烈推荐"]
BAD_WORDS = ["速成", "分钟", "秒懂", "必看", "震惊", "别再", "一次搞懂", "三天", "考前", "押题"]

# 弹幕词典：用命中率反推课程属性，比 N-gram 靠谱。
# 注意别放单字（"难""拖"）和纯数字（"408" 会命中 "01408B"），误伤比漏检更坏。
LEXICON = {
    "困惑": ["听不懂", "没懂", "不懂", "什么意思", "为啥", "为什么", "太快", "跟不上",
             "救命", "劝退", "懵", "迷糊", "太难", "怎么回事", "没讲清"],
    "顺畅": ["懂了", "明白了", "清晰", "讲得好", "讲的真好", "牛", "谢谢", "恍然大悟",
             "有道理", "通透", "舒服", "精辟", "干货", "有用", "好评", "爱了", "神了"],
    "节奏": ["倍速", "1.5", "2倍", "1.25", "跳过", "快进", "太慢", "拖沓", "啰嗦", "废话", "水文"],
    "人群": ["考研", "期末", "考试", "作业", "大学", "我们老师", "教材", "自学",
             "专升本", "高考", "面试", "复试", "课设", "毕设", "零基础"],
    "氛围": ["笑死", "哈哈", "doge", "绷不住", "好家伙", "泪目", "破防", "awsl"],
    # 下面两类是给非学习类视频用的：找吃的、找攻略、找测评时，"踩雷"比"听不懂"重要
    "踩坑": ["踩雷", "智商税", "翻车", "别买", "退货", "后悔", "避雷", "不推荐", "坑人", "虚假"],
    "实效": ["学会了", "成功了", "有用", "有效", "好吃", "好看", "管用", "解决", "实测", "亲测"],
}

# 不同场景该看重什么。总分恒为 100，只是权重挪位置。
WEIGHTS = {
    # 找体系课：能讲 26 小时 vs 7 小时，是本质差别
    "course": {"结构": 30, "口碑": 25, "相关": 25, "时效": 10, "标题": 10},
    # 泛搜：不知道用户要什么，让"相关"和"口碑"说话
    "any":    {"结构": 12, "口碑": 28, "相关": 30, "时效": 12, "标题": 8},
    # 短视频/单集：讲清楚一个点就行，分P 数完全不该参与打分
    "clip":   {"结构": 0,  "口碑": 32, "相关": 36, "时效": 14, "标题": 8},
}


def log(msg):
    print(msg, file=sys.stderr)


def http_bytes(url, cookie=None, referer="https://www.bilibili.com/"):
    h = dict(HEADERS)
    h["Referer"] = referer
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def http_json(url, cookie=None):
    try:
        return json.loads(http_bytes(url, cookie).decode("utf-8", "replace"))
    except Exception as e:
        return {"_err": str(e)}


def inflate(data):
    """弹幕 XML 是 raw deflate，不处理会得到一堆乱码。"""
    if not data:
        return b""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except Exception:
            pass
    for wbits in (-15, 15 + 32, 31):
        try:
            return zlib.decompressobj(wbits).decompress(data)
        except Exception:
            continue
    return b""


class Bili:
    def __init__(self, delay=0.8, sessdata=None):
        self.delay = delay
        self.sessdata = (sessdata or os.environ.get("BILI_SESSDATA", "")).strip()
        self.cookie = ""
        self.mixin = ""
        self._last = 0.0
        if not self._load_cache():
            self.refresh_session()

    # ---------- session ----------
    def _load_cache(self):
        try:
            with open(CACHE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < CACHE_TTL and c.get("mixin"):
                self.cookie, self.mixin = c["cookie"], c["mixin"]
                if self.sessdata:
                    self.cookie += "; SESSDATA=" + self.sessdata
                return True
        except Exception:
            pass
        return False

    def _save_cache(self):
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "cookie": self.cookie, "mixin": self.mixin}, f)
        except Exception:
            pass

    def refresh_session(self):
        spi = http_json("https://api.bilibili.com/x/frontend/finger/spi").get("data") or {}
        base = "buvid3=%s; buvid4=%s" % (spi.get("b_3", ""), spi.get("b_4", ""))
        self.cookie = base + ("; SESSDATA=" + self.sessdata if self.sessdata else "")
        nav = http_json("https://api.bilibili.com/x/web-interface/nav", self.cookie).get("data") or {}
        wbi = nav.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        rawk = img + sub
        if len(rawk) < 64:
            raise RuntimeError("拿不到 wbi key，接口可能又变了")
        self.mixin = "".join(rawk[i] for i in MIXIN_TAB)[:32]
        self._save_cache()

    # ---------- 签名 ----------
    def sign(self, params):
        p = dict(params)
        p["wts"] = int(time.time())
        qs = urllib.parse.urlencode(sorted(p.items()), quote_via=urllib.parse.quote)
        qs = "".join(ch for ch in qs if ch not in "!'()*")
        p["w_rid"] = hashlib.md5((qs + self.mixin).encode("utf-8")).hexdigest()
        return p

    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.time()

    def _wbi_get(self, path, params):
        self._throttle()
        u = "https://api.bilibili.com" + path + "?" + urllib.parse.urlencode(
            self.sign(params), quote_via=urllib.parse.quote)
        r = http_json(u, self.cookie)
        if r.get("code") in (-352, -412, -401):
            log("[!] 风控 %s，刷新 session 重试" % r["code"])
            self.refresh_session()
            self._throttle()
            u = "https://api.bilibili.com" + path + "?" + urllib.parse.urlencode(
                self.sign(params), quote_via=urllib.parse.quote)
            r = http_json(u, self.cookie)
        return r

    # ---------- 接口 ----------
    def search(self, keyword, order="totalrank", page=1, page_size=20):
        kw = re.sub(r"\s+", "", keyword)      # 带空格 B 站一律 0 条
        r = self._wbi_get("/x/web-interface/wbi/search/type", {
            "search_type": "video", "keyword": kw, "page": page, "page_size": page_size,
            "order": order, "platform": "pc", "web_location": 1430654,
            "from_source": "webtop_search"})
        if r.get("code") != 0:
            log("[!] 搜索 %s 失败: %s" % (kw, r.get("message")))
            return []
        return (r.get("data") or {}).get("result") or []

    def view(self, bvid):
        self._throttle()
        r = http_json("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid, self.cookie)
        return r.get("data") if r.get("code") == 0 else None

    def danmaku(self, cid):
        """返回 [(秒, 文本)]。XML 端点同时也是 deflate 流。"""
        self._throttle()
        try:
            data = http_bytes("https://comment.bilibili.com/%d.xml" % cid, self.cookie,
                              referer="https://www.bilibili.com/")
        except Exception as e:
            log("[!] 弹幕失败 %s: %s" % (cid, e))
            return []
        text = inflate(data).decode("utf-8", "replace")
        out = []
        for p, txt in re.findall(r'<d p="([^"]+)">([^<]*)</d>', text):
            try:
                t = float(p.split(",")[0])
            except Exception:
                continue
            txt = txt.strip()
            if txt:
                out.append((t, txt))
        return out

    def comments(self, aid, page_size=20):
        """按热度取一级评论。老接口还活着，比 wbi/reply/main 省事且同源。"""
        self._throttle()
        u = ("https://api.bilibili.com/x/v2/reply?type=1&oid=%d&sort=2&ps=%d&pn=1"
             % (aid, page_size))
        r = http_json(u, self.cookie)
        if r.get("code") != 0:
            return []
        out = []
        for rp in ((r.get("data") or {}).get("replies") or []):
            msg = re.sub(r"\s+", " ", (rp.get("content") or {}).get("message") or "").strip()
            if len(msg) < 4:
                continue
            out.append({"user": (rp.get("member") or {}).get("uname", ""),
                        "like": rp.get("like", 0), "reply": rp.get("rcount", 0),
                        "text": msg[:220]})
        return out

    def subtitle(self, aid, cid):
        """需要登录态（SESSDATA）；没登录 B 站不返回字幕轨道。"""
        self._throttle()
        r = http_json("https://api.bilibili.com/x/player/v2?aid=%d&cid=%d" % (aid, cid), self.cookie)
        subs = (((r.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not subs:
            return [], []
        pick = next((s for s in subs if s.get("lan") in ("ai-zh", "zh-CN", "zh-Hans")), subs[0])
        u = pick.get("subtitle_url") or ""
        if u.startswith("//"):
            u = "https:" + u
        body = []
        try:
            sj = http_json(u, self.cookie)
            body = [{"t": int(x.get("from", 0)), "text": (x.get("content") or "").strip()}
                    for x in (sj.get("body") or []) if (x.get("content") or "").strip()]
        except Exception as e:
            log("[!] 字幕内容失败: %s" % e)
        return [s.get("lan_doc") for s in subs], body


# ---------------- 解析与统计 ----------------
def clean_title(t):
    t = re.sub(r"</?em[^>]*>", "", t or "")
    for a, b in [("&quot;", '"'), ("&amp;", "&"), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")]:
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def parse_search_duration(s):
    """搜索接口的 duration 是 **MM:SS**（分钟位可超过 59，如 1752:1 = 29 小时）。
    它不包含分P数——分P只能从详情接口的 pages 里拿，别猜。返回总分钟数。"""
    parts = re.split(r"[:：]", str(s or "").strip())
    try:
        nums = [int(x) for x in parts if x.strip() != ""]
    except Exception:
        return 0
    if not nums:
        return 0
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return round(sec / 60)


def fmt_duration(sec):
    if not sec:
        return "?"
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return "%d 小时 %02d 分" % (h, m)
    return "%d 分钟" % max(m, 1)


def bigrams(s):
    s = re.sub(r"\s", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def relevance(title, topic, keywords):
    t = re.sub(r"\s", "", title.lower())
    core = bigrams(topic.lower())
    hit = len([g for g in core if g in t]) / max(len(core), 1)
    if re.sub(r"\s", "", topic.lower()) in t:
        hit = max(hit, 1.0)
    # 只奖励"主题是否被覆盖"，命中 1 个词和命中 3 个词同样相关，
    # 否则标题堆关键词的课会靠数量刷分
    kw_hit = 1 if any(re.sub(r"\s", "", k.lower()) in t for k in keywords) else 0
    return min(1.0, hit * 0.9 + kw_hit * 0.1)


def analyze_danmaku(items, duration_sec):
    """弹幕 -> 情绪画像。带时间戳的好处：能看出哪一段观众在崩。"""
    n = len(items)
    if not n:
        # 结构必须完整，调用方要按固定字段读；弹幕为 0 本身也是信号（新课没人验证）
        cats = {k: {"count": 0, "pct": 0.0, "samples": []} for k in LEXICON}
        cats["纯问号"] = {"count": 0, "pct": 0.0, "samples": []}
        return {"total": 0, "per_minute": 0.0, "cats": cats,
                "density_10pct": [0] * 10, "samples": []}
    cats = {}
    for cat, words in LEXICON.items():
        multi = [w for w in words if len(w) > 1]
        cnt, samples = 0, []
        for t, txt in items:
            if any(w in txt for w in multi):
                cnt += 1
                if len(samples) < 6:
                    samples.append(txt[:40])
        cats[cat] = {"count": cnt, "pct": round(cnt / n * 100, 1), "samples": samples}

    # 单字符问号单独统计（容易误伤，只用来估困惑强度）
    q = sum(1 for _, txt in items if txt.strip("?？ ") == "" and txt.strip())
    cats["纯问号"] = {"count": q, "pct": round(q / n * 100, 1), "samples": []}

    # 时间分桶：平均切成 10 段，看弹幕密度曲线
    dur = max(duration_sec, 1)
    buckets = [0] * 10
    for t, _ in items:
        buckets[min(9, int(t / dur * 10))] += 1

    # 均匀采样原文，让模型看到各阶段真实反应（按时间排序后等距抽取）
    ordered = sorted(items, key=lambda x: x[0])
    k = min(120, len(ordered))
    step = max(len(ordered) // max(k, 1), 1)
    picked = ordered[::step][:k]

    return {
        "total": n,
        "per_minute": round(n / max(dur / 60, 1), 2),
        "cats": cats,
        "density_10pct": buckets,
        "samples": [{"t": int(t), "text": txt[:60]} for t, txt in picked],
    }


def score_item(it, topic, keywords, mode="any"):
    """按场景加权，总分恒为 100。每个维度先算 0-1 的比例，再乘该模式下的权重。"""
    w = WEIGHTS.get(mode) or WEIGHTS["any"]
    bd = {}
    now_year = time.localtime().tm_year

    # 结构：分P 数。只对"找体系课"有强指向性，clip 模式直接不给权重。
    if not it.get("pages_known", True):
        # 分P数未知，但总时长是搜索接口给的可靠信息，用它代理"体量"。
        # 给中性分会把 11 分钟的单集和 41 小时的全集排成同分，排序就失效了。
        h = (it.get("duration_sec") or 0) / 3600.0
        if h >= 10:
            raw = 26
        elif h >= 4:
            raw = 20
        elif h >= 1.5:
            raw = 14
        elif h >= 0.5:
            raw = 8
        else:
            raw = 3
        bd["结构"] = round(w["结构"] * raw / 30.0)
    else:
        n = max(it.get("pages", 1), it.get("videos", 1))
        if n >= 30:
            raw = 30
        elif n >= 15:
            raw = 26
        elif n >= 8:
            raw = 20
        elif n >= 4:
            raw = 12
        else:
            raw = 4
        if it.get("is_season"):
            raw = min(30, raw + 5)
        bd["结构"] = round(w["结构"] * raw / 30.0)

    # 口碑：比率会被小样本放大（2 万播放也能刷出 7% 收藏率），
    # 用播放量做置信度加权，避免新课靠"分母小"虚高。
    play = max(it.get("play", 1) or 1, 1)
    fav_rate = (it.get("fav", 0) or 0) / play
    like_rate = (it.get("like", 0) or 0) / play
    if fav_rate >= 0.05:
        base = 25
    elif fav_rate >= 0.03:
        base = 21
    elif fav_rate >= 0.02:
        base = 16
    elif fav_rate >= 0.01:
        base = 10
    else:
        base = 4
    if like_rate >= 0.05:
        base = min(25, base + 3)
    conf = min(1.0, math.log10(max(play, 10)) / 6.0)   # 10 万播放≈0.83，100 万=1.0
    bd["口碑"] = round(w["口碑"] * base / 25.0 * conf)

    rel = relevance(it.get("title", ""), topic, keywords)
    # 标题用分隔符并列多个方向（"CTF逆向/CTFweb/CTFpwn/CTFmisc"）而主题只占其中一小段，
    # 说明这是大杂烩课，主题只是引流词。等 probe 拿到大纲后会再核一次。
    _title = it.get("title", "")
    segs = [x for x in re.split(r"[/|｜、+＋]", _title) if x.strip()]
    if len(segs) >= 3:
        tl = re.sub(r"\s", "", topic).lower()
        named = sum(1 for x in segs if tl in x.lower())
        if named < len(segs) * 0.6:
            rel *= 0.6
    bd["相关"] = round(w["相关"] * rel)

    # 时效：只有"技术/工具类"才怕过时，"怎么做红烧肉"十年前的做法今天照样对
    age = now_year - it.get("year", 1970)
    if age <= 2:
        raw = 10
    elif age <= 4:
        raw = 7
    elif age <= 6:
        raw = 4
    else:
        raw = 2
    if mode == "course" and any(h in topic for h in TIMELESS_HINTS):
        raw = max(raw, 7)
    bd["时效"] = round(w["时效"] * raw / 10.0)

    title = it.get("title", "")
    raw = 5
    raw += min(3, sum(1 for x in GOOD_WORDS if x in title))
    raw -= min(3, sum(1 for x in BAD_WORDS if x in title))
    seo = sum(1 for x in SEO_WORDS if x in title)
    if seo >= 3:            # 三个以上营销词 = 典型 SEO 堆砌标题，通常是搬运/营销号
        raw -= min(4, seo - 1)
    bd["标题"] = round(w["标题"] * max(0, min(10, raw)) / 10.0)

    return sum(bd.values()), bd


def build_items(raw_list, max_minutes, mode="any"):
    # 门槛按场景放开：找体系课时 8 分钟以下没意义，但泛搜时短视频才是主体，
    # 冷门方向的优质内容可能只有几百播放，不能用"热门"标准去筛。
    if mode == "course":
        min_dur, min_play = 8, 500
    elif mode == "clip":
        min_dur, min_play = 0, 200
    else:
        min_dur, min_play = 1, 100

    seen_bv, seen_title, out = set(), set(), []
    for v in raw_list:
        bv = v.get("bvid")
        if not bv or bv in seen_bv:
            continue
        title = clean_title(v.get("title"))
        norm = re.sub(r"[^\w\u4e00-\u9fff]", "", title)[:24]
        if norm in seen_title:
            continue
        mins = parse_search_duration(v.get("duration"))
        play = int(v.get("play") or 0)
        if 0 < mins < min_dur or play < min_play:
            continue
        if max_minutes and mins > max_minutes:
            continue
        seen_bv.add(bv)
        seen_title.add(norm)
        pub = int(v.get("pubdate") or 0)
        out.append({
            "bvid": bv, "title": title,
            "url": "https://www.bilibili.com/video/" + bv,
            "author": v.get("author") or "",
            "play": play, "like": int(v.get("like") or 0),
            "fav": int(v.get("favorites") or 0),
            "danmaku": int(v.get("video_review") or 0),
            "duration_sec": mins * 60, "duration_text": fmt_duration(mins * 60),
            "pubdate": time.strftime("%Y-%m-%d", time.localtime(pub)) if pub else "",
            "year": time.localtime(pub).tm_year if pub else 1970,
            # 搜索接口不返回分P数，先标 1 并打上 pages_known=False，
            # 粗筛给中性分，等详情接口拿到真实值再重算，不要在这里编。
            "pages": 1, "videos": 1, "pages_known": False,
            "is_season": False, "desc": "",
        })
    return out


def dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def make_keywords(topic, extra=None, mode="any"):
    """首轮只用原词 + 用户/模型给的精准词。
    泛搜时乱拼"入门/教程"会把用户本来很精确的查询意图带跑偏。"""
    base = re.sub(r"\s+", "", topic)
    return dedup([base] + [re.sub(r"\s+", "", e) for e in (extra or [])])


def expand_keywords(topic, mode="any"):
    """只有首轮召回不够时才启用后缀扩展。"""
    base = re.sub(r"\s+", "", topic)
    if any(x in base for x in ["入门", "零基础", "教程", "全集", "测评", "评测", "攻略", "怎么"]):
        extra = [base + "讲解", base + "推荐"]
    elif mode == "course":
        extra = [base + "入门", base + "教程", base + "零基础", base + "全集", base + "精讲"]
    elif mode == "clip":
        extra = [base + "技巧", base + "怎么做", base + "教程"]
    else:
        extra = [base + "教程", base + "讲解", base + "推荐"]
    return dedup(extra)


# ---------------- 深度体检 ----------------
def probe_one(sess, bvid, dm_parts=3, dm_sample=120, n_comments=20, with_sub=False):
    """单课体检：大纲 + 弹幕情绪 + 高赞评论 (+ 字幕)。"""
    v = sess.view(bvid)
    if not v:
        return {"bvid": bvid, "error": "view 失败（可能失效或风控）"}
    pages = v.get("pages") or []
    st = v.get("stat") or {}
    total_dur = sum(int(p.get("duration") or 0) for p in pages)

    # 1 结构层：分P标题就是大纲
    outline = [{"p": p.get("page"), "title": p.get("part"), "sec": int(p.get("duration") or 0)}
               for p in pages]

    # 2 内容层：抽样首/中/末几个分P抓弹幕，既看情绪也看完播衰减
    picks = []
    if pages:
        idx = sorted({0, len(pages) // 2, len(pages) - 1})[:dm_parts]
        picks = [pages[i] for i in idx]
    part_stats = []
    for p in picks:
        dms = sess.danmaku(p["cid"])
        stat = analyze_danmaku(dms, int(p.get("duration") or 0))
        stat["page"] = p.get("page")
        stat["part_title"] = p.get("part")
        stat["part_sec"] = int(p.get("duration") or 0)
        part_stats.append(stat)

    # 合并抽样分P的弹幕原文：同一句最多留 2 次（"请评论区留言"刷屏没有信息量），
    # 保留所属分P，便于定位是哪一段观众在反应
    merged, seen = [], {}
    for stat in part_stats:
        for s in stat.get("samples", []):
            key = s.get("text", "")[:20]
            if not key or seen.get(key, 0) >= 2:
                continue
            seen[key] = seen.get(key, 0) + 1
            merged.append({"p": stat.get("page"), "t": s.get("t"), "text": s.get("text")})
    merged = merged[:140]

    # 3 评论层
    comments = sess.comments(v.get("aid"), n_comments)

    # 4 字幕层（可选，需登录）
    sub_langs, sub_body = ([], [])
    if with_sub:
        sub_langs, sub_body = sess.subtitle(v.get("aid"), pages[0]["cid"] if pages else 0)

    play = max(st.get("view", 1) or 1, 1)
    dm_total, reply_total = st.get("danmaku") or 0, st.get("reply") or 0
    if dm_total >= 2000 and reply_total >= 50:
        conf = "充足"
    elif dm_total >= 300 or reply_total >= 20:
        conf = "一般"
    else:
        conf = "稀少"
    return {
        "bvid": bvid,
        "title": v.get("title"),
        "url": "https://www.bilibili.com/video/" + bvid,
        "author": (v.get("owner") or {}).get("name"),
        "audience": {"danmaku_total": dm_total, "reply_total": reply_total,
                     "verdict": conf,
                     "note": "" if conf == "充足" else "观众样本不足，弹幕/评论结论仅供参考"},
        "pubdate": time.strftime("%Y-%m-%d", time.localtime(v["pubdate"])) if v.get("pubdate") else "",
        "tname": v.get("tname"),
        "pages": len(pages),
        "is_season": bool(v.get("ugc_season")),
        "season_title": ((v.get("ugc_season") or {}).get("title")) or "",
        "total_hours": round(total_dur / 3600, 1),
        "stat": {"play": st.get("view"), "like": st.get("like"), "fav": st.get("favorite"),
                 "coin": st.get("coin"), "danmaku": st.get("danmaku"), "reply": st.get("reply"),
                 "fav_rate": round((st.get("favorite") or 0) / play, 4),
                 "like_rate": round((st.get("like") or 0) / play, 4),
                 "coin_rate": round((st.get("coin") or 0) / play, 4)},
        "desc": re.sub(r"\s+", " ", (v.get("desc") or ""))[:400],
        "outline": outline,
        "danmaku_by_part": part_stats,
        "danmaku_samples": merged,
        "comments": comments,
        "subtitle_langs": sub_langs,
        "subtitle_chars": sum(len(x["text"]) for x in sub_body),
        "subtitle_samples": [x["text"] for x in sub_body[:40]] if sub_body else [],
    }


# ---------------- 命令 ----------------
def load_cfg(args):
    cfg = {}
    if getattr(args, "config", None):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    topic = getattr(args, "topic", None) or cfg.get("topic") or ""
    if not topic:
        raise SystemExit("缺少 topic：用 --topic 或 --config")
    return cfg, topic


def harvest(sess, keywords, pages_per):
    pool = []
    for kw in keywords:
        for order in ("totalrank", "click"):
            for pg in range(1, pages_per + 1):
                got = sess.search(kw, order=order, page=pg)
                if not got:
                    break
                pool += got
        log("   %s -> 累计 %d" % (kw, len(pool)))
    return pool


def collect_pool(sess, topic, extra_kw, max_minutes, mode="any", pages_per=2, min_pool=25):
    keywords = make_keywords(topic, extra_kw, mode)
    log("[召回] 首轮 %d 组关键词 × 2 排序 × %d 页（模式 %s）" % (len(keywords), pages_per, mode))
    pool = harvest(sess, keywords, pages_per)
    items = build_items(pool, max_minutes, mode)
    log("[粗筛] 原始 %d -> 候选 %d" % (len(pool), len(items)))

    # 冷门方向首轮可能捞不到东西，这时才补后缀词，避免把精确查询带跑偏
    if len(items) < min_pool:
        more = expand_keywords(topic, mode)
        log("[扩展] 候选偏少（%d < %d），追加 %d 组后缀词" % (len(items), min_pool, len(more)))
        pool += harvest(sess, more, pages_per)
        keywords = dedup(keywords + more)
        items = build_items(pool, max_minutes, mode)
        log("[粗筛] 原始 %d -> 候选 %d" % (len(pool), len(items)))

    for it in items:
        it["score"], it["breakdown"] = score_item(it, topic, keywords, mode)
    items.sort(key=lambda x: x["score"], reverse=True)
    return keywords, items


def cmd_find(args):
    cfg, topic = load_cfg(args)
    mode = args.mode or cfg.get("mode") or "any"
    sess = Bili(delay=args.delay)
    keywords, items = collect_pool(sess, topic, cfg.get("extra_keywords") or args.extra_keyword,
                                   cfg.get("max_minutes") or args.max_minutes, mode=mode)
    top = args.top or cfg.get("top") or 15
    for it in items[:top]:
        it["fav_rate"] = round(it["fav"] / max(it["play"], 1), 4)
        it["like_rate"] = round(it["like"] / max(it["play"], 1), 4)
    res = {"topic": topic, "mode": mode, "keywords": keywords, "items": items[:top]}
    emit(res, args.out)


def diversify(items, n, per_author=2):
    """同一 UP 主最多占 per_author 个名额。
    不这么做，营销号批量搬运的同质课会把真正经典的那门挤到体检名单之外。"""
    out, cnt = [], {}
    for it in items:
        a = it.get("author") or ""
        if cnt.get(a, 0) >= per_author:
            continue
        cnt[a] = cnt.get(a, 0) + 1
        out.append(it)
        if len(out) >= n:
            break
    return out


def cmd_deep(args):
    """粗筛 Top N，再逐个体检。默认 N=8，够挑出 3-5 门课。"""
    cfg, topic = load_cfg(args)
    mode = args.mode or cfg.get("mode") or "any"
    sess = Bili(delay=args.delay)
    keywords, items = collect_pool(sess, topic, cfg.get("extra_keywords") or args.extra_keyword,
                                   cfg.get("max_minutes") or args.max_minutes, mode=mode)
    n = args.deep_n or cfg.get("deep_n") or 8
    shortlist = diversify(items, n, per_author=2 if mode == "course" else 3)
    log("[体检] 对 Top %d 逐个抓大纲/弹幕/评论" % len(shortlist))
    reports = []
    for i, it in enumerate(shortlist, 1):
        log("   (%d/%d) %s" % (i, len(shortlist), it["title"][:34]))
        rep = probe_one(sess, it["bvid"], n_comments=args.n_comments,
                        with_sub=bool(sess.sessdata))
        rep["pre_score"] = it["score"]
        # 详情接口才有真实分P数，用它重算结构分，粗筛的中性分作废
        real = {"pages": rep.get("pages", 1), "videos": rep.get("pages", 1),
                "is_season": rep.get("is_season", False), "pages_known": True,
                "play": rep["stat"]["play"], "like": rep["stat"]["like"],
                "fav": rep["stat"]["fav"], "title": rep.get("title", ""),
                "year": int(rep["pubdate"][:4]) if rep.get("pubdate") else 1970}
        rep["meta_score"], rep["meta_breakdown"] = score_item(real, topic, keywords, mode)
        # 标题可以堆关键词，100 个分P标题造不了假。这个比例用来识破"挂羊头"。
        terms = [re.sub(r"\s", "", x).lower() for x in ([topic] + list(keywords)) if x]
        ol = rep.get("outline") or []
        hits = sum(1 for o in ol if any(t in (o.get("title") or "").lower() for t in terms))
        rep["outline_topic_hits"] = hits
        rep["outline_topic_rate"] = round(hits / max(len(ol), 1), 3)
        # 标题用分隔符并列多个方向（"CTF逆向/CTFweb/CTFpwn/CTFmisc"）但大纲几乎不碰主题
        # = 挂羊头。这类课靠堆关键词拿到高相关分，必须按大纲打折。
        segs = [x for x in re.split(r"[/|｜、+＋]", rep.get("title") or "") if x.strip()]
        tl = re.sub(r"\s", "", topic).lower()
        if len(segs) >= 3 and rep["outline_topic_rate"] < 0.2:
            named = sum(1 for x in segs if tl in x.lower())
            if named < len(segs) * 0.6:
                rep["title_bait"] = True
                rep["meta_breakdown"]["相关"] = round(rep["meta_breakdown"]["相关"] * 0.4)
                rep["meta_score"] = sum(rep["meta_breakdown"].values())
        reports.append(rep)
    # 重算后的分数才是准的，按它重排，别让顺序和分数打架
    reports.sort(key=lambda r: r.get("meta_score", 0), reverse=True)
    emit({"topic": topic, "mode": mode, "keywords": keywords, "reports": reports}, args.out)


def cmd_probe(args):
    sess = Bili(delay=args.delay)
    out = []
    for bv in args.bvid:
        log("[体检] " + bv)
        out.append(probe_one(sess, bv, n_comments=args.n_comments,
                             with_sub=bool(sess.sessdata) or args.with_sub))
    emit(out[0] if len(out) == 1 else out, args.out)


def cmd_search(args):
    sess = Bili(delay=args.delay)
    rows = sess.search(args.keyword, order=args.order, page=args.page)
    res = [{"bvid": v.get("bvid"), "title": clean_title(v.get("title")), "author": v.get("author"),
            "play": v.get("play"), "duration": v.get("duration"), "pubdate": v.get("pubdate")}
           for v in rows]
    emit(res, args.out)


def cmd_detail(args):
    sess = Bili(delay=args.delay)
    rows = []
    for bv in args.bvid:
        d = sess.view(bv)
        if not d:
            rows.append({"bvid": bv, "error": "view failed"})
            continue
        stt = d.get("stat") or {}
        rows.append({"bvid": bv, "title": d.get("title"),
                     "owner": (d.get("owner") or {}).get("name"),
                     "videos": d.get("videos"), "is_season": bool(d.get("ugc_season")),
                     "view": stt.get("view"), "like": stt.get("like"),
                     "favorite": stt.get("favorite"), "duration": d.get("duration"),
                     "pubdate": d.get("pubdate")})
    emit(rows, args.out)


def emit(obj, out):
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(s)
        log("[ok] 写入 %s" % out)
    else:
        print(s)


def main():
    ap = argparse.ArgumentParser(description="B 站网课检索与体检")
    ap.add_argument("--delay", type=float, default=0.8, help="请求间隔秒，低于 0.5 易触发风控")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="召回+粗筛（元数据层），输出候选池")
    f.add_argument("--config")
    f.add_argument("--topic")
    f.add_argument("--mode", choices=["any", "course", "clip"], help="any 泛搜(默认) / course 体系课 / clip 短视频")
    f.add_argument("--top", type=int)
    f.add_argument("--max-minutes", type=int, default=0)
    f.add_argument("--extra-keyword", action="append")
    f.add_argument("-o", "--out")
    f.set_defaults(func=cmd_find)

    d = sub.add_parser("deep", help="粗筛后逐个体检（含弹幕/评论/大纲），最准但最慢")
    d.add_argument("--config")
    d.add_argument("--topic")
    d.add_argument("--mode", choices=["any", "course", "clip"], help="any 泛搜(默认) / course 体系课 / clip 短视频")
    d.add_argument("--deep-n", type=int, help="体检几个，默认 8")
    d.add_argument("--n-comments", type=int, default=20)
    d.add_argument("--max-minutes", type=int, default=0)
    d.add_argument("--extra-keyword", action="append")
    d.add_argument("-o", "--out")
    d.set_defaults(func=cmd_deep)

    p = sub.add_parser("probe", help="指定 bvid 体检")
    p.add_argument("--bvid", nargs="+", required=True)
    p.add_argument("--n-comments", type=int, default=20)
    p.add_argument("--with-sub", action="store_true", help="尝试抓字幕（通常需登录态）")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_probe)

    s = sub.add_parser("search", help="只调搜索接口")
    s.add_argument("--keyword", required=True)
    s.add_argument("--order", default="totalrank",
                   choices=["totalrank", "click", "pubdate", "dm", "stow"])
    s.add_argument("--page", type=int, default=1)
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_search)

    dt = sub.add_parser("detail", help="只调详情接口")
    dt.add_argument("--bvid", nargs="+", required=True)
    dt.add_argument("-o", "--out")
    dt.set_defaults(func=cmd_detail)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
