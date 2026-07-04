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

def test_create_brand_auto_creates_default_campaign(owner_client):
    _create_brand(owner_client)
    # 常驻 campaign「品牌日常」显示在首页（campaign 已挪到首页，不在品牌定义页）
    assert "品牌日常" in owner_client.get("/").text


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


def test_delete_default_campaign_forbidden(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import Campaign
    brand_id = _create_brand(owner_client)
    with Session(fresh_db) as s:
        default = s.exec(select(Campaign).where(Campaign.brand_id == brand_id)).first()
    resp = owner_client.post(f"/campaigns/{default.id}/delete")
    assert resp.status_code == 400          # 品牌日常不可删


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


def test_parse_campaign_stores_digest(owner_client, fresh_db):
    from sqlmodel import Session
    from app.modules.knowledge.models import Campaign
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    resp = owner_client.post(f"/campaigns/{cid}/parse")
    assert resp.status_code == 200
    assert "stub:campaign_digest" in resp.text
    with Session(fresh_db) as s:
        assert "stub:campaign_digest" in s.get(Campaign, cid).campaign_digest


# ── 数据池 增查 ──

def test_pool_add_and_list(owner_client):
    resp = owner_client.post("/pool",
                             data={"title": "618复盘经验", "kind": "经验包",
                                   "brand_tag": "敦煌", "content": "转化率要点"},
                             follow_redirects=False)
    assert resp.status_code == 303
    page = owner_client.get("/pool").text
    assert "618复盘经验" in page and "经验包" in page


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
    # 六模块 tab 导航齐全
    for tab in ("①知识库", "②选题库", "③写作引擎", "④排期版", "⑤数据反馈", "⑥权限"):
        assert tab in home
    assert "敦煌IP" in home and "敦煌当代美术展" in home   # ①知识库那屏内容
    assert "模型配置" in home and "退出登录" in home       # 顶栏融了真实 app 入口
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


def test_toggle_button_shown_to_owner(owner_client):
    assert "点击切换" in owner_client.get("/").text            # 顶栏切换按钮（开发模式 base.html）


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
        assert s.exec(select(Campaign).where(Campaign.is_default == True)).first() is not None  # noqa: E712


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


# ── 新写路由权限：editor 越权 → 403 ──

def test_editor_cannot_define_or_ref(owner_client, editor_client, fresh_db):
    brand_id = _create_brand(owner_client)
    cid = _create_campaign(owner_client, brand_id)
    assert editor_client.post(f"/brands/{brand_id}/define",
                              data={"brand_prompt": "x"}).status_code == 403
    assert editor_client.post(f"/campaigns/{cid}/pool-refs",
                              data={"pool_topic_id": "1"}).status_code == 403
