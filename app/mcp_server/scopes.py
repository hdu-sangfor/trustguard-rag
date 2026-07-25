"""兼容导出；Scope Registry 已下沉到应用层。"""

from app.application.scopes import ScopeRegistry

__all__ = ["ScopeRegistry"]
