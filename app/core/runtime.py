"""全站运行时开关——读写 DB(AppSetting)，通过维护接口切换、持久保存。

`knowledge_writable()` 每次读 DB(动态引用 db.engine，测试可指向测试库)；
DB 不可用/无表时回退 config 初始默认，保证不崩。
"""
from sqlmodel import Session

from app.core import config, db


def knowledge_writable() -> bool:
    """当前是否开发模式(动态知识库)。False=演示模式(只读演示壳)。"""
    try:
        from app.core.settings import get_app_settings
        with Session(db.engine) as s:
            return get_app_settings(s).knowledge_writable
    except Exception as e:                       # 首启无表/DB 不可用 → 回退 config 默认
        print(f"[runtime] 读 AppSetting 失败，回退 config 默认：{e}")
        return config.KNOWLEDGE_WRITABLE


def set_knowledge_writable(value: bool) -> None:
    """切换开发/演示模式并持久到 DB。"""
    from app.core.settings import get_app_settings
    with Session(db.engine) as s:
        st = get_app_settings(s)
        st.knowledge_writable = value
        s.add(st)
        s.commit()
