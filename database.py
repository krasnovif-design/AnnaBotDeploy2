import sqlite3
import json

class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect("memory.db")
        self.conn.execute("CREATE TABLE IF NOT EXISTS memory (user_id INTEGER PRIMARY KEY, history TEXT)")
    
    def get_history(self, uid):
        r = self.conn.execute("SELECT history FROM memory WHERE user_id=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else []
    
    def add_message(self, uid, role, content, max_h=1000):
        h = self.get_history(uid)
        h.append({"role": role, "content": content})
        if len(h) > max_h:
            h = h[-max_h:]
        self.conn.execute("INSERT OR REPLACE INTO memory VALUES (?,?)", (uid, json.dumps(h)))
        self.conn.commit()
    
    def clear(self, uid):
        self.conn.execute("DELETE FROM memory WHERE user_id=?", (uid,))
        self.conn.commit()
