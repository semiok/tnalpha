"""Shared Jinja template setup."""
from fastapi.templating import Jinja2Templates

from app.core.timezone import format_display_time, to_display_time


def create_templates(directory: str = "app/templates") -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory)
    templates.env.filters["cn_time"] = format_display_time
    templates.env.filters["to_cn_time"] = to_display_time
    return templates
