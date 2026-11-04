#!/usr/bin/env python3
"""视频热度抓取小工具：抓取 B站 / YouTube 视频的播放、点赞、评论等公开数据。

用法：
    python video_stats.py URL [URL ...]
    python video_stats.py -f urls.txt                  # 每行一个链接，# 开头为注释
    python video_stats.py -f urls.txt -o stats.csv     # 追加写入 CSV（带时间戳，方便看趋势）
    python video_stats.py -f urls.txt --every 60       # 每 60 分钟抓一次，Ctrl+C 停止

YouTube 需要 API Key：设置环境变量 YOUTUBE_API_KEY（Google Cloud 控制台免费申请
YouTube Data API v3）。B站使用公开的视频信息接口，无需登录。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
FIELDS = ["time", "platform", "id", "title", "author", "views", "likes",
          "comments", "favorites", "coins", "shares", "danmaku", "url", "error"]

session = requests.Session()
session.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})


# ---------- 链接识别 ----------
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"(?:^|/|av)(\d+)(?:[/?#]|$)", re.I)
YT_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def detect(url: str) -> tuple[str, str] | None:
    """返回 (平台, 视频ID)，识别不了返回 None。"""
    url = url.strip()
    if "b23.tv" in url:  # B站短链，先跟随跳转
        try:
            url = session.head(url, allow_redirects=True, timeout=10).url
        except requests.RequestException:
            return None
    if "bilibili.com" in url or url.startswith("BV") or url.lower().startswith("av"):
        if m := BV_RE.search(url):
            return "bilibili", m.group(1)
        if m := AV_RE.search(url):
            return "bilibili", "av" + m.group(1)
    if "youtu" in url:
        if m := YT_RE.search(url):
            return "youtube", m.group(1)
    if re.fullmatch(r"[\w-]{11}", url):  # 裸 YouTube ID
        return "youtube", url
    return None


# ---------- B站 ----------
def fetch_bilibili(vid: str) -> dict:
    params = {"aid": vid[2:]} if vid.startswith("av") else {"bvid": vid}
    r = session.get("https://api.bilibili.com/x/web-interface/view",
                    params=params, timeout=10)
    r.raise_for_status()
    return parse_bilibili(r.json(), vid)


def parse_bilibili(js: dict, vid: str) -> dict:
    if js.get("code") != 0:
        # -404 不存在；-403 无权限；-352 / -412 触发风控，降低频率或稍后再试
        raise RuntimeError(f"B站返回 code={js.get('code')} {js.get('message')}")
    d, s = js["data"], js["data"]["stat"]
    return {
        "platform": "bilibili", "id": d.get("bvid", vid), "title": d.get("title"),
        "author": d.get("owner", {}).get("name"), "views": s.get("view"),
        "likes": s.get("like"), "comments": s.get("reply"),
        "favorites": s.get("favorite"), "coins": s.get("coin"),
        "shares": s.get("share"), "danmaku": s.get("danmaku"),
        "url": f"https://www.bilibili.com/video/{d.get('bvid', vid)}",
    }


# ---------- YouTube ----------
def fetch_youtube(ids: list[str]) -> dict[str, dict]:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("未设置环境变量 YOUTUBE_API_KEY")
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):  # 接口一次最多 50 个
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
            "part": "snippet,statistics", "id": ",".join(ids[i:i + 50]), "key": key,
        }, timeout=10)
        r.raise_for_status()
        out.update(parse_youtube(r.json()))
    return out


def parse_youtube(js: dict) -> dict[str, dict]:
    def num(v):
        return int(v) if v is not None else None
    out = {}
    for it in js.get("items", []):
        s, sn = it.get("statistics", {}), it.get("snippet", {})
        out[it["id"]] = {
            "platform": "youtube", "id": it["id"], "title": sn.get("title"),
            "author": sn.get("channelTitle"), "views": num(s.get("viewCount")),
            "likes": num(s.get("likeCount")), "comments": num(s.get("commentCount")),
            "favorites": None, "coins": None, "shares": None, "danmaku": None,
            "url": f"https://www.youtube.com/watch?v={it['id']}",
        }
    return out


# ---------- 主流程 ----------
def collect(urls: list[str], delay: float) -> list[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    rows, yt_ids = [], []
    for u in urls:
        hit = detect(u)
        if not hit:
            rows.append({"platform": "?", "id": "", "url": u,
                         "error": "无法识别（目前只支持 B站 / YouTube）"})
            continue
        plat, vid = hit
        if plat == "youtube":
            yt_ids.append(vid)
            continue
        try:
            rows.append(fetch_bilibili(vid))
        except Exception as e:  # noqa: BLE001
            rows.append({"platform": plat, "id": vid, "url": u, "error": str(e)})
        time.sleep(delay)  # 别请求太快，避免触发风控
    if yt_ids:
        try:
            got = fetch_youtube(yt_ids)
            for vid in yt_ids:
                rows.append(got.get(vid) or {"platform": "youtube", "id": vid,
                                             "error": "视频不存在或不公开"})
        except Exception as e:  # noqa: BLE001
            rows += [{"platform": "youtube", "id": v, "error": str(e)} for v in yt_ids]
    for r in rows:
        r["time"] = now
    return rows


def fmt(n) -> str:
    if n is None:
        return "-"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def show(rows: list[dict]) -> None:
    print(f"\n[{rows[0]['time'] if rows else ''}]")
    print(f"{'平台':<9}{'播放':>9}{'点赞':>9}{'评论':>8}  标题")
    for r in rows:
        if r.get("error"):
            print(f"{r['platform']:<9}{'失败':>9}  {r.get('id')}  {r['error']}")
            continue
        title = (r.get("title") or "")[:40]
        print(f"{r['platform']:<9}{fmt(r['views']):>9}{fmt(r['likes']):>9}"
              f"{fmt(r['comments']):>8}  {title}")


def save(rows: list[dict], path: Path) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel 打开不乱码
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 B站 / YouTube 视频热度数据")
    ap.add_argument("urls", nargs="*", help="视频链接或 ID")
    ap.add_argument("-f", "--file", type=Path, help="链接列表文件，每行一个")
    ap.add_argument("-o", "--out", type=Path, help="追加写入的 CSV 文件")
    ap.add_argument("--every", type=float, help="每隔多少分钟重复抓取一次")
    ap.add_argument("--delay", type=float, default=1.5, help="B站请求间隔秒数（默认 1.5）")
    a = ap.parse_args()

    urls = list(a.urls)
    if a.file:
        urls += [l.strip() for l in a.file.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
    if not urls:
        ap.error("请提供视频链接，或用 -f 指定链接文件")

    while True:
        rows = collect(urls, a.delay)
        show(rows)
        if a.out:
            save(rows, a.out)
            print(f"已追加 {len(rows)} 行到 {a.out}")
        if not a.every:
            return 0
        try:
            time.sleep(a.every * 60)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
