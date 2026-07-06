"""③写作引擎给②选题库的只读契约。"""
from sqlmodel import Session, select

from app.modules.writing.models import Article


def writing_status_map(session: Session, topic_ids: list[int]) -> dict[int, str]:
    """topic_id -> ③当前写作/发布状态。

    ②只读这个映射来显示「已创作/已发布」分类；③不回写 Topic.status。
    未开始写作的 topic 不在返回里。
    """
    if not topic_ids:
        return {}
    rows = session.exec(select(Article).where(Article.topic_id.in_(topic_ids))).all()
    latest: dict[int, Article] = {}
    for row in rows:
        cur = latest.get(row.topic_id)
        if cur is None or row.updated_at >= cur.updated_at:
            latest[row.topic_id] = row
    return {topic_id: article.status for topic_id, article in latest.items()}
