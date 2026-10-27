"""Explicit private-chat access; IDs are local configuration, never public defaults."""
import json
from pathlib import Path


def allowed_users(owner, path):
    users = {owner}
    path = Path(path)
    if not path.exists():
        return users
    data = json.loads(path.read_text())
    extra = data.get('allowed_user_ids') if isinstance(data, dict) else None
    if not isinstance(extra, list) or any(type(value) is not int or value <= 0 for value in extra):
        raise ValueError('allowed_user_ids must be a list of positive integer Telegram IDs')
    return users | set(extra)


def attach_users(dispatcher, bot, owner, owner_path, users, engine=None):
    from .bot import create_router
    from .preferences import Preferences
    owner_path = Path(owner_path)
    databases = {}
    try:
        for user in sorted(set(users) | {owner}):
            path = owner_path if user == owner else owner_path.parent / 'users' / str(user) / 'preferences.sqlite3'
            path.parent.mkdir(parents=True, exist_ok=True)
            prefs = Preferences(path)
            databases[user] = prefs
            dispatcher.include_router(create_router(bot, user, prefs, engine))
    except Exception:
        for prefs in databases.values():
            prefs.db.close()
        raise
    return databases
