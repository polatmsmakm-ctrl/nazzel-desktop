"""What the assistant remembers about the user (preferences, names, habits): short notes the user can see and delete."""
import time
import uuid

from .settings import JsonStore, settings

MAX_ITEMS = 40
MAX_LEN = 200


class Memory:
    def __init__(self):
        self.store = JsonStore('assistant-memory.json', {'items': []})

    @property
    def enabled(self):
        return bool(settings().get('assistant_memory'))

    def list(self):
        return list(self.store.data.get('items', []))

    def add(self, text):
        text = ' '.join((text or '').split())[:MAX_LEN]
        if not text:
            return None
        items = [i for i in self.list() if i['text'] != text]
        item = {'id': uuid.uuid4().hex[:8], 'text': text, 'at': time.time()}
        items.append(item)
        self.store.data['items'] = items[-MAX_ITEMS:]
        self.store.save()
        return item

    def forget(self, query):
        """Removes the notes that contain every word of the query. Returns how many."""
        words = [w for w in (query or '').lower().split() if w]
        if not words:
            return 0
        keep, gone = [], 0
        for item in self.list():
            if all(w in item['text'].lower() for w in words):
                gone += 1
            else:
                keep.append(item)
        if gone:
            self.store.data['items'] = keep
            self.store.save()
        return gone

    def delete(self, item_id):
        self.store.data['items'] = [i for i in self.list() if i['id'] != item_id]
        self.store.save()

    def clear(self):
        self.store.data['items'] = []
        self.store.save()

    def prompt(self):
        """A block for the system prompt (empty when switched off or nothing saved)."""
        items = self.list() if self.enabled else []
        if not items:
            return ''
        return ('\n\nمعلومات عن المستخدم محفوظة من محادثات قبل (استخدمها لما تفيد، وما تذكرها له إلا إذا ناسب الكلام):\n'
                + '\n'.join('- ' + i['text'] for i in items))


MEMORY = Memory()
