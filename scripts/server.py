"""Local soft exam knowledge-card server with SQLite storage."""
import base64, gzip, hashlib, json, mimetypes, re, sqlite3, subprocess, sys, tempfile, threading, time, uuid, webbrowser
import xml.etree.ElementTree as ET
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
from app_config import exam_config, model_config, server_config
from cpolar_client import tunnel_status
from ollama_engine import models, organize_knowledge as generate, h2_ranges, example_blocks, image_references, source_rows, analyze_points, \
	meaningful_cloze, CHECKPOINT_VERSION

ROOT = Path (__file__).resolve ().parents[1];
EXAM = exam_config ();
DB = EXAM['database'];
NOTES_DIR = EXAM['notes_dir'];
WEB = ROOT / 'web';
LOCK = threading.RLock ()
JOBS = {};
JOB_LOCK = threading.Lock ()
PICKS = {}
QUEUE_LOCK = threading.RLock ();
QUEUE_RUNNING = False;
QUEUE_WATCHDOG = None
QUEUE_RETRY_BASE_SECONDS = 30;
QUEUE_RETRY_MAX_SECONDS = 300;
QUEUE_WATCHDOG_INTERVAL_SECONDS = 5
@contextmanager
def connect ():
	c = sqlite3.connect (DB, timeout=30)
	try:
		c.row_factory = sqlite3.Row;
		c.execute ('PRAGMA journal_mode=WAL');
		c.execute ('PRAGMA foreign_keys=ON')
		with c:
			yield c
	finally:
		c.close ()


def init ():
	DB.parent.mkdir (parents=True, exist_ok=True)
	with connect () as c:
		c.executescript ('''CREATE TABLE IF NOT EXISTS notes
                            (
                                name    TEXT PRIMARY KEY,
                                digest  TEXT NOT NULL,
                                updated TEXT NOT NULL,
                                content TEXT
                            );
        CREATE TABLE IF NOT EXISTS app_meta
        (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cards
        (
            id              TEXT PRIMARY KEY,
            note            TEXT NOT NULL,
            section         TEXT NOT NULL,
            original        TEXT NOT NULL,
            answer          TEXT NOT NULL,
            options         TEXT NOT NULL,
            line            INTEGER DEFAULT 0,
            topic           TEXT NOT NULL,
            active          INTEGER DEFAULT 1,
            created         TEXT NOT NULL,
            favorite        INTEGER DEFAULT 0,
            wrong_marked    INTEGER DEFAULT 0,
            round1_done     INTEGER DEFAULT 0,
            retry_deferred  INTEGER DEFAULT 0,
            h2              TEXT    DEFAULT '',
            wrong_marked_at TEXT,
            favorite_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews
        (
            card_id       TEXT PRIMARY KEY,
            attempts      INTEGER DEFAULT 0,
            wrong         INTEGER DEFAULT 0,
            streak        INTEGER DEFAULT 0,
            interval_days REAL    DEFAULT 0,
            due           TEXT,
            last_correct  INTEGER DEFAULT 0,
            FOREIGN KEY (card_id) REFERENCES cards (id)
        );
        CREATE TABLE IF NOT EXISTS attempts
        (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id  TEXT    NOT NULL,
            selected TEXT    NOT NULL,
            correct  INTEGER NOT NULL,
            created  TEXT    NOT NULL,
            FOREIGN KEY (card_id) REFERENCES cards (id)
        );
        CREATE TABLE IF NOT EXISTS media
        (
            id      TEXT PRIMARY KEY,
            note    TEXT NOT NULL,
            path    TEXT NOT NULL,
            title   TEXT NOT NULL,
            mime    TEXT NOT NULL,
            data    BLOB NOT NULL,
            annotation TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_group_members
        (
            member_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
            root_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            position INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_card_group_root ON card_group_members(root_id,position);
        CREATE TABLE IF NOT EXISTS generation_batches
        (
            note     TEXT    NOT NULL,
            digest   TEXT    NOT NULL,
            model    TEXT    NOT NULL,
            version  TEXT    NOT NULL,
            batch_no INTEGER NOT NULL,
            cards    TEXT    NOT NULL,
            rejected INTEGER NOT NULL,
            PRIMARY KEY (note, digest, model, version, batch_no)
        );
        CREATE INDEX IF NOT EXISTS idx_cards_note ON cards (note);
        CREATE INDEX IF NOT EXISTS idx_attempts_card ON attempts (card_id);''')
		columns = {row['name'] for row in c.execute ('PRAGMA table_info(cards)')}
		if 'favorite' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN favorite INTEGER DEFAULT 0')
		if 'wrong_marked' not in columns:
			c.execute ('ALTER TABLE cards ADD COLUMN wrong_marked INTEGER DEFAULT 0')
			c.execute ('UPDATE cards SET wrong_marked=1 WHERE id IN (SELECT card_id FROM reviews WHERE wrong>0)')
		if 'round1_done' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN round1_done INTEGER DEFAULT 0')
		if 'retry_deferred' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN retry_deferred INTEGER DEFAULT 0')
		if 'random_return_pending' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN random_return_pending INTEGER DEFAULT 0')
		if 'persistent_unknown' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN persistent_unknown INTEGER DEFAULT 0')
		if 'manual' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN manual INTEGER DEFAULT 0')
		note_columns = {row['name'] for row in c.execute ('PRAGMA table_info(notes)')}
		if 'content' not in note_columns: c.execute ('ALTER TABLE notes ADD COLUMN content TEXT')
		if 'source_path' not in note_columns: c.execute ('ALTER TABLE notes ADD COLUMN source_path TEXT')
		if 'processor' not in note_columns: c.execute ('ALTER TABLE notes ADD COLUMN processor TEXT')
		if 'wrong_marked_at' not in columns:
			c.execute ('ALTER TABLE cards ADD COLUMN wrong_marked_at TEXT')
			c.execute ('''UPDATE cards
                          SET wrong_marked_at=COALESCE(
                                  (SELECT MIN(created) FROM attempts WHERE card_id = cards.id AND correct = 0), created)
                          WHERE wrong_marked = 1''')
		if 'favorite_at' not in columns:
			c.execute ('ALTER TABLE cards ADD COLUMN favorite_at TEXT')
			c.execute ('UPDATE cards SET favorite_at=created WHERE favorite=1')
		if 'h2' not in columns:
			c.execute ("ALTER TABLE cards ADD COLUMN h2 TEXT DEFAULT ''")
			for note in c.execute ('SELECT name,content FROM notes WHERE content IS NOT NULL').fetchall ():
				scopes = h2_ranges (note['content'])
				for card in c.execute ('SELECT id,line FROM cards WHERE note=?', (note['name'],)).fetchall ():
					title = next ((s['title'] for s in scopes if s['start'] <= card['line'] <= s['end']), '未分组')
					c.execute ('UPDATE cards SET h2=? WHERE id=?', (title, card['id']))
		if 'kind' not in columns: c.execute ("ALTER TABLE cards ADD COLUMN kind TEXT DEFAULT 'cloze'")
		if 'media_id' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN media_id TEXT')
		if 'sort_order' not in columns: c.execute ('ALTER TABLE cards ADD COLUMN sort_order INTEGER')
		media_columns = {row['name'] for row in c.execute ('PRAGMA table_info(media)')}
		if 'annotation' not in media_columns: c.execute ("ALTER TABLE media ADD COLUMN annotation TEXT NOT NULL DEFAULT ''")
		backfill_source_order (c)
	if not rows ("SELECT value FROM app_meta WHERE key='image_backfill_v1'"):
		backfill_local_materials ()
		with connect () as c: c.execute ("INSERT INTO app_meta(key,value) VALUES('image_backfill_v1',?)", (now (),))
	refresh_image_titles ()
	resume_import_queue ()


def backfill_source_order (db):
	"""Recover the source order of split cards saved before sort_order existed."""
	groups = db.execute ('''SELECT note,line FROM cards WHERE active=1 AND kind='knowledge' AND sort_order IS NULL
	                      GROUP BY note,line HAVING COUNT(*)>1''').fetchall ()
	for group in groups:
		note = db.execute ('SELECT content FROM notes WHERE name=?', (group['note'],)).fetchone ()
		if not note or not note['content']: continue
		content = note['content']
		lines = content.splitlines (keepends=True)
		start = sum (len (line) for line in lines[:max (0, group['line']-1)])
		cards_at_line = db.execute ('''SELECT id,original FROM cards WHERE note=? AND line=? AND active=1
			AND kind='knowledge' AND sort_order IS NULL ORDER BY created,id''',
			(group['note'], group['line'])).fetchall ()
		positions = [(content.find (card['original'], start), card['id']) for card in cards_at_line]
		if any (position < 0 for position, _ in positions) or len ({position for position, _ in positions}) != len (positions):
			continue
		for order, (_, card_id) in enumerate (sorted (positions)):
			db.execute ('UPDATE cards SET sort_order=? WHERE id=?', (order, card_id))


def queue_path (): return DB.with_name ('import-queue.json')


def read_import_queue ():
	path = queue_path ()
	if not path.exists (): return None
	return json.loads (path.read_text (encoding='utf-8'))


def write_import_queue (state):
	path = queue_path ();
	path.parent.mkdir (parents=True, exist_ok=True)
	temp = path.with_suffix ('.json.tmp')
	temp.write_text (json.dumps (state, ensure_ascii=False), encoding='utf-8')
	temp.replace (path)


def outdated_import_queue (state):
	return bool (state and state.get ('pending') and state.get ('generator_version') != CHECKPOINT_VERSION)


def import_queue_status ():
	with QUEUE_LOCK:
		state = read_import_queue ()
		if not state: return {'active': False, 'pending': [], 'completed': []}
		result = {key: state.get (key) for key in
		          ('id', 'pending', 'completed', 'error', 'started', 'retry_at', 'retry_count')}
		result['pending'] = [item['name'] for item in state['pending']]
		result['active'] = bool (state['pending']) and QUEUE_RUNNING
		result['outdated'] = outdated_import_queue (state)
		job_id = state.get ('job_id')
		if job_id:
			with JOB_LOCK: result['progress'] = dict (JOBS.get (job_id, {}))
		return result


def resume_import_queue ():
	global QUEUE_RUNNING
	with QUEUE_LOCK:
		state = read_import_queue ()
		if QUEUE_RUNNING or not state or not state['pending'] or outdated_import_queue (state): return
		if state.get ('error'):
			if time.time () < float (state.get ('retry_at') or 0): return
			state['error'] = None;
			state['retry_at'] = None;
			state.pop ('job_id', None)
			write_import_queue (state)
		QUEUE_RUNNING = True
	threading.Thread (target=run_import_queue, daemon=True).start ()


def start_queue_watchdog ():
	global QUEUE_WATCHDOG
	with QUEUE_LOCK:
		if QUEUE_WATCHDOG and QUEUE_WATCHDOG.is_alive (): return
		
		def watch ():
			last_error = None
			while True:
				time.sleep (QUEUE_WATCHDOG_INTERVAL_SECONDS)
				try:
					resume_import_queue ();last_error = None
				except Exception as ex:
					if str (ex) != last_error: print ('导入队列检查失败：' + str (ex), file=sys.stderr, flush=True)
					last_error = str (ex)
		
		QUEUE_WATCHDOG = threading.Thread (target=watch, daemon=True, name='import-queue-watchdog')
		QUEUE_WATCHDOG.start ()


