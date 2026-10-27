"""Append-only snapshots; disappearance is never inferred from search omission."""
import json
import sqlite3
from datetime import datetime, timezone


class Archive:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL, observed_at TEXT NOT NULL,
            payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS snapshots_url ON snapshots(url, id);
        CREATE TABLE IF NOT EXISTS watches (url TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS search_queries (query TEXT PRIMARY KEY);
        ''')

    def watch(self, url):
        self.db.execute('INSERT OR IGNORE INTO watches VALUES (?)', (url,))
        self.db.commit()

    def unwatch(self, url):
        self.db.execute('DELETE FROM watches WHERE url=?', (url,))
        self.db.commit()

    def watches(self):
        return [r[0] for r in self.db.execute('SELECT url FROM watches ORDER BY url')]

    def save(self, url, item):
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
        old = self.db.execute('SELECT payload FROM snapshots WHERE url=? ORDER BY id DESC LIMIT 1', (url,)).fetchone()
        if old and old[0] == payload:
            return False
        self.db.execute('INSERT INTO snapshots(url,observed_at,payload) VALUES (?,?,?)',
                        (url, datetime.now(timezone.utc).isoformat(), payload))
        self.db.commit()
        return True

    def history(self, url):
        return [dict(r) for r in self.db.execute('SELECT observed_at,payload FROM snapshots WHERE url=? ORDER BY id', (url,))]

    def search(self, query, limit=10):
        rows = self.db.execute('''SELECT s.url,s.observed_at,s.payload FROM snapshots s
            JOIN (SELECT MAX(id) id FROM snapshots GROUP BY url) latest ON s.id=latest.id
            ORDER BY s.id DESC''')
        return [dict(r) for r in rows if query.casefold() in r['payload'].casefold()][:limit]

    def add_query(self, query):
        self.db.execute('INSERT OR IGNORE INTO search_queries VALUES (?)', (query,))
        self.db.commit()

    def remove_query(self, query):
        self.db.execute('DELETE FROM search_queries WHERE query=?', (query,))
        self.db.commit()

    def queries(self):
        return [r[0] for r in self.db.execute('SELECT query FROM search_queries ORDER BY query')]

    def save_discovery(self, url, item):
        # A search snippet must not replace a richer product snapshot.
        # Existing full cards are updated by the separate listing monitor.
        previous = self.db.execute('SELECT payload FROM snapshots WHERE url=? ORDER BY id DESC LIMIT 1', (url,)).fetchone()
        if previous and json.loads(previous[0]).get('capture_kind') != 'search_result':
            return False
        return self.save(url, item)
