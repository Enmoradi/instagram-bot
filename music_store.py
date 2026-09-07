"""Small persistent catalog of search results and Telegram audio references."""
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager


class MusicStore:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
                    artist TEXT NOT NULL, duration INTEGER NOT NULL DEFAULT 0,
                    file_id TEXT, touched REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library (
                    user_id INTEGER NOT NULL, track_id TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0, touched REAL NOT NULL,
                    PRIMARY KEY(user_id, track_id)
                );
                CREATE INDEX IF NOT EXISTS library_user_time ON library(user_id, touched);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, title, artist='', source_id='', duration=0):
        identity = source_id or ('query:' + title + '\n' + artist)
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        with self.connection() as db:
            db.execute('''INSERT INTO tracks(id,source_id,title,artist,duration,touched)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET touched=excluded.touched''',
                (key, source_id, title[:180], artist[:120], duration, time.time()))
            # Keep recent catalog entries and any entries referenced by a personal library.
            db.execute('''DELETE FROM tracks WHERE id NOT IN (SELECT track_id FROM library)
                AND id NOT IN (SELECT id FROM tracks ORDER BY touched DESC LIMIT 2000)''')
        return key

    def get(self, key):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tracks WHERE id=?', (key,)).fetchone()
            return dict(row) if row else None

    def cache(self, key, file_id):
        with self.connection() as db:
            db.execute('UPDATE tracks SET file_id=? WHERE id=?', (file_id, key))

    def remember(self, uid, key):
        with self.connection() as db:
            db.execute('''INSERT INTO library(user_id,track_id,touched) VALUES(?,?,?)
                ON CONFLICT(user_id,track_id) DO UPDATE SET touched=excluded.touched''', (uid,key,time.time()))
            db.execute('''DELETE FROM library WHERE user_id=? AND favorite=0 AND track_id NOT IN
                (SELECT track_id FROM library WHERE user_id=? ORDER BY touched DESC LIMIT 20)''', (uid,uid))

    def toggle_favorite(self, uid, key):
        with self.connection() as db:
            row = db.execute('SELECT favorite FROM library WHERE user_id=? AND track_id=?', (uid,key)).fetchone()
            enabled = not (row and row['favorite'])
            count = db.execute('SELECT COUNT(*) FROM library WHERE user_id=? AND favorite=1', (uid,)).fetchone()[0]
            if enabled and count >= 100:
                raise ValueError('Favorite limit reached')
            db.execute('''INSERT INTO library(user_id,track_id,favorite,touched) VALUES(?,?,?,?)
                ON CONFLICT(user_id,track_id) DO UPDATE SET favorite=excluded.favorite,touched=excluded.touched''',
                (uid,key,int(enabled),time.time()))
        return enabled

    def listing(self, uid, favorites=False, offset=0):
        with self.connection() as db:
            return [dict(row) for row in db.execute('''SELECT t.* FROM tracks t JOIN library l ON t.id=l.track_id
                WHERE l.user_id=? AND (?=0 OR l.favorite=1) ORDER BY l.touched DESC LIMIT 11 OFFSET ?''',
                (uid,int(favorites),max(0,offset)))]

    def forget(self, uid):
        with self.connection() as db:
            db.execute('DELETE FROM library WHERE user_id=?', (uid,))
