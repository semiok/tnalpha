"""搜狗公众号源——搜狗微信搜索抓取，只吃摘要（不打开文章）。免费、无需 key。

自包含：抓取逻辑内联（urllib + BeautifulSoup + 固定 UA），不依赖 OpenClaw / miku_ai。
抓取本身脆（搜狗反爬/改版），失败即返回空——上层跳过该源，不影响其他源与生成。
只取搜索结果页的 标题/摘要/公众号名/跳转链，**不逐篇打开文章**（读全文是更重的能力，见需求）。
"""
import urllib.request
from http.cookiejar import CookieJar
from urllib.parse import urlencode

from app.core import config
from app.core.sources.base import SourceAdapter

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SEARCH = "https://weixin.sogou.com/weixin"
_SEED = "https://v.sogou.com/v?ie=utf8&query=&p=40030600"


def _fetch(query: str, timeout: int) -> str:
    """带 cookie jar 抓搜狗微信搜索结果页（先 seed 拿 SNUID，再搜）。"""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", _UA)]
    try:
        opener.open(_SEED, timeout=timeout).read()   # 播 cookie，失败无妨
    except Exception:
        pass
    url = _SEARCH + "?" + urlencode({"query": query, "type": "2", "page": "1"})
    with opener.open(url, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


def _parse(html: str) -> list[dict]:
    from bs4 import BeautifulSoup   # 延迟导入：缺依赖时不拖垮注册表加载
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("li", id=lambda x: x and x.startswith("sogou_vr_11002601_box_"))
    out: list[dict] = []
    for it in items:
        h3 = it.find("h3")
        title = h3.get_text(strip=True) if h3 else ""
        if not title:
            continue
        p = it.find("p", class_="txt-info")
        summary = p.get_text(strip=True) if p else ""
        src = it.find("span", class_="all-time-y2")
        source = src.get_text(strip=True) if src else "公众号"
        a = it.find("a", target="_blank")
        href = a.get("href", "") if a else ""
        url = ("https://weixin.sogou.com" + href) if href.startswith("/") else href
        out.append({"title": title, "summary": summary, "url": url, "source": source})
    return out


class SogouAdapter(SourceAdapter):
    name = "mp"
    label = "搜狗公众号"
    emoji = "📰"
    paid = False
    default_on = False

    def is_available(self) -> bool:
        return True   # 无需 key；抓取失败在 gather 里被吞成空

    def search(self, query: str) -> list[dict]:
        return _parse(_fetch(query, config.SOURCE_TIMEOUT))
