"""文件存储抽象——本地磁盘起步（`config.DATA_DIR` 下），后可换对象存储，接口不变。

模块上传文件走这里，不自己拼路径。返回的是落盘后的存储路径，存进 *Doc.file_path。

    from app.core import storage
    path = storage.save_upload(upload_file, subdir="brand/3")
"""
import os
import uuid

from app.core import config


def save_upload(file, subdir: str = "") -> str:
    """把上传对象（FastAPI UploadFile，或任何含 `.filename` / `.file` 的对象）落盘。

    - 文件名随机化（避免碰撞/穿越），保留原扩展名。
    - 返回相对/绝对存储路径（DATA_DIR 下），供 *Doc.file_path 记录。
    """
    dest_dir = os.path.join(config.DATA_DIR, subdir)
    os.makedirs(dest_dir, exist_ok=True)

    original = getattr(file, "filename", "") or ""
    ext = os.path.splitext(original)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(dest_dir, stored_name)

    data = file.file.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return path