def run_import_queue ():
	global QUEUE_RUNNING
	try:
		while True:
			with QUEUE_LOCK:
				state = read_import_queue ()
				if not state or not state['pending'] or state.get ('error') or outdated_import_queue (state): return
				item = state['pending'][0]
			try:
				source = Path (item['path']) if item.get ('path') else None
				markdown = source.read_text (encoding='utf-8-sig') if source else item['markdown']
				task = start_generation (
					item['name'], markdown,
					state['model'], state['skip_unchanged'], source_path=source, checkpoint=True)
				if 'job_id' in task:
					with QUEUE_LOCK:
						state = read_import_queue ();
						state['job_id'] = task['job_id'];
						write_import_queue (state)
					while True:
						with JOB_LOCK:
							job = dict (JOBS[task['job_id']])
						if job['done']: break
						time.sleep (0.5)
					if job['error']: raise RuntimeError (job['error'])
					result = job['result']
				else:
					result = task
				with QUEUE_LOCK:
					state = read_import_queue ()
					state['completed'].append ({'name': item['name'], 'result': result})
					state['pending'].pop (0);
					state.pop ('job_id', None)
					state['error'] = None;
					state['retry_count'] = 0;
					state['retry_at'] = None
					write_import_queue (state)
			except Exception as ex:
				with QUEUE_LOCK:
					state = read_import_queue ();
					state['error'] = str (ex);
					state.pop ('job_id', None)
					state['retry_count'] = int (state.get ('retry_count') or 0) + 1
					state['retry_at'] = time.time () + min (QUEUE_RETRY_BASE_SECONDS * 2 ** min (
						state['retry_count'] - 1, 10), QUEUE_RETRY_MAX_SECONDS)
					write_import_queue (state)
				return
	finally:
		with QUEUE_LOCK:
			QUEUE_RUNNING = False


def start_import_queue (items, model, skip_unchanged):
	if not items: raise ValueError ('请先选择 Markdown 笔记文件')
	with QUEUE_LOCK:
		existing = read_import_queue ()
		if QUEUE_RUNNING: raise ValueError ('已有导入任务正在运行，请稍后再试')
		if existing and existing['pending']:
			if not outdated_import_queue (existing): raise ValueError ('还有未处理完的导入任务，请先继续上次导入')
			archive = queue_path ().with_name ('import-queue.previous.' + uuid.uuid4 ().hex + '.json')
			archive.write_text (json.dumps (existing, ensure_ascii=False), encoding='utf-8')
		state = {'id': uuid.uuid4 ().hex, 'pending': items, 'completed': [], 'error': None, 'retry_count': 0,
		         'retry_at': None, 'model': model, 'skip_unchanged': bool (skip_unchanged),
		         'generator_version': CHECKPOINT_VERSION, 'started': now ()}
		write_import_queue (state)
	resume_import_queue ()
	return import_queue_status ()


def retry_import_queue ():
	with QUEUE_LOCK:
		state = read_import_queue ()
		if not state or not state['pending']: raise ValueError ('没有待处理的导入任务')
		if outdated_import_queue (state): raise ValueError ('旧导入队列使用过时的出题规则，请重新选择文件开始导入')
		state['error'] = None;
		state['retry_count'] = 0;
		state['retry_at'] = None;
		write_import_queue (state)
	resume_import_queue ()
	return import_queue_status ()


def resolve_note_path (note, markdown):
	expected = hashlib.sha256 (markdown.encode ()).hexdigest ()
	stored = rows ('SELECT source_path FROM notes WHERE name=?', (note,))
	def matching (paths):
		found = []
		for path in paths:
			try:
				if path.is_file () and hashlib.sha256 (path.read_text (encoding='utf-8-sig').encode ()).hexdigest () == expected:
					resolved = path.resolve ()
					if resolved not in found: found.append (resolved)
			except (OSError, UnicodeError):
				continue
		return found
	if stored and stored[0].get ('source_path'):
		found = matching ([Path (stored[0]['source_path'])])
		if found: return found[0]
	if NOTES_DIR.is_dir ():
		found = matching (NOTES_DIR.rglob (note))
		if len (found) == 1: return found[0]
	if (ROOT / 'notes').is_dir ():
		found = matching ((ROOT / 'notes').rglob (note))
		if len (found) == 1: return found[0]
	return None


def require_image_source (note, markdown, source_path, line_range=None):
	if source_path: return
	for ref in image_references (markdown, note):
		if line_range and not line_range[0] <= ref['line'] <= line_range[1]: continue
		if not rows ('SELECT id FROM media WHERE note=? AND path=?', (note, ref['path'])):
			raise ValueError ('笔记引用了图片，但无法定位它所在的目录。请使用页面上的“选择文件”按钮重新选择 Markdown；无需单独选择图片。')


def now (): return datetime.now (timezone.utc).isoformat ()


def rows (sql, args=()):
	with connect () as c: return [dict (r) for r in c.execute (sql, args)]


def checkpoint_key (note, markdown, model):
	return (note, hashlib.sha256 (markdown.encode ()).hexdigest (), model or '', CHECKPOINT_VERSION)


def load_generation_batches (note, markdown, model):
	saved = rows ('SELECT batch_no,cards,rejected FROM generation_batches WHERE note=? AND digest=? AND model=? AND version=?', checkpoint_key (note, markdown, model))
	return {item['batch_no']: {'cards': json.loads (item['cards']), 'rejected': item['rejected']} for item in saved}


def save_generation_batch (key, batch_no, cards, rejected):
	with LOCK, connect () as db:
		db.execute ('INSERT OR REPLACE INTO generation_batches(note,digest,model,version,batch_no,cards,rejected) VALUES(?,?,?,?,?,?,?)', (
			*key, batch_no, json.dumps (cards, ensure_ascii=False), rejected))


def clear_generation_batches (key):
	with LOCK, connect () as db: db.execute ('DELETE FROM generation_batches WHERE note=? AND digest=? AND model=? AND version=?', key)


def clean_card (c, note):
	kind = c.get ('kind', 'cloze')
	if kind in ('image', 'example', 'knowledge'):
		original = str (c.get ('original', '')).strip ();
		answer = str (c.get ('answer', '')).strip ();
		opt = c.get ('options', [])
		if not original or not answer or not isinstance (opt, list) or answer not in opt or len (set (opt)) != len (opt): return None
		section = str (c.get ('section') or '未分类')[:150];
		h2 = str (c.get ('h2') or '')[:150];
		line = int (c.get ('line') or 0)
		identity = str (c.get ('identity') or (original + '\0' + answer))
		cid = kind[0] + hashlib.sha256 ((note + '\0' + identity).encode ()).hexdigest ()[:24]
		topic = 't' + hashlib.sha256 ((note + '\0' + original).encode ()).hexdigest ()[:20]
		return (cid, note, section, h2, original, answer, json.dumps (opt, ensure_ascii=False), line, topic, now (),
		        kind, c.get ('media_id'))
	answer = c.get ('answer', '').strip ();
	sentence = c.get ('sentence', '');
	original = c.get ('original') or sentence.replace ('【' + answer + '】', answer)
	if not meaningful_cloze (original, answer) or not (2 <= len (answer) <= 80): return None
	opt = c.get ('options', [])
	if not isinstance (opt, list) or len (opt) != 4 or len (set (opt)) != 4 or answer not in opt: return None
	section = str (c.get ('section') or '未分类')[:150];
	h2 = str (c.get ('h2') or '')[:150];
	line = int (c.get ('line') or 0)
	# One stable ID per note + exact source fragment + cloze term; preserves history on regeneration.
	cid = 'c' + hashlib.sha256 ((note + '\0' + original + '\0' + answer).encode ()).hexdigest ()[:24]
	topic = 't' + hashlib.sha256 ((note + '\0' + original).encode ()).hexdigest ()[:20]
	return (cid, note, section, h2, original, answer, json.dumps (opt, ensure_ascii=False), line, topic, now (),
	        'cloze', None)


def fill_h2 (db, note, markdown):
	scopes = h2_ranges (markdown)
	for card in db.execute ("SELECT id,line FROM cards WHERE note=? AND (h2='' OR h2 IS NULL)", (note,)).fetchall ():
		title = next ((s['title'] for s in scopes if s['start'] <= card['line'] <= s['end']), '未分组')
		db.execute ('UPDATE cards SET h2=? WHERE id=?', (title, card['id']))


def refresh_image_titles ():
	"""Update imported image captions from saved Markdown without changing study history."""
	with LOCK, connect () as db:
		for note in db.execute ("SELECT name,content FROM notes WHERE content IS NOT NULL AND content<>''").fetchall ():
			refs = image_references (note['content'], note['name'])
			by_location = {(ref['line'], ref['path']): ref for ref in refs}
			for card in db.execute ('''SELECT c.id,c.line,m.path,c.original,c.section,c.h2,m.title
			                          FROM cards c JOIN media m ON m.id=c.media_id
			                          WHERE c.note=? AND c.kind='image' ''', (note['name'],)).fetchall ():
				ref = by_location.get ((card['line'], card['path']))
				if not ref: continue
				if (card['original'], card['section'], card['h2']) != (ref['title'], ref['title'], ref['h2']):
					db.execute ('UPDATE cards SET original=?,section=?,h2=? WHERE id=?',
					            (ref['title'], ref['title'], ref['h2'], card['id']))
				if card['title'] != ref['title']:
					db.execute ('UPDATE media SET title=? WHERE note=? AND path=?',
					            (ref['title'], note['name'], ref['path']))


