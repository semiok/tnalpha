"""②选题库：parse 解析 / generate 两模式（活动·品牌常青）/ 路由（生成·采纳·删除·权限）。

LLM 走 stub（conftest 强制），故 generate 用 monkeypatch 把 llm.generate_text 换成
可解析的纯文本样例——测的是"读 KnowledgeContext→组 prompt→parse→落库"这条链，非真模型输出。
"""
import pytest
from urllib.parse import unquote
from sqlmodel import Session, select

from app.modules.knowledge.models import Brand, Campaign
from app.core.llm.errors import ModelRateLimited
from app.modules.topic import generate as gen
from app.modules.topic import routes as troutes
from app.modules.topic.generate import parse_candidates
from app.modules.topic.models import Topic

# 两个选题的规范纯文本（含多行纲要，验证「抓到下一标签为止」）
_SAMPLE = """标题：一枚汉简写了什么
纲要：从悬泉置里程简切入，讲古人如何计量丝路里程。
可延伸到驿站制度。
受众：城市青年
时效：中
素材：汉代悬泉里程简 19×2×0.2cm
配图：文物暗场特写
时机：暑期研学季

标题：抓娃娃机里的敦煌
纲要：沈少民装置的反消费解读。
受众：艺术爱好者
时效：强"""


def _seed_brand(session: Session, with_campaign: bool = False) -> tuple[int, int | None]:
    b = Brand(name="敦煌", brand_prompt="调性", content_notes="规范", doc_digest="全景")
    session.add(b); session.commit(); session.refresh(b)
    cid = None
    if with_campaign:
        c = Campaign(brand_id=b.id, name="丝路有多长", campaign_digest="③选题方向：里程简故事")
        session.add(c); session.commit(); session.refresh(c)
        cid = c.id
    return b.id, cid


# ---------- parse ----------

def test_parse_candidates_two_blocks():
    cands = parse_candidates(_SAMPLE)
    assert len(cands) == 2
    a = cands[0]
    assert a.title == "一枚汉简写了什么"
    assert "里程简" in a.outline and "驿站制度" in a.outline    # 多行纲要抓全
    assert a.audience == "城市青年" and a.timeliness == "中"
    assert "19×2" in a.materials and a.image_hint == "文物暗场特写"
    assert a.publish_window == "暑期研学季"
    assert cands[1].title == "抓娃娃机里的敦煌" and cands[1].timeliness == "强"


def test_parse_candidates_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_candidates("[stub:topic_gen] 一段没有标题结构的文本")


# ---------- generate：两模式 ----------

def test_generate_topics_campaign_mode(fresh_db, monkeypatch):
    seen = {}

    def fake(prompt, task="default", module="default", **k):
        seen["prompt"] = prompt
        return _SAMPLE

    monkeypatch.setattr(gen.llm, "generate_text", fake)
    with Session(fresh_db) as s:
        bid, cid = _seed_brand(s, with_campaign=True)
        created = gen.generate_topics(s, bid, cid, count=5)
    assert len(created) == 2
    assert all(t.campaign_id == cid for t in created)
    assert created[0].source == "generated"               # 首轮=generated
    assert "③选题方向" in seen["prompt"]                    # 活动简报进了 prompt


def test_generate_topics_brand_evergreen_mode(fresh_db, monkeypatch):
    seen = {}

    def fake(prompt, task="default", module="default", **k):
        seen["prompt"] = prompt
        return _SAMPLE

    monkeypatch.setattr(gen.llm, "generate_text", fake)
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s, with_campaign=False)
        created = gen.generate_topics(s, bid, None, count=5)
    assert len(created) == 2 and all(t.campaign_id is None for t in created)
    assert "③选题方向" not in seen["prompt"]                # 品牌常青不含活动简报


