"""Persistent search settings only; product results are never stored here."""
import sqlite3


class Preferences:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        if self.get('initialized') is None:
            self.add('DMR')
            self.set('source', 'all')
            self.set('auto', 'off')
            self.set('initialized', 'yes')

    def get(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        self.db.commit()

    def add(self, name):
        name = ' '.join(name.split())
        if not 2 <= len(name) <= 200 or not any(c.isalnum() for c in name):
            raise ValueError('Название должно содержать от 2 до 200 символов и хотя бы одну букву или цифру.')
        cursor = self.db.execute('INSERT OR IGNORE INTO items(name,key) VALUES (?,?)', (name, name.casefold()))
        self.db.commit()
        return bool(cursor.rowcount)

    def remove(self, item_id):
        self.db.execute('DELETE FROM items WHERE id=?', (item_id,))
        self.db.commit()

    def items(self):
        return [dict(row) for row in self.db.execute('SELECT id,name FROM items ORDER BY id')]

    def item(self, item_id):
        row = self.db.execute('SELECT id,name FROM items WHERE id=?', (item_id,)).fetchone()
        return dict(row) if row else None
