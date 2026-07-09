"""①知识库 端到端测试：品牌/campaign/文档/AI解析/数据池 + 权限守卫。"""


def _create_brand(client, name="敦煌IP") -> int:
    resp = client.post("/brands", data={"name": name}, follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


def _create_campaign(client, brand_id, name="美术展") -> int:
    resp = client.post("/campaigns", data={"brand_id": brand_id, "name": name},
                       follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


# ── 品牌 + 默认 campaign ──

def test_create_brand_no_default_campaign(owner_client):
    _create_brand(owner_client)
    # 品牌日常已去除：不再自动建默认 campaign（品牌库已承载品牌内容）
    assert "品牌日常" not in owner_client.get("/").text


def test_home_shows_default_brand_and_entries(owner_client):
    # 空库首访自动建默认品牌「敦煌当代美术馆」+ 两个管理入口，无「新建品牌」
    home = owner_client.get("/").text
    assert "敦煌当代美术馆" in home
    assert "品牌库管理" in home and "数据池管理" in home
    assert "新建品牌" not in home


# ── campaign ──

def test_create_campaign(owner_client):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    page = owner_client.get(f"/campaigns/{cid}")
    assert page.status_code == 200
    assert "美术展" in page.text


def test_delete_campaign(owner_client):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    resp = owner_client.post(f"/campaigns/{cid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert owner_client.get(f"/campaigns/{cid}").status_code == 404


# ── 文档上传（走 core/storage）──

def test_upload_brand_doc(owner_client):
    brand_id = _create_brand(owner_client)
    resp = owner_client.post(f"/brands/{brand_id}/docs",
                             files={"file": ("brief.txt", b"hello", "text/plain")},
                             follow_redirects=False)
    assert resp.status_code == 303
    assert "brief.txt" in owner_client.get(f"/brands/{brand_id}").text


def test_upload_campaign_doc(owner_client):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    resp = owner_client.post(f"/campaigns/{cid}/docs",
                             data={"note": "开幕"},
                             files={"file": ("plan.txt", b"x", "text/plain")},
                             follow_redirects=False)
    assert resp.status_code == 303
    page = owner_client.get(f"/campaigns/{cid}").text
    assert "plan.txt" in page and "开幕" in page


def test_upload_forms_show_loading_state(owner_client):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    brand_page = owner_client.get(f"/brands/{brand_id}").text
    campaign_page = owner_client.get(f"/campaigns/{cid}").text
    pool_page = owner_client.get("/pool").text
    for html in (brand_page, campaign_page, pool_page):
        assert 'x-data="{ uploading:false }"' in html
        assert '@submit="uploading=true"' in html
        assert "上传中" in html


# ── AI 解析：后台 analyze 路由 + run_analysis 逻辑（照 tngen）──

def test_analyze_route_sets_running(owner_client, fresh_db, monkeypatch):
    from sqlmodel import Session
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import Brand
    monkeypatch.setattr(analysis, "start_background_analysis", lambda bid: None)  # 不起线程
    brand_id = _create_brand(owner_client)
    r = owner_client.post(f"/brands/{brand_id}/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/brands/{brand_id}"
    with Session(fresh_db) as s:
        assert s.get(Brand, brand_id).analysis_status == "running"


def test_parse_campaign_sets_running(owner_client, fresh_db, monkeypatch):
    """活动 AI 解析改异步（同 brand）：路由置 running + 起后台，303 回活动页。"""
    from sqlmodel import Session
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import Campaign
    monkeypatch.setattr(analysis, "start_campaign_analysis", lambda cid: None)  # 不起线程
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    r = owner_client.post(f"/campaigns/{cid}/parse", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/campaigns/{cid}"
    with Session(fresh_db) as s:
        assert s.get(Campaign, cid).analysis_status == "running"


def test_run_campaign_analysis_stores_digest(owner_client, fresh_db):
    """直接同步跑 run_campaign_analysis（避开后台线程）：资料 → campaign_digest 入库。"""
    from sqlmodel import Session
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import Campaign
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    owner_client.post(f"/campaigns/{cid}/docs", data={"note": "开幕"},
                      files={"file": ("plan.txt", "展会主题：丝路".encode(), "text/plain")})
    with Session(fresh_db) as s:
        analysis.run_campaign_analysis(cid, s)
        assert "stub:campaign_digest" in s.get(Campaign, cid).campaign_digest


def test_campaign_doc_deep_read_toggle(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import CampaignDoc
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    owner_client.post(f"/campaigns/{cid}/docs", data={"note": ""},
                      files={"file": ("p.txt", b"x", "text/plain")})
    with Session(fresh_db) as s:
        doc = s.exec(select(CampaignDoc).where(CampaignDoc.campaign_id == cid)).first()
        assert doc.deep_read is False
    owner_client.post(f"/campaigns/{cid}/docs/{doc.id}/deep-read", follow_redirects=False)
    with Session(fresh_db) as s:
        assert s.get(CampaignDoc, doc.id).deep_read is True


def test_pool_file_upload_stores_file(owner_client, fresh_db):
    """数据池上传文件：存 file_path；手填正文优先保留（真实 pdf/docx 抽取由 docparse 负责）。"""
    from sqlmodel import Session, select
    from app.modules.knowledge.models import PoolTopic
    resp = owner_client.post("/pool", data={"title": "展会资料", "kind": "资料包", "content": "手填摘要"},
                             files={"file": ("brief.txt", b"x", "text/plain")},
                             follow_redirects=False)
    assert resp.status_code == 303
    with Session(fresh_db) as s:
        t = s.exec(select(PoolTopic).where(PoolTopic.title == "展会资料")).first()
        assert t.file_path and t.content == "手填摘要"              # 存了文件 + 手填正文保留


def test_pool_upload_extracts_supported_format(owner_client, fresh_db, monkeypatch):
    """支持格式（如 pdf）上传：无手填正文时，抽取的正文入 content。"""
    from sqlmodel import Session, select
    from app.modules.knowledge import routes as kroutes
    from app.modules.knowledge.models import PoolTopic
    monkeypatch.setattr(kroutes.docparse, "extract_text", lambda p: "抽出的展会正文")
    resp = owner_client.post("/pool", data={"title": "PDF资料", "kind": "资料包"},
                             files={"file": ("brief.pdf", b"%PDF", "application/pdf")},
                             follow_redirects=False)
    assert resp.status_code == 303
    with Session(fresh_db) as s:
        t = s.exec(select(PoolTopic).where(PoolTopic.title == "PDF资料")).first()
        assert t.content == "抽出的展会正文"                        # 无手填 → 用抽取正文


def test_pool_file_download(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import PoolTopic
    owner_client.post("/pool", data={"title": "下载测试", "kind": "资料包"},
                      files={"file": ("d.txt", b"hello-pool", "text/plain")})
    with Session(fresh_db) as s:
        t = s.exec(select(PoolTopic).where(PoolTopic.title == "下载测试")).first()
    r = owner_client.get(f"/pool/{t.id}/download")
    assert r.status_code == 200 and b"hello-pool" in r.content


def test_pool_material_delete_removes_topic_and_refs(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import CampaignPoolRef, PoolTopic
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    tid = _create_pool_topic(owner_client, fresh_db, "可删资料")
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": tid})
    r = owner_client.post(f"/pool/{tid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pool"
    with Session(fresh_db) as s:
        assert s.get(PoolTopic, tid) is None
        assert not s.exec(select(CampaignPoolRef).where(CampaignPoolRef.pool_topic_id == tid)).all()


# ── 数据池 增查 ──

def test_pool_add_and_list(owner_client):
    resp = owner_client.post("/pool",
                             data={"title": "竹简资料", "kind": "资料包",
                                   "brand_tag": "敦煌", "content": "竹简图文资料"},
                             follow_redirects=False)
    assert resp.status_code == 303
    page = owner_client.get("/pool").text
    assert "资料包" in page
    assert "竹简资料" in page


# ── 权限：editor/publisher 越权写 → 403 ──

def test_editor_cannot_create_brand(editor_client):
    assert editor_client.post("/brands", data={"name": "x"}).status_code == 403


def test_publisher_cannot_create_brand(publisher_client):
    assert publisher_client.post("/brands", data={"name": "x"}).status_code == 403


def test_editor_cannot_add_pool(editor_client):
    assert editor_client.post("/pool", data={"title": "x"}).status_code == 403


def test_editor_cannot_upload_doc(owner_client, editor_client):
    brand_id = _create_brand(owner_client)
    resp = editor_client.post(f"/brands/{brand_id}/docs",
                              files={"file": ("a.txt", b"x", "text/plain")})
    assert resp.status_code == 403


def test_editor_can_browse(owner_client, editor_client):
    _create_brand(owner_client, "可浏览")
    assert editor_client.get("/").status_code == 200
    assert "可浏览" in editor_client.get("/").text


def test_anon_redirected_to_login(anon_client):
    resp = anon_client.post("/brands", data={"name": "x"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login")


# ── 文档真实解析（复用 tngen 逻辑）──

def _docx_bytes(text: str) -> bytes:
    import io
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ── 只读演示模式（KNOWLEDGE_WRITABLE=false）：整个原型全貌当演示壳 ──

def test_readonly_renders_prototype_shell(owner_client):
    from app.core import runtime
    runtime.set_knowledge_writable(False)               # 切演示模式
    home = owner_client.get("/").text
    for tab in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈"):
        assert tab in home
    assert "⑥权限" not in home and "⑦提示词展示" not in home
    assert "敦煌IP" in home and "敦煌当代美术展" in home   # ①知识库那屏内容
    assert "模型配置" not in home and "退出登录" in home
    assert 'action="/brands"' not in home               # 不是动态首页
    from app import __version__
    assert f"v{__version__}" in home                     # 版本号已注入演示壳（占位符替换）
    assert "__APP_VERSION__" not in home                 # 占位符不残留


def test_readonly_detail_routes_redirect_home(owner_client):
    from app.core import runtime
    brand_id = _create_brand(owner_client)             # 后端 CRUD 代码保留，仍可建
    runtime.set_knowledge_writable(False)              # 切演示模式
    r = owner_client.get(f"/brands/{brand_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert owner_client.get("/pool", follow_redirects=False).status_code == 303
    assert owner_client.get(f"/campaigns/1", follow_redirects=False).status_code == 303


def test_writable_default_shows_dynamic_home(owner_client):
    # 开发模式（测试基线）：GET / 是动态首页（默认品牌 + 管理入口），不是演示壳
    home = owner_client.get("/").text
    assert "敦煌当代美术馆" in home and "品牌库管理" in home
    assert "敦煌IP" not in home           # 不是演示壳


# ── 右上角「开发/演示」模式切换（DB 持久）──

def test_toggle_flips_mode_and_persists(owner_client):
    from app.core import runtime
    runtime.set_knowledge_writable(False)                     # 起始：演示模式
    r = owner_client.post("/settings/knowledge-writable", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert runtime.knowledge_writable() is True               # 切到开发模式并持久
    assert "品牌库管理" in owner_client.get("/").text          # 动态首页
    owner_client.post("/settings/knowledge-writable")         # 再点 → 切回演示
    assert runtime.knowledge_writable() is False
    assert "②选题库" in owner_client.get("/").text            # 演示壳


def test_toggle_requires_owner(editor_client):
    assert editor_client.post("/settings/knowledge-writable").status_code == 403


def test_toggle_button_hidden_from_owner(owner_client):
    home = owner_client.get("/").text
    assert "点击切换" not in home
    assert "开发模式" not in home
    assert "演示模式" not in home


def test_upload_extracts_text_and_analysis_uses_it(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import Brand, BrandDoc
    brand_id = _create_brand(owner_client)
    content = "敦煌的色彩体系与矿物颜料工艺"
    docx = ("brand.docx", _docx_bytes(content),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    r = owner_client.post(f"/brands/{brand_id}/docs", files={"file": docx},
                          follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc).where(BrandDoc.brand_id == brand_id)).first()
        assert doc is not None and content in doc.extracted_text
    # 直接同步跑 run_analysis（避开后台线程）：单篇解读 + 综合解读入库
    with Session(fresh_db) as s:
        analysis.run_analysis(brand_id, s)
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc).where(BrandDoc.brand_id == brand_id)).first()
        assert "stub:doc_analysis" in doc.ai_analysis         # 单篇解读（stub）
        assert s.get(Brand, brand_id).doc_digest               # 综合文档解读非空


def test_run_analysis_autofills_brand_fields(owner_client, fresh_db, monkeypatch):
    from sqlmodel import Session
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import Brand
    brand_id = _create_brand(owner_client)
    owner_client.post(f"/brands/{brand_id}/docs",
                      files={"file": ("a.txt", b"content", "text/plain")})
    # 让 LLM 返回 tngen 约定格式 → 自动填充主题调性/内容要求
    monkeypatch.setattr(analysis.llm, "generate_text",
                        lambda *a, **k: "调性：以身为尺的丝路\n要求：小红书500字")
    with Session(fresh_db) as s:
        analysis.run_analysis(brand_id, s)
    with Session(fresh_db) as s:
        b = s.get(Brand, brand_id)
        assert b.brand_prompt == "以身为尺的丝路" and b.content_notes == "小红书500字"


def test_parse_brand_fields_unit():
    from app.modules.knowledge.analysis import _parse_brand_fields
    bf = _parse_brand_fields("调性：A\n要求：B")
    assert bf == {"brand_prompt": "A", "content_notes": "B"}
    import pytest
    with pytest.raises(ValueError):
        _parse_brand_fields("没有格式的文本")


def test_deep_read_toggle(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import BrandDoc
    brand_id = _create_brand(owner_client)
    owner_client.post(f"/brands/{brand_id}/docs",
                      files={"file": ("a.pdf", b"x", "application/pdf")})
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc)).first()
    assert doc.deep_read is False
    owner_client.post(f"/brands/{brand_id}/docs/{doc.id}/deep-read")
    with Session(fresh_db) as s:
        assert s.get(BrandDoc, doc.id).deep_read is True


def test_editor_cannot_analyze_or_deepread(owner_client, editor_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import BrandDoc
    brand_id = _create_brand(owner_client)
    owner_client.post(f"/brands/{brand_id}/docs", files={"file": ("a.txt", b"x", "text/plain")})
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc)).first()
    assert editor_client.post(f"/brands/{brand_id}/analyze").status_code == 403
    assert editor_client.post(f"/brands/{brand_id}/docs/{doc.id}/deep-read").status_code == 403


# ── 知识库 v2：默认品牌 / 品牌定义 / 文档下载删除 / campaign 引用数据池 ──

def test_default_brand_created_on_first_home(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import Brand, Campaign
    owner_client.get("/")                                     # 首访触发建默认品牌
    with Session(fresh_db) as s:
        brands = s.exec(select(Brand)).all()
        assert len(brands) == 1 and brands[0].name == "敦煌当代美术馆"
        # 品牌日常已去除：首访只建品牌，不建默认 campaign
        assert s.exec(select(Campaign)).first() is None


def test_save_brand_define_persists(owner_client):
    brand_id = _create_brand(owner_client)
    r = owner_client.post(f"/brands/{brand_id}/define",
                          data={"brand_prompt": "以身为尺的丝路", "content_notes": "小红书500字"},
                          follow_redirects=False)
    assert r.status_code == 303
    page = owner_client.get(f"/brands/{brand_id}").text
    assert "以身为尺的丝路" in page and "小红书500字" in page


def test_brand_doc_download_and_delete(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import BrandDoc
    brand_id = _create_brand(owner_client)
    owner_client.post(f"/brands/{brand_id}/docs",
                      files={"file": ("brief.txt", b"hello", "text/plain")})
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc)).first()
    assert owner_client.get(f"/brands/{brand_id}/docs/{doc.id}/download").content == b"hello"
    r = owner_client.post(f"/brands/{brand_id}/docs/{doc.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    with Session(fresh_db) as s:
        assert s.get(BrandDoc, doc.id) is None


def _create_pool_topic(owner_client, fresh_db, title="非遗白皮书2026") -> int:
    from sqlmodel import Session, select
    from app.modules.knowledge.models import PoolTopic
    owner_client.post("/pool", data={"title": title, "kind": "资料包", "content": "参考"})
    with Session(fresh_db) as s:
        return s.exec(select(PoolTopic).where(PoolTopic.title == title)).first().id


def test_campaign_pool_ref_add_shows_and_remove(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import CampaignPoolRef
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    tid = _create_pool_topic(owner_client, fresh_db)
    # 引用 → 详情页「已引用」出现
    r = owner_client.post(f"/campaigns/{cid}/pool-refs",
                          data={"pool_topic_id": tid}, follow_redirects=False)
    assert r.status_code == 303
    assert "✅ 非遗白皮书2026" in owner_client.get(f"/campaigns/{cid}").text
    # 取消引用 → link 表清空
    owner_client.post(f"/campaigns/{cid}/pool-refs/{tid}/delete")
    with Session(fresh_db) as s:
        assert s.exec(select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == cid)).first() is None


def test_add_duplicate_pool_ref_idempotent(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import CampaignPoolRef
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    tid = _create_pool_topic(owner_client, fresh_db)
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": tid})
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": tid})  # 重复
    with Session(fresh_db) as s:
        refs = s.exec(select(CampaignPoolRef).where(CampaignPoolRef.campaign_id == cid)).all()
        assert len(refs) == 1                                # 幂等：不重复引用


def test_campaign_parse_with_ref_generates_digest(owner_client, fresh_db):
    from sqlmodel import Session
    from app.modules.knowledge.models import Campaign
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    tid = _create_pool_topic(owner_client, fresh_db)
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": tid})
    r = owner_client.post(f"/campaigns/{cid}/parse")         # 解析含引用数据池
    assert r.status_code == 200
    with Session(fresh_db) as s:
        assert "campaign_digest" in s.get(Campaign, cid).campaign_digest


def test_campaign_experience_pack_shows_in_pool_and_can_be_inherited(owner_client, fresh_db, monkeypatch):
    from datetime import datetime
    from sqlmodel import Session, select
    from app.modules.feedback.experience import create_experience_pair_from_slot
    from app.modules.feedback.models import FeedbackExperience
    from app.modules.knowledge.models import CampaignPoolRef, PoolTopic
    from app.modules.schedule import schedule
    from app.modules.schedule.models import ScheduleMetric
    from app.modules.topic.contract import KnowledgeContext
    from app.modules.topic.models import Topic
    from app.modules.writing.models import Article

    def fake_draft(session, slot_id, experience_type):
        return {
            "title": f"{experience_type}：物件切口有效",
            "summary": "从发布数据沉淀 campaign 方法",
            "positive_notes": "保留具体物件和现代场景",
            "negative_notes": "避免只讲抽象历史",
            "action_advice": "新 campaign 生成选题时优先复用这条经验",
        }

    monkeypatch.setattr("app.modules.feedback.experience.build_experience_draft", fake_draft)
    brand_id = _create_brand(owner_client)
    old_cid = _create_campaign(owner_client, brand_id, "丝路有多长")
    with Session(fresh_db) as s:
        topic = Topic(brand_id=brand_id, campaign_id=old_cid, title="习字简", status="采纳")
        s.add(topic)
        s.commit()
        s.refresh(topic)
        article = Article(topic_id=topic.id, campaign_id=old_cid, title="在边塞练字的人", body="正文", status="已审核")
        s.add(article)
        s.commit()
        s.refresh(article)
        week = schedule.add_week(s, brand_id)
        slot = schedule.add_slot(s, week.id, article.id, week.week_start, "09:30")
        slot = schedule.publish_slot(s, slot.id, "小红书", "https://example.com", datetime(2026, 7, 8, 9, 30))
        s.add(ScheduleMetric(
            slot_id=slot.id,
            article_id=article.id,
            topic_id=topic.id,
            brand_id=brand_id,
            campaign_id=old_cid,
            xhs_like=100,
            xhs_comment=10,
            xhs_collect=20,
        ))
        s.commit()
        create_experience_pair_from_slot(s, slot.id)
        pack = s.exec(select(PoolTopic).where(PoolTopic.source_campaign_id == old_cid)).first()
        assert pack is not None
        assert pack.kind == "经验包"
        assert "在边塞练字的人" in pack.content
        assert "选题经验" in pack.content and "写作经验" in pack.content
        pack_id = pack.id
        assert len(s.exec(select(FeedbackExperience).where(FeedbackExperience.source_slot_id == slot.id)).all()) == 2

    pool_page = owner_client.get("/pool")
    assert pool_page.status_code == 200
    assert "经验包" in pool_page.text
    assert "丝路有多长" in pool_page.text
    assert "在边塞练字的人" in pool_page.text
    assert "正向经验" in pool_page.text and "反向风险" in pool_page.text
    assert "删除经验包" in pool_page.text
    assert "2条经验" not in pool_page.text
    home = owner_client.get("/")
    assert "继承历史经验包" in home.text

    created = owner_client.post(
        "/campaigns",
        data={"brand_id": brand_id, "name": "新活动", "experience_pack_id": [pack_id]},
        follow_redirects=False,
    )
    assert created.status_code == 303
    new_cid = int(created.headers["location"].rsplit("/", 1)[-1])
    with Session(fresh_db) as s:
        ref = s.exec(
            select(CampaignPoolRef).where(
                CampaignPoolRef.campaign_id == new_cid,
                CampaignPoolRef.pool_topic_id == pack_id,
            )
        ).first()
        assert ref is not None
        ctx = KnowledgeContext.load(s, brand_id, new_cid)
        assert any("新 campaign 生成选题时优先复用" in item for item in ctx.pool_experiences)

    deleted = owner_client.post(f"/pool/{pack_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/pool"
    with Session(fresh_db) as s:
        assert s.get(PoolTopic, pack_id) is None
        assert not s.exec(select(CampaignPoolRef).where(CampaignPoolRef.pool_topic_id == pack_id)).all()
        entries = s.exec(select(FeedbackExperience).where(FeedbackExperience.campaign_id == old_cid)).all()
        assert entries and all(not entry.is_active for entry in entries)


def test_brand_evergreen_experience_pack_shows_in_pool(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.feedback.models import FeedbackExperience
    from app.modules.knowledge.models import PoolTopic

    brand_id = _create_brand(owner_client)
    with Session(fresh_db) as s:
        s.add(FeedbackExperience(
            brand_id=brand_id,
            campaign_id=None,
            platform="通用",
            experience_type="选题经验",
            title="品牌常青切口",
            summary="品牌常青也要沉淀复盘经验",
            action_advice="品牌常青生成选题时复用",
            performance_level="中表现",
        ))
        s.add(FeedbackExperience(
            brand_id=brand_id,
            campaign_id=None,
            platform="通用",
            experience_type="写作经验",
            title="品牌常青写法",
            summary="正文也要延续品牌常青经验",
            action_advice="品牌常青写作时复用",
            performance_level="中表现",
        ))
        s.commit()

    page = owner_client.get("/pool")
    assert page.status_code == 200
    assert "品牌常青" in page.text
    assert "1篇文章" in page.text
    with Session(fresh_db) as s:
        pack = s.exec(select(PoolTopic).where(PoolTopic.title == "经验包｜品牌常青")).first()
        assert pack is not None
        assert pack.source_campaign_id is None
        assert "品牌常青生成选题时复用" in pack.content


# ── 新写路由权限：editor 越权 → 403 ──

def test_editor_cannot_define_or_ref(owner_client, editor_client, fresh_db):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    assert editor_client.post(f"/brands/{brand_id}/define",
                              data={"brand_prompt": "x"}).status_code == 403
    assert editor_client.post(f"/campaigns/{cid}/pool-refs",
                              data={"pool_topic_id": "1"}).status_code == 403


def test_campaign_analysis_attaches_pool_image(owner_client, fresh_db, monkeypatch):
    """引用的图片型数据池（有文件、无正文）→ 作 vision 附件传给 LLM（修复"图片未附上"）。"""
    from sqlmodel import Session, select
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import PoolTopic
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    owner_client.post("/pool", data={"title": "竹简图", "kind": "资料包"},
                      files={"file": ("z.png", b"\x89PNG", "image/png")})
    with Session(fresh_db) as s:
        pid = s.exec(select(PoolTopic).where(PoolTopic.title == "竹简图")).first().id
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": pid})
    captured = {}

    def fake_gen(prompt, task="default", attachments=None, **k):
        captured["attachments"] = attachments or []
        return "digest"

    monkeypatch.setattr(analysis.llm, "generate_text", fake_gen)
    with Session(fresh_db) as s:
        analysis.run_campaign_analysis(cid, s)
    assert any(a.endswith(".png") for a in captured["attachments"])   # 图片作附件了


def test_pool_deep_read_toggle(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import PoolTopic
    owner_client.post("/pool", data={"title": "T", "kind": "资料包"},
                      files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    with Session(fresh_db) as s:
        t = s.exec(select(PoolTopic).where(PoolTopic.title == "T")).first()
        assert t.deep_read is False
    owner_client.post(f"/pool/{t.id}/deep-read", follow_redirects=False)
    with Session(fresh_db) as s:
        assert s.get(PoolTopic, t.id).deep_read is True


def test_campaign_analysis_pool_kind_label_and_pdf_deepread(owner_client, fresh_db, monkeypatch):
    """数据池引用带 kind 标签；PDF 默认走文字、开深度读图才作 vision 附件。"""
    from sqlmodel import Session, select
    from app.modules.knowledge import analysis
    from app.modules.knowledge.models import PoolTopic
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    owner_client.post("/pool", data={"title": "复盘A", "kind": "经验包", "content": "开箱视频转化高"})
    owner_client.post("/pool", data={"title": "手册B", "kind": "资料包"},
                      files={"file": ("m.pdf", b"%PDF", "application/pdf")})
    with Session(fresh_db) as s:
        exp = s.exec(select(PoolTopic).where(PoolTopic.title == "复盘A")).first()
        pdf = s.exec(select(PoolTopic).where(PoolTopic.title == "手册B")).first()
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": exp.id})
    owner_client.post(f"/campaigns/{cid}/pool-refs", data={"pool_topic_id": pdf.id})
    cap = {}

    def fake_gen(prompt, task="default", attachments=None, **k):
        cap["prompt"], cap["attachments"] = prompt, attachments or []
        return "d"

    monkeypatch.setattr(analysis.llm, "generate_text", fake_gen)
    with Session(fresh_db) as s:
        analysis.run_campaign_analysis(cid, s)
    assert "经验包" in cap["prompt"] and "资料包" in cap["prompt"]        # 带 kind 标签
    assert not any(a.endswith(".pdf") for a in cap["attachments"])        # PDF 未开深度读图 → 不附件
    owner_client.post(f"/pool/{pdf.id}/deep-read")                        # 开 PDF 深度读图
    with Session(fresh_db) as s:
        analysis.run_campaign_analysis(cid, s)
    assert any(a.endswith(".pdf") for a in cap["attachments"])           # 开了 → 作附件