def save_cards (note, markdown, cards, replace=True, scope_range=None, store_source=True, hard_replace=False, media_items=None, line_shift=None, replace_title=None, source_path=None, processor=None):
	validated = {}
	scopes = h2_ranges (markdown) if store_source and note.lower ().endswith (('.md', '.markdown')) else []
	for c in cards:
		if not c.get ('h2') and c.get ('line'):
			title = next ((s['title'] for s in scopes if s['start'] <= int (c['line']) <= s['end']), '未分组')
			c = dict (c, h2=title)
		v = clean_card (c, note)
		if v: validated[v[0]] = v
	digest = hashlib.sha256 (markdown.encode ()).hexdigest ();
	old = rows ('SELECT digest FROM notes WHERE name=?', (note,));
	with LOCK, connect () as db:
		source = markdown if store_source and note.lower ().endswith (('.md', '.markdown')) else None
		if store_source or not old:
			db.execute ('''INSERT INTO notes(name, digest, updated, content, source_path, processor)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(name) DO UPDATE SET digest=excluded.digest,
                                                           updated=excluded.updated,
                                                           content=COALESCE(excluded.content, notes.content),
                                                           source_path=COALESCE(excluded.source_path, notes.source_path),
                                                           processor=COALESCE(excluded.processor, notes.processor)''', (
				note, digest, now (), source, str (source_path) if source_path else None, processor))
		if line_shift and line_shift[1]: db.execute ('UPDATE cards SET line=line+? WHERE note=? AND line>=?', (
			line_shift[1], note, line_shift[0]))
		if replace and hard_replace:
			if replace_title:
				existing = [r['id'] for r in
				            db.execute ('SELECT id FROM cards WHERE note=? AND h2=?', (note, replace_title))]
			elif scope_range:
				scope_title = next ((s['title'] for s in scopes if s['start'] == scope_range[0]), None)
				existing = [r['id'] for r in
				            db.execute ("SELECT id FROM cards WHERE note=? AND (h2=? OR ((h2='' OR h2 IS NULL) AND line>=? AND line<=?))", (
					            note, scope_title, *scope_range))]
			else:
				existing = [r['id'] for r in db.execute ('SELECT id FROM cards WHERE note=?', (note,))]
			remove_card_ids (db, existing)
		for item in media_items or []:
			db.execute ('''INSERT INTO media(id, note, path, title, mime, data, created)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                                                         mime=excluded.mime,
                                                         data=excluded.data,
                                                         created=excluded.created''', item)
		for v in validated.values ():
			db.execute ('''INSERT INTO cards(id, note, section, h2, original, answer, options, line, topic, active,
                                             created, kind, media_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
			                           ON CONFLICT(id) DO UPDATE SET section=CASE WHEN cards.manual=1 AND cards.kind='knowledge' THEN cards.section ELSE excluded.section END,
                                                         h2=excluded.h2,
			                                                         original=CASE WHEN cards.kind='image' THEN excluded.original ELSE cards.original END,
                                                         options=excluded.options,
                                                         line=excluded.line,
                                                         active=1,
                                                         kind=excluded.kind,
                                                         media_id=excluded.media_id''', v)
		if replace and validated and not hard_replace:
			keys = set (validated)
			if scope_range:
				existing = [r['id'] for r in
				            db.execute ('SELECT id FROM cards WHERE note=? AND active=1 AND manual=0 AND line>=? AND line<=?', (note,
				                                                                                                   *scope_range))]
			else:
				existing = [r['id'] for r in db.execute ('SELECT id FROM cards WHERE note=? AND active=1 AND manual=0', (note,))]
			for k in existing:
				if k not in keys: db.execute ('UPDATE cards SET active=0 WHERE id=?', (k,))
		if source: fill_h2 (db, note, markdown)
		if hard_replace:
			db.execute ('DELETE FROM media WHERE note=? AND id NOT IN (SELECT media_id FROM cards WHERE media_id IS NOT NULL)', (
				note,))
	result = {'saved': len (validated), 'changed': not old or old[0]['digest'] != digest, 'name': note}
	if not validated: result['warning'] = '没有可保存的知识点卡片'
	return result


def cards ():
	r = rows ('''SELECT c.*, m.path AS media_path, m.annotation AS annotation, m.created AS media_created,
	                 COALESCE(r.attempts, 0) attempts, COALESCE(r.wrong, 0) wrong
                 FROM cards c
                          LEFT JOIN reviews r ON c.id = r.card_id
                          LEFT JOIN media m ON c.media_id = m.id
                 WHERE c.active = 1
				 ORDER BY c.note, c.line, CASE WHEN c.sort_order IS NULL THEN 1 ELSE 0 END,
				          c.sort_order, c.created, c.id''')
	r = [c for c in r if c['kind'] in ('knowledge', 'image')]
	for c in r:
		c['options'] = json.loads (c['options'])
		c['sentence'] = c['original'].replace (c['answer'], '【' + c['answer'] + '】', 1) if c['kind'] == 'cloze' else c[
			'original']
	by_id = {c['id']: c for c in r}
	groups = {}
	for member in rows ('SELECT root_id,member_id FROM card_group_members ORDER BY root_id,position'):
		groups.setdefault (member['root_id'], []).append (member['member_id'])
	hidden = set ()
	for root_id, member_ids in groups.items ():
		if root_id not in by_id: continue
		parts = [by_id[member_id] for member_id in member_ids if member_id in by_id]
		if len (parts) < 2: continue
		by_id[root_id]['merged_parts'] = [{key: part[key] for key in ('id', 'kind', 'section', 'h2', 'original',
			'line', 'media_id', 'media_path', 'media_created', 'annotation')} for part in parts]
		hidden.update (part['id'] for part in parts if part['id'] != root_id)
	return [c for c in r if c['id'] not in hidden]


def save_host_backup ():
	backup_dir = DB.parent / 'backups'
	with LOCK:
		backup_dir.mkdir (parents=True, exist_ok=True)
		backup_path = backup_dir / ('study-' + datetime.now ().strftime ('%Y%m%d-%H%M%S') + '-' + uuid.uuid4 ().hex[:8] + '.sqlite3')
		try:
			with closing (sqlite3.connect (DB)) as source, closing (sqlite3.connect (backup_path)) as destination:
				source.backup (destination)
		except Exception:
			backup_path.unlink (missing_ok=True)
			raise
	return {'path': str (backup_path), 'filename': backup_path.name, 'bytes': backup_path.stat ().st_size}


def import_sqlite_backup (data):
	if not isinstance (data, bytes) or not data.startswith (b'SQLite format 3\x00'):
		raise ValueError ('请选择本系统生成的有效 .sqlite3 备份')
	with tempfile.NamedTemporaryFile (dir=DB.parent, suffix='.sqlite3', delete=False) as upload:
		upload.write (data)
		upload_path = Path (upload.name)
	try:
		with closing (sqlite3.connect (upload_path)) as source:
			if source.execute ('PRAGMA integrity_check').fetchone ()[0] != 'ok':
				raise ValueError ('备份数据库已损坏')
			required = {'notes': {'name', 'content'}, 'cards': {'id', 'kind', 'original', 'media_id'},
			            'media': {'id', 'data'}, 'reviews': {'card_id'}, 'attempts': {'card_id'},
			            'card_group_members': {'root_id', 'member_id'}, 'app_meta': {'key', 'value'},
			            'generation_batches': {'note'}}
			for table, columns in required.items ():
				present = {row[1] for row in source.execute ('PRAGMA table_info('+table+')')}
				if not columns <= present: raise ValueError ('备份不是本系统生成的完整数据库')
			counts = {table: source.execute ('SELECT COUNT(*) FROM '+table).fetchone ()[0]
			          for table in ('notes', 'cards', 'media', 'attempts')}
			with QUEUE_LOCK:
				if QUEUE_RUNNING or (read_import_queue () or {}).get ('pending'):
					raise ValueError ('请等待当前笔记导入完成后再恢复备份')
				with LOCK:
					previous = save_host_backup ()['path']
					try:
						with closing (sqlite3.connect (DB)) as destination:
							source.backup (destination)
					except Exception:
						with closing (sqlite3.connect (previous)) as saved, closing (sqlite3.connect (DB)) as destination:
							saved.backup (destination)
						raise
		return {**counts, 'previous_database': previous}
	finally:
		upload_path.unlink (missing_ok=True)


def pinned_card_id ():
	pin = rows ("SELECT value FROM app_meta WHERE key='pinned_card_id'")
	if not pin: return None
	return pin[0]['value'] if rows ('SELECT 1 FROM cards WHERE id=? AND active=1 AND kind IN (\'knowledge\',\'image\')', (pin[0]['value'],)) else None


