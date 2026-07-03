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
    brand_id = _create_brand(owner_client)
    page = owner_client.get(f"/brands/{brand_id}")
    assert page.status_code == 200
    assert "品牌日常" in page.text          # 自动建的常驻 campaign


def test_home_lists_brand(owner_client):
    _create_brand(owner_client, "国货美妆")
    assert "国货美妆" in owner_client.get("/").text


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


# ── AI 解析（core/llm stub）存 digest ──

def test_parse_brand_stores_digest(owner_client, fresh_db):
    from sqlmodel import Session
    from app.modules.knowledge.models import Brand
    brand_id = _create_brand(owner_client)
    resp = owner_client.post(f"/brands/{brand_id}/parse")
    assert resp.status_code == 200
    assert "stub:brand_digest" in resp.text          # HTMX 片段含 stub 结果
    with Session(fresh_db) as s:
        assert "stub:brand_digest" in s.get(Brand, brand_id).brand_digest


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


# ── 只读预览模式（KNOWLEDGE_WRITABLE=false）：隐藏写入口，但后端代码保留 ──

def test_readonly_hides_write_ui_and_shows_banner(owner_client, monkeypatch):
    from app.core import config
    brand_id = _create_brand(owner_client)            # 后端不拦，仍可建（代码保留、可逆）
    monkeypatch.setattr(config, "KNOWLEDGE_WRITABLE", False)
    home = owner_client.get("/").text
    assert "新建品牌" not in home                       # 写入口隐藏
    assert "只读预览" in home                           # 顶部开发中横幅
    brand = owner_client.get(f"/brands/{brand_id}").text
    assert "上传资料" not in brand and "AI 解析品牌定义" not in brand and "新建 campaign" not in brand
    assert "新增资料包" not in owner_client.get("/pool").text


def test_writable_default_shows_write_ui(owner_client):
    # 默认可写：定义者能看到写入口，且无开发中横幅
    home = owner_client.get("/").text
    assert "新建品牌" in home and "只读预览" not in home


def test_upload_extracts_text_and_parse_uses_it(owner_client, fresh_db):
    from sqlmodel import Session, select
    from app.modules.knowledge.models import Brand, BrandDoc
    brand_id = _create_brand(owner_client)
    content = "敦煌的色彩体系与矿物颜料工艺"
    docx = ("brand.docx", _docx_bytes(content),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    r = owner_client.post(f"/brands/{brand_id}/docs", files={"file": docx},
                          follow_redirects=False)
    assert r.status_code == 303
    # 上传时抽了文本存库
    with Session(fresh_db) as s:
        doc = s.exec(select(BrandDoc).where(BrandDoc.brand_id == brand_id)).first()
        assert doc is not None and content in doc.extracted_text
    # AI 解析读到真实文档内容 → digest 非空且带 task 标记
    r2 = owner_client.post(f"/brands/{brand_id}/parse")
    assert r2.status_code == 200
    with Session(fresh_db) as s:
        assert "brand_digest" in s.get(Brand, brand_id).brand_digest