def test_generate_topics_count_and_source(fresh_db, monkeypatch):
    monkeypatch.setattr(gen.llm, "generate_text", lambda *a, **k: _SAMPLE)
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        first = gen.generate_topics(s, bid, None, count=1)     # count 截断
        assert len(first) == 1
        second = gen.generate_topics(s, bid, None, count=5)    # 已有→source=added
        assert second[0].source == "added"


# ---------- 路由 ----------

def _stub_generate(monkeypatch):
    """把路由用到的 generate_topics 换成直接落一条，绕开 LLM。"""
    def fake_gen(session, brand_id, campaign_id=None, count=5, sources_used=None, hot_query="",
                 use_rejection_experience=False, use_publish_experience=False):
        t = Topic(brand_id=brand_id, campaign_id=campaign_id, title="测试选题", outline="纲要")
        session.add(t); session.commit(); session.refresh(t)
        return [t]
    monkeypatch.setattr(troutes, "generate_topics", fake_gen)


def test_topics_home_empty(owner_client):
    r = owner_client.get("/topics")
    assert r.status_code == 200 and "②选题库" in r.text


def test_generate_route_creates_and_lists(owner_client, fresh_db, monkeypatch):
    with Session(fresh_db) as s:
        _seed_brand(s)
    _stub_generate(monkeypatch)
    r = owner_client.post("/topics/generate", data={"campaign_id": "", "count": "3"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert "测试选题" in owner_client.get("/topics").text


def test_adopt_and_delete_moves_to_recycle_bin(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        t = Topic(brand_id=bid, title="待采纳")
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id
    r = owner_client.post(f"/topics/{tid}/adopt", follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        assert s.get(Topic, tid).status == "采纳"
    owner_client.post(f"/topics/{tid}/delete", data={"rejection_reason": "角度太泛"},
                      follow_redirects=False)
    with Session(fresh_db) as s:
        t = s.get(Topic, tid)
        assert t is not None
        assert t.status == "回收站"
        assert t.rejection_reason == "角度太泛"
        assert t.rejected_at is not None


def test_delete_requires_rejection_reason(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        t = Topic(brand_id=bid, title="缺少原因")
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id
    r = owner_client.post(f"/topics/{tid}/delete", data={"rejection_reason": "   "},
                          follow_redirects=False)
    assert r.status_code == 400
    with Session(fresh_db) as s:
        assert s.get(Topic, tid).status == "候选"


def test_unadopt_reverts_to_candidate(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        t = Topic(brand_id=bid, title="采纳后又反悔", status="采纳")
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id
    r = owner_client.post(f"/topics/{tid}/unadopt", follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        assert s.get(Topic, tid).status == "候选"
    # 非采纳态不受影响
    owner_client.post(f"/topics/{tid}/unadopt", follow_redirects=False)
    with Session(fresh_db) as s:
        assert s.get(Topic, tid).status == "候选"


def test_generate_requires_editor_level(publisher_client, fresh_db):
    with Session(fresh_db) as s:
        _seed_brand(s)
    r = publisher_client.post("/topics/generate", data={"campaign_id": "", "count": "3"},
                              follow_redirects=False)
    assert r.status_code == 403        # publisher(level 0) < 选题者(level 1)


# ---------- 联网搜索注入 ----------

def test_generate_injects_hot_hits_into_prompt(fresh_db, monkeypatch):
    seen = {}

    def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return _SAMPLE

    monkeypatch.setattr(gen.llm, "generate_text", fake_llm)
    monkeypatch.setattr(gen.sources, "gather", lambda names, q, **k: [
        {"title": "敦煌大展开幕", "summary": "九座洞窟1:1复刻", "url": "", "source": "google"}])
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        gen.generate_topics(s, bid, None, count=2, sources_used=["google"], hot_query="敦煌")
    assert "实时热点参考" in seen["prompt"] and "敦煌大展开幕" in seen["prompt"]


def test_generate_no_sources_skips_network(fresh_db, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(gen.llm, "generate_text", lambda p, **k: _SAMPLE)

    def spy(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(gen.sources, "gather", spy)
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        gen.generate_topics(s, bid, None, count=2)          # 没勾源
    assert called["n"] == 0                                  # → 不联网


def test_generate_default_query_uses_brand_name(fresh_db, monkeypatch):
    seen = {}
    monkeypatch.setattr(gen.llm, "generate_text", lambda p, **k: _SAMPLE)

    def spy(names, q, **k):
        seen["q"] = q
        return []

    monkeypatch.setattr(gen.sources, "gather", spy)
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        gen.generate_topics(s, bid, None, count=2, sources_used=["google"], hot_query="")
    assert seen["q"] == "敦煌"                               # 空关键词→品牌名兜底


def test_generate_route_passes_and_filters_sources(owner_client, fresh_db, monkeypatch):
    with Session(fresh_db) as s:
        _seed_brand(s)
    captured = {}

    def fake_gen(session, brand_id, campaign_id=None, count=5, sources_used=None, hot_query="",
                 use_rejection_experience=False, **_kwargs):
        captured.update(sources_used=sources_used, hot_query=hot_query,
                        use_rejection_experience=use_rejection_experience)
        return []

    monkeypatch.setattr(troutes, "generate_topics", fake_gen)
    owner_client.post("/topics/generate", follow_redirects=False, data={
        "campaign_id": "", "count": "3", "source": ["google", "bogus"], "hot_query": "敦煌"})
    assert captured["sources_used"] == ["google"]           # 非法源 bogus 被过滤
    assert captured["hot_query"] == "敦煌"
    assert captured["use_rejection_experience"] is True


def test_manual_topics_keep_input_titles(owner_client, fresh_db, monkeypatch):
    def fake(prompt, task="default", module="default", **k):
        assert "用户已经手动指定" in prompt
        return """标题：模型想改掉的题目
纲要：模型补的纲要
受众：亲子
时效：中
素材：习字简
配图：简牍
时机：周末"""

    monkeypatch.setattr(gen.llm, "generate_text", fake)
    with Session(fresh_db) as s:
        bid, cid = _seed_brand(s, with_campaign=True)
    r = owner_client.post(
        "/topics/manual",
        data={"campaign_id": str(cid), "title": ["在边塞练字的人：一枚习字简", "", "300尊佛，翻模300次"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(fresh_db) as s:
        rows = s.exec(select(Topic).where(Topic.brand_id == bid).order_by(Topic.id)).all()
        assert [t.title for t in rows] == ["在边塞练字的人：一枚习字简", "300尊佛，翻模300次"]
        assert all(t.source == "manual" for t in rows)
        assert all(t.campaign_id == cid for t in rows)
        assert rows[0].outline == "模型补的纲要"


def test_generate_route_shows_rate_limit_modal(owner_client, fresh_db, monkeypatch):
    with Session(fresh_db) as s:
        _seed_brand(s)

    def fake_gen(*a, **k):
        raise ModelRateLimited("当前模型已限流")

    monkeypatch.setattr(troutes, "generate_topics", fake_gen)
    r = owner_client.post("/topics/generate", follow_redirects=False, data={
        "campaign_id": "", "count": "1"})
    assert r.status_code == 303
    assert "modal_error=" in r.headers["location"]
    assert "当前模型已限流" in unquote(r.headers["location"])


def test_generate_can_reference_recycle_bin_experience(fresh_db, monkeypatch):
    seen = {}

    def fake(prompt, task="default", module="default", **k):
        seen["prompt"] = prompt
        return _SAMPLE

    monkeypatch.setattr(gen.llm, "generate_text", fake)
    with Session(fresh_db) as s:
        bid, cid = _seed_brand(s, with_campaign=True)
        s.add(Topic(brand_id=bid, campaign_id=cid, title="过宽泛的展览打卡",
                    status="回收站", rejection_reason="只像普通打卡，缺少丝路知识点"))
        s.add(Topic(brand_id=bid, campaign_id=None, title="别的范围",
                    status="回收站", rejection_reason="品牌常青的经验不混入活动"))
        s.commit()
        gen.generate_topics(s, bid, cid, count=2, use_rejection_experience=True)
    assert "Campaign 总体经验包" in seen["prompt"]
    assert "过宽泛的展览打卡" in seen["prompt"]
    assert "只像普通打卡" in seen["prompt"]
    assert "品牌常青的经验不混入活动" not in seen["prompt"]


# ---------- 分类 tab ----------

def test_topics_tab_filter(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        s.add(Topic(brand_id=bid, title="候选选题X", status="候选"))
        s.add(Topic(brand_id=bid, title="采纳选题Y", status="采纳"))
        s.add(Topic(brand_id=bid, title="发布选题Z", status="已发布"))
        s.add(Topic(brand_id=bid, title="回收选题R", status="回收站", rejection_reason="不够具体"))
        s.commit()
    assert "候选选题X" in owner_client.get("/topics").text           # 全部含候选
    assert "回收选题R" not in owner_client.get("/topics").text        # 全部不含回收站
    adopted = owner_client.get("/topics?status=采纳").text
    assert "采纳选题Y" in adopted and "候选选题X" not in adopted      # 已采纳 tab 只留采纳
    published = owner_client.get("/topics?status=已发布").text
    assert "发布选题Z" in published and "采纳选题Y" not in published
    recycle = owner_client.get("/topics?status=回收站").text
    assert "回收选题R" in recycle and "不采纳原因：不够具体" in recycle


def test_topics_scope_filter_by_brand_and_campaign(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, cid = _seed_brand(s, with_campaign=True)
        c2 = Campaign(brand_id=bid, name="莫高精神")
        s.add(c2); s.commit(); s.refresh(c2)
        s.add(Topic(brand_id=bid, campaign_id=None, title="品牌常青选题", status="候选"))
        s.add(Topic(brand_id=bid, campaign_id=cid, title="丝路活动选题", status="候选"))
        s.add(Topic(brand_id=bid, campaign_id=c2.id, title="莫高活动选题", status="采纳"))
        s.commit()

    html = owner_client.get("/topics").text
    assert "全部范围" in html
    assert "品牌常青" in html
    assert "活动·丝路有多长" in html
    assert "活动·莫高精神" in html

    brand = owner_client.get("/topics?scope=brand").text
    assert "品牌常青选题" in brand
    assert "丝路活动选题" not in brand
    assert "莫高活动选题" not in brand

    campaign = owner_client.get(f"/topics?scope=campaign:{cid}").text
    assert "丝路活动选题" in campaign
    assert "品牌常青选题" not in campaign
    assert "莫高活动选题" not in campaign

    scoped_status = owner_client.get(f"/topics?status=采纳&scope=campaign:{cid}").text
    assert "丝路活动选题" not in scoped_status
    assert "莫高活动选题" not in scoped_status


def test_topics_show_id_and_gen_time(owner_client, fresh_db):
    with Session(fresh_db) as s:
        bid, _ = _seed_brand(s)
        t = Topic(brand_id=bid, title="带编号的选题")
        s.add(t); s.commit(); s.refresh(t)
        tid = t.id
    html = owner_client.get("/topics").text
    assert f"#{tid}" in html          # 编号
    assert "生成于" in html            # 生成时间（到小时）


def test_topics_catalog_checkboxes_render(owner_client, fresh_db):
    with Session(fresh_db) as s:
        _seed_brand(s)
    html = owner_client.get("/topics").text
    assert "Google 搜索" in html and "搜狗公众号" in html
    assert "weixin.sogou.com" in html
    assert "🔥 深度热点" not in html and "小红书" in html
    assert "手动上传选题" in html and "确认上传" in html
    assert "不采纳原因默认作为选题经验参考" in html
    assert "参考选题经验包" not in html and "参考发布经验包" not in html
    assert 'name="use_rejection_experience"' not in html