def toggle_pin (card_id):
	with LOCK, connect () as db:
		card = db.execute ("SELECT 1 FROM cards WHERE id=? AND active=1 AND kind IN ('knowledge','image')", (card_id,)).fetchone ()
		if not card: raise ValueError ('知识点或图片不存在')
		old = db.execute ("SELECT value FROM app_meta WHERE key='pinned_card_id'").fetchone ()
		if old and old['value'] == card_id:
			db.execute ("DELETE FROM app_meta WHERE key='pinned_card_id'")
			return {'pinned_id': None}
		db.execute ("INSERT INTO app_meta(key,value) VALUES('pinned_card_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (card_id,))
	return {'pinned_id': card_id}


def answer (card_id, selected, mode=None):
	with LOCK, connect () as db:
		card = db.execute ('SELECT * FROM cards WHERE id=? AND active=1', (card_id,)).fetchone ()
		if not card: raise ValueError ('卡片不存在')
		if selected not in json.loads (card['options']): raise ValueError ('无效答案')
		correct = int (selected == card['answer']);
		review = db.execute ('SELECT * FROM reviews WHERE card_id=?', (card_id,)).fetchone ()
		n = int (review['attempts']) if review else 0;
		wrong = int (review['wrong']) if review else 0
		db.execute ('INSERT INTO attempts(card_id,selected,correct,created) VALUES(?,?,?,?)', (card_id, selected,
		                                                                                       correct, now ()))
		db.execute ('''INSERT INTO reviews(card_id, attempts, wrong, last_correct)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(card_id) DO UPDATE SET attempts=excluded.attempts,
                                                          wrong=excluded.wrong,
                                                          last_correct=excluded.last_correct''', (card_id, n + 1,
		                                                                                          wrong + (not correct),
		                                                                                          correct))
		if mode == 'random': db.execute ('UPDATE cards SET random_return_pending=0 WHERE id=?', (card_id,))
		if not correct: db.execute ('UPDATE cards SET wrong_marked=1,wrong_marked_at=COALESCE(wrong_marked_at,?) WHERE id=?', (
			now (), card_id))
		if mode == 'wrong':
			if correct:
				db.execute ('''UPDATE cards SET wrong_marked=0,wrong_marked_at=NULL,favorite=0,favorite_at=NULL,
				              round1_done=0,persistent_unknown=0,retry_deferred=0,random_return_pending=1 WHERE id=?''', (card_id,))
			else: db.execute ('UPDATE cards SET round1_done=1,retry_deferred=0,random_return_pending=0 WHERE id=?', (card_id,))
		if mode == 'retry':
			if correct: db.execute ('UPDATE cards SET round1_done=0,persistent_unknown=0,retry_deferred=0 WHERE id=?', (card_id,))
			else: db.execute ('UPDATE cards SET persistent_unknown=1,retry_deferred=1 WHERE id=?', (card_id,))
	return {'correct': bool (correct), 'correct_answer': card['answer'], **card_update_payload (card_id)}


def release_retry_batch (note=None):
	if note is not None and (not isinstance (note, str) or not note): raise ValueError ('章节无效')
	with LOCK, connect () as db:
		if note is None:
			changed = db.execute ('UPDATE cards SET retry_deferred=0 WHERE retry_deferred=1').rowcount
		else:
			changed = db.execute ('UPDATE cards SET retry_deferred=0 WHERE retry_deferred=1 AND note=?', (note,)).rowcount
	return {'released': changed}


def update_flags (card_id, changes):
	allowed = ('favorite', 'wrong_marked')
	fields = [key for key in allowed if key in changes]
	if not fields or any (
		not isinstance (changes[key], bool) for key in fields): raise ValueError ('标记必须为 true 或 false')
	with LOCK, connect () as db:
		card = db.execute ('SELECT * FROM cards WHERE id=? AND active=1', (card_id,)).fetchone ()
		if not card: raise ValueError ('卡片不存在')
		for key in fields:
			timestamp = key + '_at'
			if changes[key]:
				db.execute (f'UPDATE cards SET {key}=1,{timestamp}=COALESCE({timestamp},?) WHERE id=?', (now (),
				                                                                                         card_id))
			else:
				db.execute (f'UPDATE cards SET {key}=0,{timestamp}=NULL WHERE id=?', (card_id,))
		if changes.get ('wrong_marked') is False: db.execute ('UPDATE cards SET persistent_unknown=0 WHERE id=?', (card_id,))
		updated = db.execute ('SELECT favorite,wrong_marked FROM cards WHERE id=?', (card_id,)).fetchone ()
		if updated['favorite'] or updated['wrong_marked']:
			db.execute ('UPDATE cards SET random_return_pending=0 WHERE id=?', (card_id,))
		elif card['favorite'] or card['wrong_marked']:
			db.execute ('UPDATE cards SET round1_done=0,persistent_unknown=0,retry_deferred=0,random_return_pending=1 WHERE id=?', (card_id,))
	return {'ok': True, **card_update_payload (card_id)}


def update_image_annotation (card_id, annotation):
	if not isinstance (annotation, str) or len (annotation) > 20000:
		raise ValueError ('图片注释必须是 20000 字以内的文本')
	with LOCK, connect () as db:
		card = db.execute ("SELECT media_id FROM cards WHERE id=? AND active=1 AND kind='image'", (card_id,)).fetchone ()
		if not card or not card['media_id']: raise ValueError ('图片卡片不存在')
		result = db.execute ('UPDATE media SET annotation=? WHERE id=?', (annotation.strip (), card['media_id']))
		if result.rowcount != 1: raise ValueError ('图片文件不存在')
	return {'ok': True, **card_update_payload (card_id)}


def card_stats (d):
	return {'total': len (d), 'studied': sum (x['attempts'] > 0 for x in d),
	        'wrong': sum (bool (x['wrong_marked']) for x in d), 'favorites': sum (bool (x['favorite']) for x in d),
	        'notes': len (set (x['note'] for x in d))}


def card_update_payload (card_id):
	items = cards ()
	return {'card': next ((item for item in items if item['id'] == card_id), None),
	        'stats': card_stats (items)}


def stats ():
	return card_stats (cards ())


def materials ():
	examples = [];
	images = []
	for note in rows ('SELECT name,content FROM notes WHERE content IS NOT NULL'):
		examples.extend (example_blocks (note['content'], note['name']))
		images.extend (image_references (note['content'], note['name']))
	stored = {(x['note'], x['path']): x['id'] for x in rows ('SELECT id,note,path FROM media')}
	for item in images: item['id'] = stored.get ((item['note'], item['path']))
	return {'examples': examples, 'images': images}


def knowledge ():
	return [{'id': c['id'], 'note': c['note'], 'h2': c['h2'], 'section': c['section'],
	         'line': c['line'], 'content': c['original']} for c in cards () if c['kind'] == 'knowledge']


def edit_knowledge_card (card_id, content, title=None):
	if not isinstance (content, str) or not content.strip (): raise ValueError ('请输入知识点内容')
	if title is not None and (not isinstance (title, str) or not title.strip () or len (title.strip ()) > 150):
		raise ValueError ('卡片标题不能为空，且不能超过 150 个字')
	with LOCK, connect () as db:
		card = db.execute ("SELECT id,section FROM cards WHERE id=? AND active=1 AND kind='knowledge'", (card_id,)).fetchone ()
		if not card: raise ValueError ('知识点不存在')
		title = title.strip () if title is not None else card['section']
		db.execute ('UPDATE cards SET original=?,section=?,manual=1 WHERE id=?', (content.strip (), title, card_id))
	return {'id': card_id, 'content': content.strip (), 'title': title}


def split_knowledge_card (card_id, original, parts):
	if not isinstance (original, str) or not isinstance (parts, list) or len (parts) < 2:
		raise ValueError ('至少需要放置一个拆分标记')
	if any (not isinstance (part, dict) or not isinstance (part.get ('content'), str) or
	        not part['content'].strip () or not isinstance (part.get ('title'), str) for part in parts):
		raise ValueError ('每段都需要内容和标题')
	with LOCK, connect () as db:
		card = db.execute ("SELECT * FROM cards WHERE id=? AND active=1 AND kind='knowledge'", (card_id,)).fetchone ()
		if not card: raise ValueError ('知识点不存在')
		if card['original'] != original: raise ValueError ('知识点内容已变化，请重新打开编辑弹窗')
		ordered_ids = [row['id'] for row in db.execute ('''SELECT id FROM cards WHERE note=? AND line=? AND active=1
			ORDER BY CASE WHEN sort_order IS NULL THEN 1 ELSE 0 END,sort_order,created,id''',
			(card['note'], card['line']))]
		new_ids = []
		for index, part in enumerate (parts):
			content = part['content'].strip ()
			title = part['title'].strip ()[:150] or card['section']
			if index == 0:
				db.execute ('UPDATE cards SET original=?,section=?,manual=1 WHERE id=?', (content, title, card_id))
			else:
				new_id = 'k' + uuid.uuid4 ().hex
				db.execute ('''INSERT INTO cards(id,note,section,h2,original,answer,options,line,topic,active,created,kind,manual,
				              favorite,wrong_marked,round1_done,persistent_unknown,retry_deferred,random_return_pending,favorite_at,wrong_marked_at)
				              VALUES(?,?,?,?,?,?,?,?,?,1,?,'knowledge',1,?,?,?,?,?,?,?,?)''',
				            (new_id, card['note'], title, card['h2'], content, card['answer'],
				             card['options'], card['line'], card['topic'], now (), card['favorite'],
				             card['wrong_marked'], card['round1_done'], card['persistent_unknown'],
				             card['retry_deferred'], card['random_return_pending'], card['favorite_at'], card['wrong_marked_at']))
				new_ids.append (new_id)
		position = ordered_ids.index (card_id)
		ordered_ids[position+1:position+1] = new_ids
		for order, sibling_id in enumerate (ordered_ids):
			db.execute ('UPDATE cards SET sort_order=? WHERE id=?', (order, sibling_id))
	return {'id': card_id, 'content': parts[0]['content'].strip (), 'new_ids': new_ids}


def merge_card_group_into_previous (card_id, previous_id, kind):
	with LOCK:
		visible = cards ()
		position = next ((i for i, card in enumerate (visible) if card['id'] == card_id), -1)
		if position <= 0 or visible[position - 1]['id'] != previous_id:
			raise ValueError ('上一张卡片已变化，请刷新知识总览后重试')
		current, previous = visible[position], visible[position - 1]
		if (current['kind'] != kind or (kind == 'knowledge' and previous['kind'] != 'knowledge') or
		        current['note'] != previous['note'] or current['h2'] != previous['h2']):
			raise ValueError ('只能合并同一章节内相邻的卡片')
		with connect () as db:
			def members (root_id):
				ids = [row['member_id'] for row in db.execute (
					'SELECT member_id FROM card_group_members WHERE root_id=? ORDER BY position', (root_id,))]
				return ids or [root_id]
			target_ids = members (previous_id)
			source_ids = members (card_id)
			if set (target_ids).intersection (source_ids): raise ValueError ('卡片已合并，请刷新知识总览')
			favorite = int (bool (previous['favorite'] or current['favorite']))
			wrong = int (bool (previous['wrong_marked'] or current['wrong_marked']))
			db.execute ('''UPDATE cards SET favorite=?,wrong_marked=?,favorite_at=?,wrong_marked_at=?,
			              round1_done=?,persistent_unknown=?,retry_deferred=?,random_return_pending=? WHERE id=?''', (
				favorite, wrong,
				min (filter (None, (previous['favorite_at'], current['favorite_at'])), default=None) if favorite else None,
				min (filter (None, (previous['wrong_marked_at'], current['wrong_marked_at'])), default=None) if wrong else None,
				int (bool (previous['round1_done'] or current['round1_done'])),
				int (bool (previous['persistent_unknown'] or current['persistent_unknown'])),
				int (bool (previous['retry_deferred'] or current['retry_deferred']) and bool (favorite or wrong)),
				int (not (favorite or wrong) and bool (previous['attempts'] == 0 or current['attempts'] == 0 or
				                                    previous['random_return_pending'] or current['random_return_pending'])),
				previous_id))
			reviews = db.execute ('SELECT attempts,wrong,last_correct FROM reviews WHERE card_id IN (?,?)',
			                      (previous_id, card_id)).fetchall ()
			if reviews:
				last = db.execute ('SELECT correct FROM attempts WHERE card_id IN (?,?) ORDER BY created DESC,id DESC LIMIT 1',
				                   (previous_id, card_id)).fetchone ()
				db.execute ('''INSERT INTO reviews(card_id,attempts,wrong,last_correct) VALUES(?,?,?,?)
				              ON CONFLICT(card_id) DO UPDATE SET attempts=excluded.attempts,
				              wrong=excluded.wrong,last_correct=excluded.last_correct''', (
					previous_id, sum (row['attempts'] for row in reviews), sum (row['wrong'] for row in reviews),
					last['correct'] if last else reviews[-1]['last_correct']))
				db.execute ('DELETE FROM reviews WHERE card_id=?', (card_id,))
			db.execute ('UPDATE attempts SET card_id=? WHERE card_id=?', (previous_id, card_id))
			db.execute ("UPDATE app_meta SET value=? WHERE key='pinned_card_id' AND value=?", (previous_id, card_id))
			db.execute ('DELETE FROM card_group_members WHERE root_id IN (?,?)', (previous_id, card_id))
			for order, member_id in enumerate (target_ids + source_ids):
				db.execute ('INSERT INTO card_group_members(root_id,member_id,position) VALUES(?,?,?)',
				            (previous_id, member_id, order))
	return {'id': previous_id, 'merged_id': card_id}


def merge_image_into_previous (card_id, previous_id):
	return merge_card_group_into_previous (card_id, previous_id, 'image')


def unmerge_card_group (root_id):
	with LOCK, connect () as db:
		members = db.execute ('SELECT member_id FROM card_group_members WHERE root_id=?', (root_id,)).fetchall ()
		if len (members) < 2: raise ValueError ('这张卡片没有可取消的合并')
		db.execute ('DELETE FROM card_group_members WHERE root_id=?', (root_id,))
	return {'id': root_id, 'restored': len (members) - 1}


def edit_card_group (root_id, parts):
	if not isinstance (parts, list): raise ValueError ('组合卡片内容无效')
	with LOCK, connect () as db:
		members = db.execute ('''SELECT g.member_id,c.kind,c.media_id FROM card_group_members g
			JOIN cards c ON c.id=g.member_id WHERE g.root_id=? AND c.active=1 ORDER BY g.position''',
			(root_id,)).fetchall ()
		if len (members) < 2 or len (members) != len (parts) or any (
			not isinstance (part, dict) or part.get ('id') != member['member_id']
			for member, part in zip (members, parts)):
			raise ValueError ('组合卡片已变化，请刷新知识总览后重试')
		for member, part in zip (members, parts):
			if member['kind'] == 'knowledge':
				content, title = part.get ('content'), part.get ('title')
				if not isinstance (content, str) or not content.strip ():
					raise ValueError ('每段文字都需要内容')
				if not isinstance (title, str) or not title.strip () or len (title.strip ()) > 150:
					raise ValueError ('文字标题不能为空，且不能超过 150 个字')
				db.execute ('UPDATE cards SET original=?,section=?,manual=1 WHERE id=?',
				            (content.strip (), title.strip (), member['member_id']))
			elif member['kind'] == 'image':
				annotation = part.get ('annotation')
				if not isinstance (annotation, str) or len (annotation) > 20000:
					raise ValueError ('图片注释必须是 20000 字以内的文本')
				result = db.execute ('UPDATE media SET annotation=? WHERE id=?',
				                     (annotation.strip (), member['media_id']))
				if result.rowcount != 1: raise ValueError ('图片文件不存在')
			else: raise ValueError ('组合卡片类型已变化')
	return {'id': root_id, 'updated': len (parts)}


def merge_knowledge_into_previous (card_id, previous_id):
	with LOCK:
		grouped = rows ('SELECT 1 FROM card_group_members WHERE root_id IN (?,?) LIMIT 1',
		                (card_id, previous_id))
		if grouped: return merge_card_group_into_previous (card_id, previous_id, 'knowledge')
	with LOCK, connect () as db:
		current = db.execute ("SELECT * FROM cards WHERE id=? AND active=1 AND kind='knowledge'", (card_id,)).fetchone ()
		if not current: raise ValueError ('知识点不存在')
		siblings = db.execute ('''SELECT * FROM cards WHERE note=? AND h2=? AND active=1 AND kind='knowledge'
		                         ORDER BY line,created,id''', (current['note'], current['h2'])).fetchall ()
		position = next ((i for i, item in enumerate (siblings) if item['id'] == card_id), -1)
		if position <= 0 or siblings[position - 1]['id'] != previous_id:
			raise ValueError ('上一条知识点已变化，请刷新总览后重试')
		previous = siblings[position - 1]
		merged = previous['original'].rstrip () + '\n\n' + current['original'].lstrip ()
		old_reviews = db.execute ('SELECT * FROM reviews WHERE card_id IN (?,?)', (previous_id, card_id)).fetchall ()
		attempts_by_id = {item['card_id']: item['attempts'] for item in old_reviews}
		favorite = int (bool (previous['favorite'] or current['favorite']))
		wrong = int (bool (previous['wrong_marked'] or current['wrong_marked']))
		db.execute ('''UPDATE cards SET original=?,manual=1,favorite=?,wrong_marked=?,
		              favorite_at=?,wrong_marked_at=?,round1_done=?,persistent_unknown=?,retry_deferred=?,random_return_pending=? WHERE id=?''', (
			merged, favorite, wrong,
			min (filter (None, (previous['favorite_at'], current['favorite_at'])), default=None) if favorite else None,
			min (filter (None, (previous['wrong_marked_at'], current['wrong_marked_at'])), default=None) if wrong else None,
			int (bool (previous['round1_done'] or current['round1_done'])),
			int (bool (previous['persistent_unknown'] or current['persistent_unknown'])),
			int (bool (previous['retry_deferred'] or current['retry_deferred']) and bool (favorite or wrong)),
			int (not (favorite or wrong) and bool (not attempts_by_id.get (previous_id) or not attempts_by_id.get (card_id) or
			                                    previous['random_return_pending'] or current['random_return_pending'])), previous_id))
		if old_reviews:
			attempts = sum (item['attempts'] for item in old_reviews)
			mistakes = sum (item['wrong'] for item in old_reviews)
			last_attempt = db.execute ('''SELECT correct FROM attempts WHERE card_id IN (?,?)
			                           ORDER BY created DESC,id DESC LIMIT 1''', (previous_id, card_id)).fetchone ()
			last_correct = last_attempt['correct'] if last_attempt else old_reviews[-1]['last_correct']
			db.execute ('''INSERT INTO reviews(card_id,attempts,wrong,last_correct) VALUES(?,?,?,?)
			              ON CONFLICT(card_id) DO UPDATE SET attempts=excluded.attempts,
			              wrong=excluded.wrong,last_correct=excluded.last_correct''',
			            (previous_id, attempts, mistakes, last_correct))
		db.execute ('UPDATE attempts SET card_id=? WHERE card_id=?', (previous_id, card_id))
		db.execute ('DELETE FROM reviews WHERE card_id=?', (card_id,))
		db.execute ('DELETE FROM cards WHERE id=?', (card_id,))
	return {'id': previous_id, 'content': merged, 'merged_id': card_id}


def edit_knowledge (note, line, new_content):
	if type (line) is not int or line < 1 or not isinstance (new_content, str) or not new_content.strip () or '\n' in new_content or '\r' in new_content: raise ValueError ('请输入单行知识点内容')
	with LOCK, connect () as db:
		source = db.execute ('SELECT content FROM notes WHERE name=?', (note,)).fetchone ()
		if not source or not source['content']: raise ValueError ('找不到保存的笔记原文')
		lines = source['content'].splitlines (keepends=True)
		if line > len (lines) or not any (
			x['line'] == line for x in source_rows (source['content'], note)): raise ValueError ('知识点不存在')
		suffix = '\n' if lines[line - 1].endswith ('\n') else ''
		lines[line - 1] = new_content.strip () + suffix
		content = ''.join (lines)
		db.execute ('UPDATE notes SET content=?,digest=?,updated=? WHERE name=?', (content,
		                                                                           hashlib.sha256 (content.encode ()).hexdigest (),
		                                                                           now (), note))
	return {'note': note, 'line': line, 'content': new_content.strip ()}


def delete_knowledge_item (note, line):
	if type (line) is not int or line < 1: raise ValueError ('知识点行号无效')
	with LOCK, connect () as db:
		source = db.execute ('SELECT content FROM notes WHERE name=?', (note,)).fetchone ()
		if not source or not source['content']: raise ValueError ('找不到保存的笔记原文')
		lines = source['content'].splitlines (keepends=True)
		if line > len (lines) or not any (
			x['line'] == line for x in source_rows (source['content'], note)): raise ValueError ('知识点不存在')
		lines[line - 1] = '\n' if lines[line - 1].endswith ('\n') else ''
		content = ''.join (lines)
		db.execute ('UPDATE notes SET content=?,digest=?,updated=? WHERE name=?', (content,
		                                                                           hashlib.sha256 (content.encode ()).hexdigest (),
		                                                                           now (), note))
	return {'deleted': 1}


def choose_notes ():
	result = subprocess.run ([sys.executable,
	                          str (ROOT / 'scripts' / 'choose_notes.py')], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
	if result.returncode: raise ValueError (result.stderr.strip () or '无法打开本地文件选择窗口')
	chosen = [Path (p).resolve () for p in json.loads (result.stdout or '[]')]
	paths = [p for p in chosen if p.suffix.lower () in ('.md', '.markdown')]
	ignored = len (chosen) - len (paths)
	if not paths: return {'token': None, 'files': [], 'documents': [], 'ignored': ignored}
	if len ({p.name.casefold () for p in
	         paths}) != len (paths): raise ValueError ('所选文件中有同名笔记，请分别导入或先重命名')
	if any (not p.is_file () for p in paths): raise ValueError ('所选 Markdown 文件不存在')
	token = uuid.uuid4 ().hex;
	PICKS[token] = (time.time (), paths)
	return {'token': token, 'files': [p.name for p in paths], 'ignored': ignored,
	        'documents': [{'name': p.name, 'sections': h2_ranges (p.read_text (encoding='utf-8-sig'))} for p in paths]}


def selected_files (token):
	item = PICKS.get (token)
	if not item or time.time () - item[0] > 3600: raise ValueError ('文件选择已过期，请重新选择笔记')
	return item[1]


def material_cards (markdown, note, path=None, line_range=None):
	cards = [];
	media = [];
	warnings = []
	
	def included (line):
		return line_range is None or line_range[0] <= line <= line_range[1]
	
	for ref in image_references (markdown, note):
		if not included (ref['line']): continue
		raw = unquote (urlparse (ref['path']).path).replace ('\\', '/')
		if not path:
			stored = rows ('SELECT id FROM media WHERE note=? AND path=?', (note, ref['path']))
			if stored:
				cards.append ({'kind': 'image', 'identity': 'image:' + ref['path'], 'original': ref['title'],
			               'answer': '了解', 'options': ['了解', '不了解'], 'section': ref['title'],
				               'h2': ref['h2'], 'line': ref['line'], 'media_id': stored[0]['id']})
			else:
				warnings.append (f"{ref['path']}：未保存图片，请重新上传包含同级 img 文件夹的笔记")
			continue
		target = (path.parent / raw).resolve ()
		if not target.is_relative_to (path.parent.resolve ()) or target.suffix.lower () not in ('.png', '.jpg', '.jpeg',
		                                                                                        '.gif', '.webp',
		                                                                                        '.svg'):
			warnings.append (f"{ref['path']}：图片路径不安全或格式不支持");
			continue
		if not target.is_file (): warnings.append (f"{ref['path']}：文件不存在");continue
		if target.stat ().st_size > 8 * 1024 * 1024: warnings.append (f"{ref['path']}：超过 8 MB");continue
		mime = mimetypes.guess_type (target.name)[0] or ''
		if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'):
			warnings.append (f"{ref['path']}：不支持的图片格式");
			continue
		mid = 'm' + hashlib.sha256 ((note + '\0' + ref['path']).encode ()).hexdigest ()[:24]
		media.append ((mid, note, ref['path'], ref['title'], mime, target.read_bytes (), now ()))
		cards.append ({'kind': 'image', 'identity': 'image:' + ref['path'], 'original': ref['title'], 'answer': '了解',
		               'options': ['了解', '不了解'], 'section': ref['title'], 'h2': ref['h2'], 'line': ref['line'],
		               'media_id': mid})
	return cards, media, warnings


def backfill_local_materials ():
	for item in rows ("SELECT name,content FROM notes WHERE content IS NOT NULL AND content<>''"):
		note = item['name'];
		markdown = item['content']
		if not image_references (markdown, note) and not example_blocks (markdown, note): continue
		path = resolve_note_path (note, markdown)
		if not path: continue
		extras, media, _ = material_cards (markdown, note, path)
		if extras: save_cards (note, markdown, extras, replace=False, media_items=media, source_path=path)


def import_media (note, filename, mime, data, relative_path=None):
	if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp',
	                'image/svg+xml'): raise ValueError ('仅支持 PNG、JPEG、GIF、WebP 或 SVG 图片')
	try:
		blob = base64.b64decode (data, validate=True)
	except Exception as ex:
		raise ValueError ('图片数据无效') from ex
	if not blob or len (blob) > 8 * 1024 * 1024: raise ValueError ('单张图片必须在 8 MB 以内')
	source = rows ('SELECT content FROM notes WHERE name=?', (note,))
	if not source or not source[0]['content']: raise ValueError ('请先导入包含图片引用的 Markdown')
	filename = Path (filename).name
	
	def normalized (path):
		return unquote (urlparse (path).path).replace ('\\', '/').lstrip ('./').casefold ()
	
	candidate = normalized (relative_path) if relative_path else ''
	refs = []
	for ref in image_references (source[0]['content'], note):
		target = normalized (ref['path'])
		if candidate:
			if target == candidate or candidate.endswith ('/' + target): refs.append (ref)
		elif Path (target).name.casefold () == filename.casefold ():
			refs.append (ref)
	if not refs: raise ValueError ('笔记中没有引用这张图片：' + filename)
	with LOCK, connect () as db:
		for ref in refs:
			mid = 'm' + hashlib.sha256 ((note + '\0' + ref['path']).encode ()).hexdigest ()[:24]
			db.execute ('''INSERT INTO media(id, note, path, title, mime, data, created)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                                                         mime=excluded.mime,
                                                         data=excluded.data,
                                                         created=excluded.created''', (mid, note, ref['path'],
			                                                                           ref['title'], mime, blob,
			                                                                           now ()))
	return {'saved': len (refs)}


def valid_svg (blob):
	try:
		source = blob.decode ('utf-8-sig')
		if re.search (r'<!\s*(?:DOCTYPE|ENTITY)\b', source, re.I): return False
		root = ET.fromstring (source)
	except (UnicodeDecodeError, ET.ParseError): return False
	local = lambda name: name.rsplit ('}', 1)[-1].lower ()
	if local (root.tag) != 'svg': return False
	for element in root.iter ():
		if local (element.tag) in ('script', 'foreignobject', 'iframe', 'object', 'embed'):
			return False
		for key, value in element.attrib.items ():
			name = local (key)
			if name.startswith ('on'): return False
			if name in ('href', 'src') and not value.strip ().startswith ('#'): return False
			if any (not ref.strip ().strip ('\'"').startswith ('#')
			        for ref in re.findall (r'url\s*\(([^)]*)\)', value, re.I)):
				return False
	return True


def add_manual_image (note, title, filename, mime, data, annotation=''):
	if not isinstance (note, str) or not note.lower ().endswith (('.md', '.markdown')):
		raise ValueError ('请选择有效章节')
	if not isinstance (title, str) or not title.strip () or len (title.strip ()) > 150:
		raise ValueError ('请输入 150 字以内的二级标题')
	if not isinstance (annotation, str) or len (annotation) > 20000:
		raise ValueError ('图片注释必须在 20000 字以内')
	if not isinstance (filename, str) or not filename.strip ():
		raise ValueError ('请选择图片文件')
	mime = mime or mimetypes.guess_type (filename)[0]
	if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'):
		raise ValueError ('仅支持 PNG、JPEG、GIF、WebP 或 SVG 图片')
	if not isinstance (data, str): raise ValueError ('图片数据无效')
	try:
		blob = base64.b64decode (data, validate=True)
	except Exception as ex:
		raise ValueError ('图片数据无效') from ex
	if not blob or len (blob) > 8 * 1024 * 1024: raise ValueError ('单张图片必须在 8 MB 以内')
	valid = (mime == 'image/png' and blob.startswith (b'\x89PNG\r\n\x1a\n') or
	         mime == 'image/jpeg' and blob.startswith (b'\xff\xd8\xff') or
	         mime == 'image/gif' and blob.startswith ((b'GIF87a', b'GIF89a')) or
	         mime == 'image/webp' and len (blob) >= 12 and blob[:4] == b'RIFF' and blob[8:12] == b'WEBP' or
	         mime == 'image/svg+xml' and valid_svg (blob))
	if not valid: raise ValueError ('图片内容与格式不符')
	title = title.strip ()
	card_id = 'i' + uuid.uuid4 ().hex
	media_id = 'm' + uuid.uuid4 ().hex
	created = now ()
	path = 'manual/' + media_id + '/' + Path (filename).name
	with LOCK, connect () as db:
		if not db.execute ('SELECT 1 FROM notes WHERE name=?', (note,)).fetchone ():
			raise ValueError ('章节不存在，请先导入笔记')
		db.execute ('''INSERT INTO media(id,note,path,title,mime,data,annotation,created)
		              VALUES (?,?,?,?,?,?,?,?)''', (media_id, note, path, title, mime, blob, annotation.strip (), created))
		db.execute ('''INSERT INTO cards(id,note,section,original,answer,options,line,topic,active,created,kind,media_id,h2,manual)
		              VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,1)''', (card_id, note, title, title, '了解',
		                                                    json.dumps (['了解', '不了解'], ensure_ascii=False), 0,
		                                                    't' + uuid.uuid4 ().hex, created, 'image', media_id, title))
	return {'saved': 1, 'id': card_id}


def deletion_ids (note, scope_start=None, markdown=None):
	existing = rows ('SELECT content,digest FROM notes WHERE name=?', (note,))
	if not existing: raise ValueError ('章节不存在')
	if scope_start is None: return [x['id'] for x in rows ('SELECT id FROM cards WHERE note=?', (note,))]
	content = existing[0]['content'] or markdown
	if not content or type (scope_start) is not int: raise ValueError ('请先选择有效的二级标题或同名源 Markdown')
	if markdown and hashlib.sha256 (markdown.encode ()).hexdigest () != existing[0][
		'digest']: raise ValueError ('源 Markdown 已变化，请先整章更新后再按标题删除')
	scopes = {x['start']: x for x in h2_ranges (content)}
	if scope_start not in scopes: raise ValueError ('二级标题不存在，请重新选择')
	scope = scopes[scope_start]
	return [x['id'] for x in
	        rows ('SELECT id FROM cards WHERE note=? AND line>=? AND line<=?', (note, scope['start'], scope['end']))]


def deletion_preview (note, scope_start=None, markdown=None):
	ids = deletion_ids (note, scope_start, markdown)
	active = {x['id'] for x in rows ('SELECT id FROM cards WHERE note=? AND active=1', (note,))}
	return {'count': len (active.intersection (ids)), 'total': len (ids)}


def remove_card_ids (db, ids):
	selected = set (ids)
	root_ids = list (selected)
	for start in range (0, len (root_ids), 400):
		batch = root_ids[start:start + 400]
		placeholders = ','.join ('?' for _ in batch)
		selected.update (row['member_id'] for row in db.execute (
			'SELECT member_id FROM card_group_members WHERE root_id IN (' + placeholders + ')', batch))
	ids = list (selected)
	for start in range (0, len (ids), 400):
		batch = ids[start:start + 400]
		placeholders = ','.join ('?' for _ in batch)
		db.execute ('DELETE FROM card_group_members WHERE member_id IN (' + placeholders + ') OR root_id IN (' + placeholders + ')', batch + batch)
		db.execute ("DELETE FROM app_meta WHERE key='pinned_card_id' AND value IN (" + placeholders + ')', batch)
		db.execute ('DELETE FROM attempts WHERE card_id IN (' + placeholders + ')', batch)
		db.execute ('DELETE FROM reviews WHERE card_id IN (' + placeholders + ')', batch)
		db.execute ('DELETE FROM cards WHERE id IN (' + placeholders + ')', batch)


def delete_cards (note, scope_start=None, markdown=None):
	preview = deletion_preview (note, scope_start, markdown)
	ids = deletion_ids (note, scope_start, markdown)
	with LOCK, connect () as db:
		remove_card_ids (db, ids)
	return {'deleted': preview['count'], 'removed_records': len (ids)}


def delete_item (card_id, expected_kind=None):
	with LOCK, connect () as db:
		row = db.execute ('SELECT kind,media_id FROM cards WHERE id=? AND active=1', (card_id,)).fetchone ()
		if not row: raise ValueError ('学习卡片不存在')
		if expected_kind is not None and row['kind'] != expected_kind: raise ValueError ('卡片类型已变化，请刷新后重试')
		member_ids = [item['member_id'] for item in db.execute (
			'SELECT member_id FROM card_group_members WHERE root_id=?', (card_id,))] or [card_id]
		media_ids = [item['media_id'] for item in db.execute (
			'SELECT media_id FROM cards WHERE id IN (' + ','.join ('?' for _ in member_ids) + ') AND media_id IS NOT NULL', member_ids)]
		remove_card_ids (db, [card_id])
		for media_id in media_ids:
			db.execute ('DELETE FROM media WHERE id=? AND NOT EXISTS(SELECT 1 FROM cards WHERE media_id=?)', (
				media_id, media_id))
	return {'deleted': 1}


def delete_targets (targets, mode='questions'):
	if mode not in ('knowledge', 'questions', 'both'): raise ValueError ('删除类型无效')
	if not isinstance (targets, list) or not targets: raise ValueError ('请选择至少一个章节或二级标题')
	grouped = {}
	for target in targets:
		if not isinstance (target, dict) or not target.get ('note'): raise ValueError ('章节不能为空')
		grouped.setdefault (target['note'], []).append (target)
	deleted_cards = 0;
	deleted_knowledge = 0
	with LOCK, connect () as db:
		for note, items in grouped.items ():
			source = db.execute ('SELECT content FROM notes WHERE name=?', (note,)).fetchone ()
			if not source: raise ValueError ('章节不存在：' + note)
			content = source['content'] or ''
			scopes = {s['start']: s for s in h2_ranges (content)}
			whole = any (t.get ('scope_start') is None for t in items)
			selected = []
			if not whole:
				for target in items:
					start = target.get ('scope_start')
					scope = scopes.get (start)
					if not scope and start is not None and target.get ('scope_title') and db.execute ('SELECT 1 FROM cards WHERE note=? AND h2=?', (
							note, target['scope_title'])).fetchone ():
						scope = {'start': start, 'end': -1, 'title': target['scope_title']}
					if not scope or (target.get ('scope_title') and target['scope_title'] != scope[
						'title']): raise ValueError ('二级标题已变化，请刷新范围')
					selected.append (scope)
			if mode in ('questions', 'both'):
				if whole:
					ids = [r['id'] for r in db.execute ('SELECT id FROM cards WHERE note=?', (note,))]
				else:
					ids = list ({r['id'] for scope in selected for r in
					             db.execute ("SELECT id FROM cards WHERE note=? AND (h2=? OR ((h2='' OR h2 IS NULL) AND line>=? AND line<=?))", (
						             note, scope['title'], scope['start'], scope['end']))})
				deleted_cards += len (ids);
				remove_card_ids (db, ids)
			if mode in ('knowledge', 'both'):
				if whole:
					deleted_knowledge += len (source_rows (content, note))
					if mode == 'both':
						db.execute ('DELETE FROM notes WHERE name=?', (note,))
					else:
						db.execute ('UPDATE notes SET content=?,digest=?,updated=? WHERE name=?', ('',
						                                                                           hashlib.sha256 (b'').hexdigest (),
						                                                                           now (), note))
				elif content:
					lines = content.splitlines (keepends=True)
					for scope in selected:
						if scope['start'] < 1: continue
						deleted_knowledge += sum (
							scope['start'] <= r['line'] <= scope['end'] for r in source_rows (content, note))
						begin = scope['start'] - 1 if mode == 'both' else scope['start']
						for i in range (begin, scope['end']): lines[i] = '\n' if lines[i].endswith ('\n') else ''
					updated = ''.join (lines)
					db.execute ('UPDATE notes SET content=?,digest=?,updated=? WHERE name=?', (updated,
					                                                                           hashlib.sha256 (updated.encode ()).hexdigest (),
					                                                                           now (), note))
			if mode in ('knowledge', 'both'):
				db.execute ('DELETE FROM media WHERE note=? AND id NOT IN (SELECT media_id FROM cards WHERE media_id IS NOT NULL)', (
					note,))
	return {'deleted': deleted_cards, 'deleted_cards': deleted_cards, 'deleted_knowledge': deleted_knowledge}


def start_generation (name, markdown, model, skip_unchanged, scope_start=None, source_path=None, hard_replace=False, scope_title=None, checkpoint=False):
	if not isinstance (markdown, str) or not markdown.strip (): raise ValueError ('空笔记')
	processor = CHECKPOINT_VERSION + ':' + (model or 'rules')
	source_path = source_path or resolve_note_path (name, markdown)
	scope_range = None
	if scope_start is not None:
		scopes = {x['start']: x for x in h2_ranges (markdown)}
		if scope_title and (scope_start not in scopes or scopes[scope_start]['title'] != scope_title):
			scope_start = next ((start for start, item in scopes.items () if item['title'] == scope_title), None)
		if type (scope_start) is not int or scope_start not in scopes: raise ValueError ('二级标题不存在，请重新选择')
		scope_range = (scopes[scope_start]['start'], scopes[scope_start]['end'])
		existing = rows ('SELECT digest FROM notes WHERE name=?', (name,))
		if existing and existing[0][
			'digest'] != hashlib.sha256 (markdown.encode ()).hexdigest () and not source_path: raise ValueError ('笔记内容已变化，请重新上传源文件')
	require_image_source (name, markdown, source_path, scope_range)
	if skip_unchanged:
		existing = rows ('SELECT digest,processor FROM notes WHERE name=?', (name,))
		has_questions = bool (rows ("SELECT id FROM cards WHERE note=? AND active=1 AND kind='knowledge' LIMIT 1", (name,)))
		if existing and has_questions and existing[0]['digest'] == hashlib.sha256 (markdown.encode ()).hexdigest () and existing[0]['processor'] == processor:
			extras, media, warnings = material_cards (markdown, name, source_path)
			if name.lower ().endswith (('.md', '.markdown')):
				with LOCK, connect () as db:
					db.execute ('UPDATE notes SET content=? WHERE name=? AND content IS NULL', (markdown, name))
					fill_h2 (db, name, markdown)
			if extras: save_cards (name, markdown, extras, replace=False, media_items=media, source_path=source_path)
			if checkpoint: clear_generation_batches (checkpoint_key (name, markdown, model))
			return {'skipped': True, 'message': '笔记内容无变化，直接使用已保存的知识点', 'materials': len (extras),
			        'images': sum (x['kind'] == 'image' for x in extras),
			        'examples': sum (x['kind'] == 'example' for x in extras), 'warnings': warnings}
	job_id = uuid.uuid4 ().hex
	with JOB_LOCK:
		for key in list (JOBS):
			if JOBS[key]['done'] and time.time () - JOBS[key]['updated'] > 3600: del JOBS[key]
		JOBS[job_id] = {'done': False, 'error': None, 'result': None, 'processed_rows': 0, 'total_rows': 0,
		                'completed_batches': 0, 'total_batches': 0, 'generated_cards': 0, 'rejected': 0,
		                'updated': time.time ()}
	
	def progress (processed, total, batches, total_batches, generated, rejected):
		with JOB_LOCK: JOBS[
			job_id].update (processed_rows=processed, total_rows=total, completed_batches=batches, total_batches=total_batches, generated_cards=generated, rejected=rejected, updated=time.time ())
	
	def work ():
		try:
			key = checkpoint_key (name, markdown, model) if checkpoint else None
			saved = load_generation_batches (name, markdown, model) if checkpoint else None
			kwargs = {'saved_batches': saved,
			          'on_batch': lambda batch_no, cards, rejected: save_generation_batch (key, batch_no, cards, rejected)} if checkpoint else {}
			out = generate (markdown, name, model, max_rows=100000, progress=progress, line_range=scope_range, **kwargs)
			extras, media, warnings = material_cards (markdown, name, source_path, scope_range)
			result = save_cards (name, markdown, out[
			                                     'cards'] + extras, scope_range=scope_range, hard_replace=hard_replace, media_items=media, source_path=source_path, processor=processor if scope_range is None else None);
			result.update ({'scanned_rows': out['scanned_rows'], 'rejected': out['rejected'],
			                'resumed_batches': out.get ('resumed_batches', 0), 'materials': len (extras),
			                'images': sum (x['kind'] == 'image' for x in extras),
			                'examples': sum (x['kind'] == 'example' for x in extras), 'warnings': warnings + out.get ('warnings', [])})
			if checkpoint: clear_generation_batches (key)
			with JOB_LOCK:
				JOBS[job_id].update (done=True, result=result, updated=time.time ())
		except Exception as ex:
			with JOB_LOCK:
				JOBS[job_id].update (done=True, error=str (ex), updated=time.time ())
	
	threading.Thread (target=work, daemon=True).start ()
	return {'job_id': job_id}


def ingest_plan (note, incoming, title, action, write_knowledge):
	if action not in ('append', 'replace'): raise ValueError ('请选择覆盖或追加')
	old = rows ('SELECT content FROM notes WHERE name=?', (note,))
	prior = old[0]['content'] or '' if old else ''
	incoming_lines = incoming.splitlines ();
	old_lines = prior.splitlines ()
	selected = next ((s for s in h2_ranges (incoming) if s['title'] == title), None) if title else None
	if title and not selected: raise ValueError ('上传文件中没有所选二级标题')
	source_range = (selected['start'], selected['end']) if selected else None
	if not write_knowledge: return prior, source_range, 0, None
	if not title:
		if action == 'replace' or not old_lines: return incoming, source_range, 0, None
		spacer = [''] if old_lines[-1].strip () else []
		merged = old_lines + spacer + incoming_lines
		return '\n'.join (merged) + '\n', None, len (old_lines) + len (spacer), None
	fragment = incoming_lines[selected['start'] - 1:selected['end']]
	prior_scope = next ((s for s in h2_ranges (prior) if s['title'] == title), None)
	if not prior_scope:
		spacer = [''] if old_lines and old_lines[-1].strip () else []
		start = len (old_lines) + len (spacer) + 1
		merged = old_lines + spacer + fragment
		return '\n'.join (merged) + '\n', source_range, start - selected['start'], None
	if action == 'replace':
		begin = prior_scope['start'] - 1;
		end = prior_scope['end']
		merged = old_lines[:begin] + fragment + old_lines[end:]
		shift = (end + 1, len (fragment) - (end - begin))
		return '\n'.join (merged) + '\n', source_range, prior_scope['start'] - selected['start'], shift
	body = incoming_lines[selected['start']:selected['end']]
	insert_at = prior_scope['end']
	merged = old_lines[:insert_at] + body + old_lines[insert_at:]
	return '\n'.join (merged) + '\n', source_range, insert_at - selected['start'], (insert_at + 1, len (body))


def start_ingest (note, markdown, model, title=None, action='append', write_knowledge=True, write_questions=True, source_path=None, target_line=None, analyze=False):
	if not isinstance (markdown, str) or not markdown.strip (): raise ValueError ('请输入或上传笔记内容')
	if not note.lower ().endswith (('.md', '.markdown')): raise ValueError ('章节名称需以 .md 或 .markdown 结尾')
	if not write_knowledge and not write_questions: raise ValueError ('至少选择保存笔记或整理知识点卡片')
	source_path = source_path or resolve_note_path (note, markdown)
	if title and not any (
		s['title'] == title for s in h2_ranges (markdown)): raise ValueError ('输入内容中没有指定二级标题')
	selected_scope = next ((s for s in h2_ranges (markdown) if s['title'] == title), None) if title else None
	require_image_source (note, markdown, source_path, (selected_scope['start'],
	                                                    selected_scope['end']) if selected_scope else None)
	if analyze: raise ValueError ('知识点整理保留笔记原文，不再使用 AI 改写内容')
	job_id = uuid.uuid4 ().hex
	with JOB_LOCK:
		JOBS[job_id] = {'done': False, 'error': None, 'result': None, 'phase': '准备处理', 'processed_rows': 0,
		                'total_rows': 0, 'completed_batches': 0, 'total_batches': 0, 'generated_cards': 0,
		                'rejected': 0, 'updated': time.time ()}
	
	def progress (processed, total, batches, total_batches, generated, rejected):
		with JOB_LOCK: JOBS[
			job_id].update (processed_rows=processed, total_rows=total, completed_batches=batches, total_batches=total_batches, generated_cards=generated, rejected=rejected, updated=time.time ())
	
	def work ():
		try:
			prepared = markdown
			merged, source_range, offset, line_shift = ingest_plan (note, prepared, title, action, write_knowledge)
			if write_questions:
				with JOB_LOCK:
					JOBS[job_id].update (phase='正在整理知识点', updated=time.time ())
				out = generate (prepared, note, model, max_rows=100000, progress=progress, line_range=source_range)
				extras, media, warnings = material_cards (prepared, note, source_path, source_range)
				generated = out['cards'] + extras
				for item in generated:
					item['line'] = int (item.get ('line') or 0) + offset if write_knowledge else (target_line or 0)
					if title: item['h2'] = title
				result = save_cards (note, merged, generated, replace=action == 'replace', hard_replace=action == 'replace', store_source=write_knowledge, media_items=media, line_shift=line_shift, replace_title=title if action == 'replace' else None, source_path=source_path)
				result.update ({'scanned_rows': out['scanned_rows'], 'rejected': out['rejected'],
				                'materials': len (extras), 'images': sum (x['kind'] == 'image' for x in extras),
				                'examples': sum (x['kind'] == 'example' for x in extras), 'warnings': warnings})
			else:
				result = save_cards (note, merged, [], replace=False, line_shift=line_shift, source_path=source_path)
				result.pop ('warning', None)
			result['knowledge_saved'] = write_knowledge
			with JOB_LOCK:
				JOBS[job_id].update (done=True, result=result, updated=time.time ())
		except Exception as ex:
			with JOB_LOCK:
				JOBS[job_id].update (done=True, error=str (ex), updated=time.time ())
	
	threading.Thread (target=work, daemon=True).start ()
	return {'job_id': job_id}


class QuietHTTPServer (ThreadingHTTPServer):
	def handle_error (self, request, client_address):
		# Browsers and local proxies may close image requests before the response finishes.
		if isinstance (sys.exc_info ()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
			return
		super ().handle_error (request, client_address)


class Handler (SimpleHTTPRequestHandler):
	def __init__ (self, *a, **kw):
		super ().__init__ (*a, directory=str (WEB), **kw)
	
	def log_message (self, *args):
		pass
	
	def sendj (self, code, obj):
		b = json.dumps (obj, ensure_ascii=False).encode ();
		compressed = len (b) >= 1024 and 'gzip' in self.headers.get ('Accept-Encoding', '').lower ()
		if compressed: b = gzip.compress (b, compresslevel=5)
		self.send_response (code);
		self.send_header ('Content-Type', 'application/json; charset=utf-8');
		if compressed: self.send_header ('Content-Encoding', 'gzip')
		self.send_header ('Vary', 'Accept-Encoding');
		self.send_header ('Cache-Control', 'no-store');
		self.send_header ('Content-Length', str (len (b)));
		self.end_headers ();
		self.wfile.write (b)
	
	def body (self):
		l = int (self.headers.get ('Content-Length', '0'))
		limit = 16
		if l <= 0 or l > limit * 1024 * 1024: raise ValueError ('单次请求必须在 '+str (limit)+' MB 以内')
		return json.loads (self.rfile.read (l))

	def sqlite_body (self):
		if self.headers.get ('Content-Type', '').split (';', 1)[0].strip () != 'application/octet-stream':
			raise ValueError ('请上传 .sqlite3 数据库文件')
		l = int (self.headers.get ('Content-Length', '0'))
		if l <= 0 or l > 256 * 1024 * 1024: raise ValueError ('备份文件必须在 256 MB 以内')
		return self.rfile.read (l)
	
	def do_GET (self):
		path = urlparse (self.path).path
		if path == '/api/health': return self.sendj (200, {'app': 'soft_exam_memory', 'status': 'ok'})
		if path == '/api/config':
			try: return self.sendj (200, {'server': {'host': self.server.server_address[0], 'port': self.server.server_port}, 'default_model': model_config ()['default_model']})
			except Exception as ex: return self.sendj (503, {'error': str (ex)})
		if path == '/api/cpolar':
			try: return self.sendj (200, tunnel_status ())
			except Exception as ex: return self.sendj (503, {'error': str (ex)})
		if path == '/api/import-queue/status': return self.sendj (200, import_queue_status ())
		if path == '/api/cards':
			items = cards ()
			return self.sendj (200, {'cards': items, 'stats': card_stats (items), 'pinned_id': pinned_card_id (),
			                         'exam': {'id': EXAM['id'], 'title': EXAM['title'],
			                                  'database': DB.relative_to (ROOT).as_posix (),
			                                  'backup_dir': (DB.parent / 'backups').relative_to (ROOT).as_posix ()}})
		if path == '/api/knowledge': return self.sendj (200, {'knowledge': knowledge ()})
		if path == '/api/materials': return self.sendj (200, materials ())
		if path.startswith ('/api/media/'):
			media = rows ('SELECT mime,data FROM media WHERE id=?', (path.rsplit ('/', 1)[-1],))
			if not media: return self.send_error (404)
			item = media[0];
			self.send_response (200);
			self.send_header ('Content-Type', item['mime']);
			if item['mime'] == 'image/svg+xml':
				self.send_header ('Content-Security-Policy', "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
			self.send_header ('Cache-Control', 'private, max-age=' + ('86400' if urlparse (self.path).query else '300'));
			self.send_header ('Content-Length', str (len (item['data'])));
			self.send_header ('X-Content-Type-Options', 'nosniff');
			self.end_headers ();
			self.wfile.write (item['data']);
			return
		if path.startswith ('/api/generate/status/'):
			job_id = path.rsplit ('/', 1)[-1]
			with JOB_LOCK: job = JOBS.get (job_id, {}).copy ()
			return self.sendj (200, job) if job else self.sendj (404, {'error': '任务不存在或已过期'})
		if path == '/api/models':
			try:
				return self.sendj (200, {'models': models (), 'default_model': model_config ()['default_model']})
			except Exception as ex:
				return self.sendj (503, {'error': str (ex)})
		if path == '/api/notes':
			notes = rows ('SELECT name,digest,updated,content FROM notes ORDER BY updated DESC')
			result = []
			for n in notes:
				sections = h2_ranges (n['content']) if n['content'] else []
				known = {s['title'] for s in sections}
				for item in rows ("SELECT DISTINCT h2 FROM cards WHERE note=? AND h2<>''", (n['name'],)):
					if item['h2'] not in known: sections.append ({'title': item['h2'], 'start': -len (sections) - 1,
					                                              'end': -1})
				result.append ({'name': n['name'], 'updated': n['updated'], 'has_source': bool (n['content']),
				                'sections': sections})
			return self.sendj (200, {'notes': result})
		if path == '/': self.path = '/index.html'
		return super ().do_GET ()
	
	def do_POST (self):
		try:
			path = urlparse (self.path).path;
			b = self.sqlite_body () if path == '/api/backup/import-sqlite' else self.body ()
			if path == '/api/backup/save': return self.sendj (200, save_host_backup ())
			if path == '/api/backup/import-sqlite': return self.sendj (200, import_sqlite_backup (b))
			if path == '/api/import-queue/start':
				names = b.get ('files')
				if not isinstance (names, list) or not names or len (names) > 200 or any (
					not isinstance (n, str) for n in
					names) or len (set (names)) != len (names): raise ValueError ('请选择有效且不重名的笔记文件')
				if b.get ('token'):
					available = {p.name: p for p in selected_files (b['token'])}
					if any (n not in available for n in names): raise ValueError ('所选文件已失效，请重新选择')
					items = [{'name': n, 'path': str (available[n])} for n in names]
				else:
					docs = b.get ('documents')
					if not isinstance (docs, list) or [x.get ('name') for x in docs if
					                                   isinstance (x, dict)] != names or any (
						not isinstance (x.get ('markdown'), str) for x in docs): raise ValueError ('笔记内容无效')
					items = [{'name': x['name'], 'markdown': x['markdown']} for x in docs]
				if any (Path (n).name != n or not n.lower ().endswith (('.md', '.markdown')) for n in
				        names): raise ValueError ('只能导入 Markdown 笔记')
				return self.sendj (200, start_import_queue (items, b.get ('model'), b.get ('skip_unchanged', True)))
			if path == '/api/import-queue/retry': return self.sendj (200, retry_import_queue ())
			if path == '/api/local/choose-notes': return self.sendj (200, choose_notes ())
			if path == '/api/notes/inspect':
				md = b.get ('markdown', '')
				if not isinstance (md, str): raise ValueError ('Markdown 内容无效')
				return self.sendj (200, {'sections': h2_ranges (md)})
			if path == '/api/ingest/start':
				note = Path (str (b.get ('filename', ''))).name
				return self.sendj (200, start_ingest (note, b.get ('markdown', ''), b.get ('model'), b.get ('scope_title'), b.get ('action', 'append'), bool (b.get ('write_knowledge', True)), bool (b.get ('write_questions', True)), analyze=bool (b.get ('analyze'))))
			if path == '/api/local/ingest/start':
				source = next ((p for p in selected_files (b.get ('token')) if p.name == b.get ('filename')), None)
				if not source: raise ValueError ('所选文件不存在')
				return self.sendj (200, start_ingest (source.name, source.read_text (encoding='utf-8-sig'), b.get ('model'), b.get ('scope_title'), b.get ('action', 'append'), bool (b.get ('write_knowledge', True)), bool (b.get ('write_questions', True)), source))
			if path == '/api/knowledge/generate/start':
				note = b.get ('note');
				line = b.get ('line')
				item = next ((x for x in knowledge () if x['note'] == note and x['line'] == line), None)
				if not item: raise ValueError ('知识点不存在')
				title = item['h2'] if item['h2'] != '未分组' else '未分组'
				snippet = '# ' + Path (note).stem + '\n## ' + title + '\n' + item['content'] + '\n'
				return self.sendj (200, start_ingest (note, snippet, b.get ('model'), title, 'append', False, True, target_line=line))
			if path == '/api/local/generate/start':
				source = next ((p for p in selected_files (b.get ('token')) if p.name == b.get ('filename')), None)
				if not source: raise ValueError ('所选文件不存在')
				markdown = source.read_text (encoding='utf-8-sig')
				return self.sendj (200, start_generation (source.name, markdown, b.get ('model'), b.get ('skip_unchanged', True), b.get ('scope_start'), source, bool (b.get ('hard_replace')), b.get ('scope_title')))
			if path == '/api/generate/start':
				name = Path (str (b.get ('filename', '笔记.md'))).name
				return self.sendj (200, start_generation (name, b.get ('markdown', ''), b.get ('model'), b.get ('skip_unchanged', True)))
			if path == '/api/regenerate/start':
				name = Path (str (b.get ('filename', ''))).name
				note = rows ('SELECT content FROM notes WHERE name=?', (name,))
				if not note: raise ValueError ('找不到所选章节')
				md = b.get ('markdown') or note[0]['content']
				if not md: raise ValueError ('旧题库未保存原 Markdown，请选择同名源文件重新上传')
				return self.sendj (200, start_generation (name, md, b.get ('model'), False, b.get ('scope_start'), hard_replace=True))
			if path == '/api/generate':
				model = b.get ('model');
				name = Path (str (b.get ('filename', '笔记.md'))).name;
				md = b.get ('markdown', '')
				if not isinstance (md, str) or not md.strip (): raise ValueError ('空笔记')
				if b.get ('skip_unchanged', True):
					existing = rows ('SELECT digest FROM notes WHERE name=?', (name,))
					if existing and existing[0][
						'digest'] == hashlib.sha256 (md.encode ()).hexdigest (): return self.sendj (200, {
						'skipped': True, 'message': '笔记内容无变化，直接使用已保存的题库'})
				out = generate (md, name, model, max_rows=100000)
				result = save_cards (name, md, out['cards']);
				result.update ({'scanned_rows': out['scanned_rows'], 'rejected': out['rejected']})
				return self.sendj (200, result)
			if path == '/api/import':
				name = Path (str (b.get ('filename', '导入题库.md'))).name;
				md = b.get ('markdown', '')
				result = save_cards (name, md, b['cards'], replace=bool (b.get ('replace', True)), store_source=False)
				return self.sendj (200, result)
			if path == '/api/media/import': return self.sendj (200, import_media (
				b['note'], b['filename'], b['mime'], b['data'], b.get ('relative_path')))
			if path == '/api/manual/image': return self.sendj (200, add_manual_image (
				b.get ('note'), b.get ('title'), b.get ('filename'), b.get ('mime'), b.get ('data'), b.get ('annotation', '')))
			if path == '/api/cards/delete/preview': return self.sendj (200, deletion_preview (
				b['note'], b.get ('scope_start'), b.get ('markdown')))
			if path == '/api/cards/delete': return self.sendj (200, delete_cards (
				b['note'], b.get ('scope_start'), b.get ('markdown')))
			if path == '/api/cards/delete-item': return self.sendj (200, delete_item (b['id'], b.get ('kind')))
			if path == '/api/cards/pin': return self.sendj (200, toggle_pin (b['id']))
			if path == '/api/cards/delete-targets': return self.sendj (200, delete_targets (
				b['targets'], b.get ('mode') or ('both' if b.get ('with_source') else 'questions')))
			if path == '/api/knowledge/edit': return self.sendj (200, edit_knowledge (
				b['note'], b['line'], b['content']))
			if path == '/api/knowledge-card/edit': return self.sendj (200, edit_knowledge_card (b['id'], b['content'], b.get ('title')))
			if path == '/api/knowledge-card/split': return self.sendj (200, split_knowledge_card (
				b['id'], b['original'], b['parts']))
			if path == '/api/knowledge-card/merge-previous': return self.sendj (200, merge_knowledge_into_previous (
				b['id'], b['previous_id']))
			if path == '/api/image/merge-previous': return self.sendj (200, merge_image_into_previous (
				b['id'], b['previous_id']))
			if path == '/api/cards/unmerge': return self.sendj (200, unmerge_card_group (b['id']))
			if path == '/api/cards/edit-group': return self.sendj (200, edit_card_group (b['id'], b['parts']))
			if path == '/api/knowledge/delete': return self.sendj (200, delete_knowledge_item (b['note'], b['line']))
			if path == '/api/answer': return self.sendj (200, answer (b['id'], b['selected'], b.get ('mode')))
			if path == '/api/retry/next-batch': return self.sendj (200, release_retry_batch (b.get ('note')))
			if path == '/api/flags': return self.sendj (200, update_flags (b['id'], b))
			if path == '/api/image/annotation': return self.sendj (200, update_image_annotation (b['id'], b.get ('annotation')))
			if path == '/api/clear':
				with LOCK, connect () as db:
					db.execute ('DELETE FROM attempts');
					db.execute ('DELETE FROM reviews');
					db.execute ('UPDATE cards SET wrong_marked=0,wrong_marked_at=NULL,favorite=0,favorite_at=NULL,round1_done=0,persistent_unknown=0,retry_deferred=0,random_return_pending=0')
				return self.sendj (200, {'ok': True})
			return self.sendj (404, {'error': '接口不存在'})
		except Exception as ex:
			return self.sendj (422, {'error': str (ex)})


if __name__ == '__main__':
	settings = server_config ()
	port_args = [arg for arg in sys.argv[1:] if arg != '--open']
	port = int (port_args[0]) if port_args else settings['port']
	httpd = QuietHTTPServer ((settings['host'], port), Handler)
	init ()
	start_queue_watchdog ()
	address = 'http://' + ('[' + settings['host'] + ']' if ':' in settings['host'] else settings['host']) + ':' + str (port) + '/'
	print (EXAM['title'] + '：' + address, flush=True)
	if '--open' in sys.argv: webbrowser.open (address)
	httpd.serve_forever ()
