import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server
import ollama_engine
from ollama_engine import source_rows, question_rows


class WorkflowTest(unittest.TestCase):
    def test_full_sentence_cloze_is_not_saved_or_served(self):
        bad = {'sentence':'【$3*1+1=4$】','answer':'$3*1+1=4$',
               'options':['$3*1+1=4$','$3*2=6>4$','$n*(R-1)$','$n*(R-1)+1$'],
               'section':'死锁','line':1}
        self.assertFalse(ollama_engine.meaningful_cloze('$3*1+1=4$', '$3*1+1=4$'))
        self.assertEqual(server.save_cards('操作系统.md','$3*1+1=4$',[bad])['saved'],0)
        with server.connect() as db:
            db.execute('''INSERT INTO cards(id,note,section,h2,original,answer,options,line,topic,active,created,kind)
                          VALUES(?,?,?,?,?,?,?,?,?,1,?,?)''',
                       ('existing-bad','操作系统.md','死锁','死锁','$3*1+1=4$','$3*1+1=4$',
                        json.dumps(bad['options']),1,'bad',server.now(),'cloze'))
        self.assertEqual(server.cards(),[])
        self.assertEqual(server.stats()['total'],0)

    def test_formula_option_is_a_valid_answer_when_question_has_context(self):
        card = {'original':'系统至少需要$n*(R-1)+1$个资源才能避免死锁。',
                'answer':'$n*(R-1)+1$',
                'options':['$n*(R-1)+1$','$n*(R-1)$','$3*1+1=4$','$3*2=6>4$'],
                'section':'死锁','line':1}
        self.assertEqual(server.save_cards('操作系统.md',card['original'],[card])['saved'],1)
        saved = server.cards()[0]
        self.assertTrue(server.answer(saved['id'],'$3*2=6>4$')['correct'] is False)

    def test_paragraph_keeps_full_context_for_cloze(self):
        markdown = '# 第一章\n## 核心\n程序计数器保存下一条指令的地址。指令寄存器保存当前正在执行的指令。'
        originals = [row['original'] for row in question_rows(markdown, '第一章.md')]
        self.assertEqual(originals, ['程序计数器保存下一条指令的地址。指令寄存器保存当前正在执行的指令。'])

    def test_related_note_lines_keep_their_shared_context(self):
        root = Path(__file__).resolve().parents[1] / 'notes' / '软考笔记'
        operating = question_rows((root / '操作系统.md').read_text(encoding='utf-8'), '操作系统.md')
        example = next(row['original'] for row in operating if '这个不会死锁' in row['original'])
        self.assertIn('资源数：4', example)
        self.assertIn('每个进程需要最大资源数2', example)
        self.assertIn('还有一个随便分', example)
        patent = question_rows((root / '标准化和知识产权.md').read_text(encoding='utf-8'), '标准化和知识产权.md')
        provision = next(row['original'] for row in patent if '对前款第（四）项' in row['original'])
        self.assertIn('（四）动物和植物品种', provision)
        self.assertIn('生产方法', provision)

    def test_deleted_questions_are_regenerated_even_when_note_is_unchanged(self):
        markdown = '## 核心\n程序计数器保存下一条指令的地址。'
        server.save_cards('重导.md', markdown, [])
        with patch.object(server, 'models', return_value=['test-model']), patch.object(server, 'generate', return_value={'cards': [], 'scanned_rows': 1, 'rejected': 0}):
            started = server.start_generation('重导.md', markdown, 'test-model', True)
            for _ in range(100):
                with server.JOB_LOCK: done = server.JOBS[started['job_id']]['done']
                if done:break
                time.sleep(0.01)
        self.assertIn('job_id', started)
        self.assertTrue(done)

    def test_generation_batches_stop_at_second_level_heading(self):
        markdown = '# 第一章\n## 处理器\n程序计数器保存下一条指令的地址。\n## 存储器\n主存储器保存正在运行的程序和数据。'
        prompts = []
        progress = []
        def fake_request(url, **kwargs):
            prompts.append(kwargs['data']['messages'][-1]['content'])
            return {'message': {'content': '{"cards":[]}'}}
        with patch.object(ollama_engine, 'request_json', side_effect=fake_request):
            ollama_engine.generate(markdown, '第一章.md', 'test-model', progress=lambda *args: progress.append(args))
        self.assertEqual(len(prompts), 2)
        self.assertIn('二级标题「处理器」', prompts[0])
        self.assertNotIn('主存储器', prompts[0])
        self.assertIn('二级标题「存储器」', prompts[1])
        self.assertEqual(progress[-1][:4], (2, 2, 2, 2))

    def test_generation_checkpoint_resumes_after_failed_batch(self):
        markdown = '# 第一章\n## 处理器\n程序计数器保存下一条指令的地址。\n## 存储器\n主存储器保存正在运行的程序和数据。'
        calls = []
        fail = [True]
        def fake_request(url, **kwargs):
            prompt = kwargs['data']['messages'][-1]['content']
            calls.append(prompt)
            if '二级标题「存储器」' in prompt and fail[0]:raise TimeoutError('timed out')
            original, answer = ('程序计数器保存下一条指令的地址。', '程序计数器') if '二级标题「处理器」' in prompt else ('主存储器保存正在运行的程序和数据。', '主存储器')
            return {'message':{'content':json.dumps({'cards':[{'original':original,'answer':answer,'distractors':['指令寄存器','高速缓存','外部存储器']}]},ensure_ascii=False)}}
        def wait_job(job_id):
            for _ in range(100):
                with server.JOB_LOCK:job=dict(server.JOBS[job_id])
                if job['done']:return job
                time.sleep(0.02)
            self.fail('生成任务未结束')
        with patch.object(server, 'models', return_value=['test-model']), patch.object(ollama_engine, 'request_json', side_effect=fake_request):
            first = wait_job(server.start_generation('第一章.md',markdown,'test-model',False,checkpoint=True)['job_id'])
            self.assertIn('timed out', first['error'])
            self.assertEqual(len(server.rows('SELECT * FROM generation_batches')), 1)
            self.assertEqual(len(server.rows('SELECT * FROM cards')), 0)
            fail[0] = False
            second = wait_job(server.start_generation('第一章.md',markdown,'test-model',False,checkpoint=True)['job_id'])
        self.assertIsNone(second['error'])
        self.assertEqual(second['result']['resumed_batches'], 1)
        self.assertEqual(second['result']['saved'], 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(server.rows('SELECT * FROM generation_batches')), 0)

    def test_failed_import_remains_pending_until_retry(self):
        note = Path(self.temp.name) / '待重试.md'
        note.write_text('## 核心\n程序计数器保存下一条指令的地址。', encoding='utf-8')
        attempts = []
        def flaky_start(name, markdown, model, skip, source_path=None, checkpoint=False):
            attempts.append(name)
            if len(attempts) == 1:raise RuntimeError('模型临时不可用')
            server.save_cards(name, markdown, [])
            return {'saved': 0}
        with patch.object(server, 'models', return_value=['test-model']), patch.object(server, 'start_generation', side_effect=flaky_start):
            server.start_import_queue([{'name': note.name, 'path': str(note)}], 'test-model', False)
            for _ in range(100):
                status = server.import_queue_status()
                if status['error']:break
                time.sleep(0.02)
            self.assertEqual(status['pending'], [note.name])
            self.assertIn('模型临时不可用', status['error'])
            server.retry_import_queue()
            for _ in range(100):
                status = server.import_queue_status()
                if not status['pending']:break
                time.sleep(0.02)
        self.assertEqual(status['pending'], [])
        self.assertEqual(len(attempts), 2)

    def test_failed_import_is_retried_automatically_when_due(self):
        note = Path(self.temp.name) / '自动续传.md'
        note.write_text('## 核心\n程序计数器保存下一条指令的地址。', encoding='utf-8')
        attempts = []
        def flaky_start(name, markdown, model, skip, source_path=None, checkpoint=False):
            attempts.append(name)
            if len(attempts) == 1:raise TimeoutError('Ollama timed out')
            server.save_cards(name, markdown, [])
            return {'saved':0}
        with patch.object(server, 'models', return_value=['test-model']), patch.object(server, 'start_generation', side_effect=flaky_start):
            server.start_import_queue([{'name':note.name,'path':str(note)}], 'test-model', False)
            for _ in range(100):
                status = server.import_queue_status()
                if status['error']:break
                time.sleep(0.02)
            self.assertEqual(status['pending'], [note.name])
            self.assertEqual(status['retry_count'], 1)
            self.assertGreater(status['retry_at'], time.time())
            for _ in range(100):
                with server.QUEUE_LOCK:running = server.QUEUE_RUNNING
                if not running:break
                time.sleep(0.01)
            server.resume_import_queue()
            self.assertEqual(len(attempts), 1)
            with server.QUEUE_LOCK:
                state = server.read_import_queue()
                state['retry_at'] = time.time()-1
                server.write_import_queue(state)
            server.resume_import_queue()
            for _ in range(100):
                status = server.import_queue_status()
                if not status['pending']:break
                time.sleep(0.02)
        self.assertEqual(status['pending'], [])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(status['retry_count'], 0)

    def test_old_import_queue_waits_for_new_generation_rules(self):
        source = Path(self.temp.name) / '新笔记.md'
        source.write_text('## 核心\n程序计数器保存下一条指令的地址。', encoding='utf-8')
        old = {'id':'old','pending':[{'name':'旧笔记.md','path':str(source)}],
               'completed':[],'model':'test-model','skip_unchanged':False,'error':None}
        server.write_import_queue(old)
        server.resume_import_queue()
        self.assertTrue(server.import_queue_status()['outdated'])
        self.assertFalse(server.QUEUE_RUNNING)
        def fake_start(name, markdown, model, skip, source_path=None, checkpoint=False):
            server.save_cards(name, markdown, [])
            return {'saved':0}
        with patch.object(server, 'models', return_value=['test-model']), patch.object(server, 'start_generation', side_effect=fake_start):
            server.start_import_queue([{'name':source.name,'path':str(source)}], 'test-model', False)
            for _ in range(100):
                status = server.import_queue_status()
                if not status['pending']:break
                time.sleep(0.02)
        self.assertFalse(status['outdated'])
        self.assertEqual(status['completed'][0]['name'], source.name)
        archives = list(Path(self.temp.name).glob('import-queue.previous.*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text(encoding='utf-8'))['id'], 'old')

    def test_import_queue_saves_one_file_and_removes_its_pending_record(self):
        first = Path(self.temp.name) / '第一章.md'
        second = Path(self.temp.name) / '第二章.md'
        first.write_text('## 第一节\n第一章的知识点用于测试。', encoding='utf-8')
        second.write_text('## 第二节\n第二章的知识点用于测试。', encoding='utf-8')
        calls = []
        def fake_start(name, markdown, model, skip, source_path=None, checkpoint=False):
            calls.append(name)
            self.assertTrue(source_path.is_file())
            server.save_cards(name, markdown, [])
            return {'saved': 0, 'name': name}
        with patch.object(server, 'models', return_value=['test-model']), patch.object(server, 'start_generation', side_effect=fake_start):
            server.start_import_queue([{'name': first.name, 'path': str(first)}, {'name': second.name, 'path': str(second)}], 'test-model', False)
            for _ in range(100):
                status = server.import_queue_status()
                if not status['pending'] or status['error']:break
                time.sleep(0.02)
        self.assertIsNone(status['error'])
        self.assertEqual(calls, [first.name, second.name])
        self.assertEqual(len(status['completed']), 2)
        self.assertEqual(status['pending'], [])
        self.assertEqual({x['name'] for x in server.rows('SELECT name FROM notes')}, {first.name, second.name})

    def test_choose_notes_decodes_chinese_path(self):
        note = Path(self.temp.name) / '第一章.md'
        note.write_text('## 基本概念\n正文', encoding='utf-8')
        output = json.dumps([str(note)], ensure_ascii=True)
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
            result = server.choose_notes()
        self.assertEqual(result['files'], ['第一章.md'])
        self.assertEqual(result['documents'][0]['sections'][0]['title'], '基本概念')
        self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
        self.assertEqual(run.call_args.kwargs['errors'], 'replace')

    def test_health_identifies_this_service(self):
        self.assertEqual(self.request('/api/health'), {'app':'soft_exam_memory','status':'ok'})

    def test_expected_disconnects_do_not_print_tracebacks(self):
        with patch.object(ThreadingHTTPServer, 'handle_error') as report_error:
            for error in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                try:
                    raise error('client disconnected')
                except error:
                    self.http.handle_error(None, ('127.0.0.1', 12345))
            report_error.assert_not_called()

            try:
                raise RuntimeError('unexpected server failure')
            except RuntimeError:
                self.http.handle_error(None, ('127.0.0.1', 12345))
            report_error.assert_called_once()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        self.old_models = server.models
        self.old_generate = server.generate
        self.old_analyze = server.analyze_points
        server.DB = Path(self.temp.name) / 'study.sqlite3'
        server.init()
        self.http = server.QuietHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        server.DB = self.old_db
        server.models = self.old_models
        server.generate = self.old_generate
        server.analyze_points = self.old_analyze
        self.temp.cleanup()

    def request(self, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_answer_and_flags_control_study_pools(self):
        card = {'section': 'CPU', 'original': '程序计数器保存下一条指令的地址',
                'answer': '程序计数器', 'options': ['程序计数器', '指令寄存器', '状态寄存器', '地址寄存器']}
        saved = self.request('/api/import', {'filename': 'test.json', 'markdown': 'test', 'cards': [card]})
        self.assertEqual(saved['saved'], 1)
        before = self.request('/api/cards')
        self.assertEqual(before['stats']['total'], 1)
        card_id = before['cards'][0]['id']
        answer = self.request('/api/answer', {'id': card_id, 'selected': '指令寄存器'})
        self.assertFalse(answer['correct'])
        after = self.request('/api/cards')
        self.assertEqual(after['stats']['studied'], 1)
        self.assertEqual(after['stats']['wrong'], 1)
        self.assertEqual(after['cards'][0]['wrong_marked'], 1)
        self.assertTrue(after['cards'][0]['wrong_marked_at'])
        self.request('/api/flags', {'id': card_id, 'favorite': True})
        self.assertEqual(self.request('/api/cards')['stats']['favorites'], 1)
        self.assertTrue(self.request('/api/cards')['cards'][0]['favorite_at'])
        self.request('/api/answer', {'id': card_id, 'selected': '程序计数器'})
        self.assertEqual(self.request('/api/cards')['cards'][0]['wrong_marked'], 1)
        self.request('/api/flags', {'id': card_id, 'wrong_marked': False, 'favorite': False})
        cleared = self.request('/api/cards')
        self.assertEqual(cleared['stats']['wrong'], 0)
        self.assertEqual(cleared['stats']['favorites'], 0)
        self.assertEqual(cleared['cards'][0]['wrong'], 1)
        self.assertIsNone(cleared['cards'][0]['wrong_marked_at'])
        self.assertIsNone(cleared['cards'][0]['favorite_at'])

    def test_clear_study_records_also_clears_favorites(self):
        card = {'section':'CPU','original':'程序计数器保存下一条指令的地址',
                'answer':'程序计数器','options':['程序计数器','指令寄存器','状态寄存器','地址寄存器']}
        self.request('/api/import', {'filename':'test.json','markdown':'test','cards':[card]})
        card_id = self.request('/api/cards')['cards'][0]['id']
        self.request('/api/answer', {'id':card_id,'selected':'指令寄存器','mode':'wrong'})
        self.request('/api/flags', {'id':card_id,'favorite':True})
        self.request('/api/clear', {})
        result = self.request('/api/cards')
        self.assertEqual(result['stats'], {'total':1,'studied':0,'wrong':0,'favorites':0,'notes':1})
        saved = result['cards'][0]
        self.assertEqual((saved['wrong_marked'],saved['favorite'],saved['round1_done']), (0,0,0))
        self.assertIsNone(saved['wrong_marked_at'])
        self.assertIsNone(saved['favorite_at'])

    def test_generation_reports_batch_progress_and_saves(self):
        server.models = lambda: ['test-model']

        def fake_generate(markdown, filename, model, max_rows, progress, line_range=None):
            progress(0, 24, 0, 2, 0, 0)
            time.sleep(0.05)
            progress(12, 24, 1, 2, 1, 0)
            time.sleep(0.05)
            progress(24, 24, 2, 2, 1, 0)
            return {'cards': [{'section': 'CPU', 'original': '程序计数器保存下一条指令的地址',
                               'answer': '程序计数器', 'options': ['程序计数器', '指令寄存器', '状态寄存器', '地址寄存器']}],
                    'scanned_rows': 24, 'rejected': 0}

        server.generate = fake_generate
        started = self.request('/api/generate/start', {'filename': 'cpu.md', 'markdown': 'content', 'model': 'test-model'})
        self.assertIn('job_id', started)
        status = {}
        for _ in range(100):
            status = self.request('/api/generate/status/' + started['job_id'])
            if status['done']:
                break
            time.sleep(0.01)
        self.assertTrue(status['done'])
        self.assertEqual(status['processed_rows'], 24)
        self.assertEqual(status['completed_batches'], 2)
        self.assertEqual(status['result']['saved'], 1)
        self.assertEqual(self.request('/api/cards')['stats']['total'], 1)
        skipped = self.request('/api/generate/start', {'filename': 'cpu.md', 'markdown': 'content',
                                                       'model': 'test-model', 'skip_unchanged': True})
        self.assertTrue(skipped['skipped'])

    def test_existing_wrong_answers_become_marks_on_migration(self):
        legacy = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            with db:
                db.execute('CREATE TABLE cards(id TEXT PRIMARY KEY, note TEXT, section TEXT, original TEXT, answer TEXT, options TEXT, line INTEGER, topic TEXT, active INTEGER, created TEXT)')
                db.execute('CREATE TABLE reviews(card_id TEXT PRIMARY KEY, attempts INTEGER, wrong INTEGER, streak INTEGER, interval_days REAL, due TEXT, last_correct INTEGER)')
                db.execute("INSERT INTO cards VALUES('old','a.md','A','原文保留上下文','原文','[]',1,'t',1,'now')")
                db.execute("INSERT INTO reviews VALUES('old',1,1,0,1,NULL,0)")
        server.DB = legacy
        server.init()
        migrated = self.request('/api/cards')
        self.assertEqual(migrated['cards'][0]['wrong_marked'], 1)
        self.assertEqual(migrated['cards'][0]['favorite'], 0)

    def test_first_round_moves_card_to_retry_pool(self):
        card = {'section': 'CPU', 'original': '程序计数器保存下一条指令的地址',
                'answer': '程序计数器', 'options': ['程序计数器', '指令寄存器', '状态寄存器', '地址寄存器']}
        self.request('/api/import', {'filename': 'test.json', 'markdown': 'test', 'cards': [card]})
        card_id = self.request('/api/cards')['cards'][0]['id']
        self.request('/api/flags', {'id': card_id, 'favorite': True})
        self.request('/api/answer', {'id': card_id, 'selected': '程序计数器', 'mode': 'wrong'})
        moved = self.request('/api/cards')['cards'][0]
        self.assertEqual(moved['round1_done'], 1)
        self.assertEqual(moved['favorite'], 1)

    def test_example_only_note_is_preserved_without_ollama_chat(self):
        server.models = lambda: ['test-model']
        md = '# 第一章\n## 考点图示\n<img src="./img/cpu.png" alt="示意" style="zoom:50%;" />\n### 例题一\n原题：下列哪项正确？\nA. 甲  B. 乙\n'
        source=Path(self.temp.name)/'图示.md';source.write_text(md,encoding='utf-8')
        image_dir=source.parent/'img';image_dir.mkdir();(image_dir/'cpu.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        started = server.start_generation('图示.md',md,'test-model',False,source_path=source)
        for _ in range(100):
            status = self.request('/api/generate/status/' + started['job_id'])
            if status['done']:
                break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        self.assertEqual(status['result']['saved'], 2)
        self.assertEqual({x['kind'] for x in self.request('/api/cards')['cards']}, {'example','image'})
        self.assertTrue(self.request('/api/notes')['notes'][0]['has_source'])
        material = self.request('/api/materials')
        self.assertEqual(len(material['examples']), 1)
        self.assertEqual(len(material['images']), 1)
        self.assertEqual(material['images'][0]['title'], '考点图示')
        image = base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()
        result = self.request('/api/media/import', {'note': '图示.md', 'filename': 'cpu.png',
                                                   'relative_path': 'img/cpu.png', 'mime': 'image/png', 'data': image})
        self.assertEqual(result['saved'], 1)

    def test_scoped_regeneration_preserves_other_heading_and_materials(self):
        md = '# 第一章\n## 标题甲\n甲知识点用于保存重要内容。\n## 标题乙\n乙知识点用于处理其他内容。\n![示意](cpu.png)\n### 例题一\n原题：CPU 的作用是什么？\nA. 保存数据  B. 处理指令\n'
        a = {'section': '标题甲', 'original': '甲知识点用于保存重要内容。', 'answer': '甲知识点',
             'options': ['甲知识点', '乙知识点', '丙知识点', '丁知识点'], 'line': 3}
        b = {'section': '标题乙', 'original': '乙知识点用于处理其他内容。', 'answer': '乙知识点',
             'options': ['乙知识点', '甲知识点', '丙知识点', '丁知识点'], 'line': 5}
        server.save_cards('第一章.md', md, [a, b])
        notes = self.request('/api/notes')['notes']
        self.assertEqual([x['title'] for x in notes[0]['sections']], ['标题甲', '标题乙'])
        self.assertTrue(notes[0]['has_source'])
        server.models = lambda: ['test-model']

        def fake_generate(markdown, filename, model, max_rows, progress, line_range=None):
            self.assertEqual(line_range, (2, 3))
            progress(1, 1, 1, 1, 1, 0)
            changed = dict(a, answer='保存重要内容', options=['保存重要内容', '处理其他内容', '增加执行速度', '检查程序结果'])
            return {'cards': [changed], 'scanned_rows': 1, 'rejected': 0}

        server.generate = fake_generate
        started = self.request('/api/regenerate/start', {'filename': '第一章.md', 'model': 'test-model', 'scope_start': 2})
        for _ in range(100):
            status = self.request('/api/generate/status/' + started['job_id'])
            if status['done']:
                break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        active = self.request('/api/cards')['cards']
        self.assertEqual(len(active), 2)
        self.assertEqual({x['answer'] for x in active}, {'保存重要内容', '乙知识点'})
        self.assertEqual({x['h2'] for x in active}, {'标题甲', '标题乙'})

        material = self.request('/api/materials')
        self.assertEqual(material['images'][0]['title'], '标题乙')
        self.assertIn('原题：CPU 的作用是什么？', material['examples'][0]['body'])
        self.assertFalse(any('原题：CPU' in row['original'] for row in source_rows(md, '第一章.md')))
        image = base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()
        self.assertEqual(self.request('/api/media/import', {'note': '第一章.md', 'filename': 'cpu.png', 'mime': 'image/png', 'data': image})['saved'], 1)
        media_id = self.request('/api/materials')['images'][0]['id']
        with urllib.request.urlopen(self.base + '/api/media/' + media_id) as response:
            self.assertEqual(response.read(), b'\x89PNG\r\n\x1a\n')

        preview = self.request('/api/cards/delete/preview', {'note': '第一章.md', 'scope_start': 2})
        self.assertEqual(preview['count'], 1)
        self.assertEqual(self.request('/api/cards/delete', {'note': '第一章.md', 'scope_start': 2})['deleted'], 1)
        self.assertEqual([c['answer'] for c in self.request('/api/cards')['cards']], ['乙知识点'])
        self.assertEqual(len(self.request('/api/materials')['images']), 1)
        self.assertEqual(self.request('/api/cards/delete', {'note': '第一章.md'})['deleted'], 1)
        self.assertEqual(self.request('/api/cards')['stats']['total'], 0)

    def test_local_note_imports_sibling_image_as_study_item_and_deletes_it(self):
        source = Path(self.temp.name) / 'chapter.md'
        image_dir = source.parent / 'img'
        image_dir.mkdir()
        blob = b'\x89PNG\r\n\x1a\nimage'
        (image_dir / 'diagram.png').write_bytes(blob)
        source.write_text('# 章\n## 结构图\n<img src="./img/diagram.png" />\n', encoding='utf-8')
        server.models = lambda: ['test-model']
        started = server.start_generation(source.name, source.read_text(encoding='utf-8'), 'test-model', False, source_path=source)
        for _ in range(100):
            status = self.request('/api/generate/status/' + started['job_id'])
            if status['done']:
                break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        card = self.request('/api/cards')['cards'][0]
        self.assertEqual(card['kind'], 'image')
        self.assertEqual(card['options'], ['了解', '不了解'])
        with urllib.request.urlopen(self.base + '/api/media/' + card['media_id']) as response:
            self.assertEqual(response.read(), blob)
        source.unlink()
        (image_dir / 'diagram.png').unlink()
        restarted = self.request('/api/regenerate/start', {'filename':'chapter.md','model':'test-model'})
        for _ in range(100):
            second = self.request('/api/generate/status/' + restarted['job_id'])
            if second['done']:
                break
            time.sleep(0.01)
        self.assertIsNone(second['error'])
        card = self.request('/api/cards')['cards'][0]
        with urllib.request.urlopen(self.base + '/api/media/' + card['media_id']) as response:
            self.assertEqual(response.read(), blob)
        self.assertFalse(self.request('/api/answer', {'id': card['id'], 'selected': '不了解'})['correct'])
        self.assertEqual(self.request('/api/cards')['cards'][0]['wrong_marked'], 1)
        self.assertEqual(self.request('/api/cards/delete-item', {'id': card['id']})['deleted'], 1)
        self.assertEqual(self.request('/api/cards')['stats']['total'], 0)

    def test_delete_selected_heading_with_saved_source_and_media(self):
        md = '# 章\n## 甲\n![图](img/a.png)\n## 乙\n乙知识点用于保存内容。\n'
        a = {'kind':'image','identity':'image:img/a.png','section':'甲','h2':'甲','line':3,
             'original':'甲','answer':'了解','options':['了解','不了解'],'media_id':'m1'}
        b = {'section':'乙','h2':'乙','line':5,'original':'乙知识点用于保存内容。',
             'answer':'乙知识点','options':['乙知识点','甲知识点','丙知识点','丁知识点']}
        server.save_cards('章.md', md, [a,b], media_items=[('m1','章.md','img/a.png','甲','image/png',b'png',server.now())])
        result = self.request('/api/cards/delete-targets', {'targets':[{'note':'章.md','scope_start':2}], 'with_source':True})
        self.assertEqual(result['deleted'], 1)
        self.assertEqual([x['h2'] for x in self.request('/api/cards')['cards']], ['乙'])
        self.assertEqual(server.rows('SELECT id FROM media'), [])
        note = server.rows('SELECT content FROM notes WHERE name=?', ('章.md',))[0]['content']
        self.assertNotIn('img/a.png', note)
        self.assertIn('## 乙', note)

    def test_reuploaded_heading_matches_title_after_line_numbers_shift(self):
        old = '# 章\n## 甲\n甲知识点用于保存内容。\n## 乙\n乙知识点用于处理内容。\n'
        new = '# 章\n说明\n## 甲\n甲知识点用于保存新内容。\n## 乙\n乙知识点用于处理内容。\n'
        first = {'section':'甲','h2':'甲','line':3,'original':'甲知识点用于保存内容。',
                 'answer':'甲知识点','options':['甲知识点','乙知识点','丙知识点','丁知识点']}
        second = {'section':'乙','h2':'乙','line':5,'original':'乙知识点用于处理内容。',
                  'answer':'乙知识点','options':['乙知识点','甲知识点','丙知识点','丁知识点']}
        server.save_cards('章.md', old, [first, second])
        path = Path(self.temp.name) / '章.md'
        path.write_text(new, encoding='utf-8')
        server.models = lambda: ['test-model']
        def fake_generate(markdown, filename, model, max_rows, progress, line_range=None):
            self.assertEqual(line_range, (3,4))
            item = dict(first, line=4, original='甲知识点用于保存新内容。')
            return {'cards':[item], 'scanned_rows':1, 'rejected':0}
        server.generate = fake_generate
        started = server.start_generation('章.md', new, 'test-model', False, 2, path, True, '甲')
        for _ in range(100):
            status = self.request('/api/generate/status/' + started['job_id'])
            if status['done']:break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        originals = [c['original'] for c in self.request('/api/cards')['cards']]
        self.assertEqual(set(originals), {'甲知识点用于保存新内容。','乙知识点用于处理内容。'})

    def test_knowledge_edit_and_three_delete_modes(self):
        md='# 章\n## 甲\n甲知识点用于保存重要内容。\n## 乙\n乙知识点用于处理其他内容。\n'
        a={'section':'甲','h2':'甲','line':3,'original':'甲知识点用于保存重要内容。','answer':'甲知识点',
           'options':['甲知识点','乙知识点','丙知识点','丁知识点']}
        b={'section':'乙','h2':'乙','line':5,'original':'乙知识点用于处理其他内容。','answer':'乙知识点',
           'options':['乙知识点','甲知识点','丙知识点','丁知识点']}
        server.save_cards('章.md',md,[a,b])
        self.assertEqual(len(self.request('/api/knowledge')['knowledge']),2)
        edited=self.request('/api/knowledge/edit',{'note':'章.md','line':3,'content':'甲知识点用于保存修改后的重要内容。'})
        self.assertIn('修改后',edited['content'])
        self.assertEqual(self.request('/api/cards')['stats']['total'],2)
        scope={'note':'章.md','scope_start':2,'scope_title':'甲'}
        removed=self.request('/api/cards/delete-targets',{'targets':[scope],'mode':'knowledge'})
        self.assertEqual(removed['deleted_knowledge'],1)
        self.assertEqual(self.request('/api/cards')['stats']['total'],2)
        removed=self.request('/api/cards/delete-targets',{'targets':[scope],'mode':'questions'})
        self.assertEqual(removed['deleted_cards'],1)
        self.assertEqual([x['h2'] for x in self.request('/api/cards')['cards']],['乙'])
        removed=self.request('/api/cards/delete-targets',{'targets':[{'note':'章.md'}],'mode':'both'})
        self.assertEqual(removed['deleted_cards'],1)
        self.assertEqual(self.request('/api/notes')['notes'],[])

    def test_ingest_append_and_replace_selected_heading(self):
        old='# 章\n## 甲\n甲知识点用于保存旧内容。\n## 乙\n乙知识点用于处理其他内容。\n'
        incoming='# 章\n## 甲\n甲知识点用于保存新内容。\n'
        a={'section':'甲','h2':'甲','line':3,'original':'甲知识点用于保存旧内容。','answer':'甲知识点',
           'options':['甲知识点','乙知识点','丙知识点','丁知识点']}
        b={'section':'乙','h2':'乙','line':5,'original':'乙知识点用于处理其他内容。','answer':'乙知识点',
           'options':['乙知识点','甲知识点','丙知识点','丁知识点']}
        server.save_cards('章.md',old,[a,b])
        server.models=lambda:['test-model']
        def fake_generate(markdown,filename,model,max_rows,progress,line_range=None):
            item=dict(a,original='甲知识点用于保存新内容。')
            return {'cards':[item],'scanned_rows':1,'rejected':0}
        server.generate=fake_generate
        started=self.request('/api/ingest/start',{'filename':'章.md','markdown':incoming,'model':'test-model',
                                                   'scope_title':'甲','action':'append','write_knowledge':True,'write_questions':True})
        for _ in range(100):
            status=self.request('/api/generate/status/'+started['job_id'])
            if status['done']:break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        self.assertEqual(self.request('/api/cards')['stats']['total'],3)
        note=server.rows('SELECT content FROM notes WHERE name=?',('章.md',))[0]['content']
        self.assertIn('旧内容',note)
        self.assertIn('新内容',note)
        started=self.request('/api/ingest/start',{'filename':'章.md','markdown':incoming,'model':'test-model',
                                                   'scope_title':'甲','action':'replace','write_knowledge':True,'write_questions':True})
        for _ in range(100):
            status=self.request('/api/generate/status/'+started['job_id'])
            if status['done']:break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        originals={x['original'] for x in self.request('/api/cards')['cards']}
        self.assertEqual(originals,{'甲知识点用于保存新内容。','乙知识点用于处理其他内容。'})

    def test_modified_knowledge_can_generate_new_question(self):
        md='# 章\n## 甲\n甲知识点用于保存重要内容。\n'
        server.save_cards('章.md',md,[],replace=False)
        self.request('/api/knowledge/edit',{'note':'章.md','line':3,'content':'甲知识点用于保存修改后的重要内容。'})
        server.models=lambda:['test-model']
        def fake_generate(markdown,filename,model,max_rows,progress,line_range=None):
            item={'section':'甲','h2':'甲','line':3,'original':'甲知识点用于保存修改后的重要内容。',
                  'answer':'甲知识点','options':['甲知识点','乙知识点','丙知识点','丁知识点']}
            return {'cards':[item],'scanned_rows':1,'rejected':0}
        server.generate=fake_generate
        started=self.request('/api/knowledge/generate/start',{'note':'章.md','line':3,'model':'test-model'})
        for _ in range(100):
            status=self.request('/api/generate/status/'+started['job_id'])
            if status['done']:break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        self.assertEqual(status['result']['saved'],1)
        self.assertEqual(self.request('/api/cards')['cards'][0]['line'],3)

    def test_manual_text_is_analyzed_before_saving(self):
        server.save_cards('章.md','# 章\n## 甲\n旧知识点用于保存重要内容。\n',[],replace=False)
        server.models=lambda:['test-model']
        server.analyze_points=lambda text,model:['新知识点用于保存由模型提取的重要内容。']
        def fake_generate(markdown,filename,model,max_rows,progress,line_range=None):
            self.assertIn('由模型提取',markdown)
            item={'section':'甲','h2':'甲','line':3,'original':'新知识点用于保存由模型提取的重要内容。',
                  'answer':'新知识点','options':['新知识点','旧知识点','其他知识点','临时知识点']}
            return {'cards':[item],'scanned_rows':1,'rejected':0}
        server.generate=fake_generate
        started=self.request('/api/ingest/start',{'filename':'章.md','markdown':'# 章\n## 甲\n输入一段原始材料。\n',
                                                   'model':'test-model','scope_title':'甲','action':'append',
                                                   'write_knowledge':True,'write_questions':True,'analyze':True})
        for _ in range(100):
            status=self.request('/api/generate/status/'+started['job_id'])
            if status['done']:break
            time.sleep(0.01)
        self.assertIsNone(status['error'])
        self.assertEqual(status['result']['analyzed_points'],1)
        self.assertIn('由模型提取',server.rows('SELECT content FROM notes WHERE name=?',('章.md',))[0]['content'])

    def test_existing_browser_import_backfills_images_once_from_notes_directory(self):
        original_root=server.ROOT
        try:
            server.ROOT=Path(self.temp.name)
            note_dir=server.ROOT/'notes';image_dir=note_dir/'img';image_dir.mkdir(parents=True)
            md='# 章\n## 图片标题\n![图](./img/test.png)\n'
            (note_dir/'browser.md').write_text(md,encoding='utf-8')
            (image_dir/'test.png').write_bytes(b'PNG image')
            server.save_cards('browser.md',md,[],replace=False)
            with server.connect() as db:db.execute("DELETE FROM app_meta WHERE key='image_backfill_v1'")
            server.init()
            image_card=next(c for c in self.request('/api/cards')['cards'] if c['kind']=='image')
            self.assertEqual(image_card['h2'],'图片标题')
            self.assertEqual(len(server.rows('SELECT id FROM media')),1)
            self.request('/api/cards/delete-item',{'id':image_card['id']})
            server.init()
            self.assertEqual(self.request('/api/cards')['stats']['total'],0)
        finally:
            server.ROOT=original_root

    def test_browser_upload_with_unresolvable_image_fails_before_generation(self):
        server.models=lambda:['test-model']
        md='# 章\n## 图片\n![图](./img/missing.png)\n'
        body=json.dumps({'filename':'outside.md','markdown':md,'model':'test-model'}).encode()
        req=urllib.request.Request(self.base+'/api/generate/start',data=body,headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code,422)
        self.assertIn('无需单独选择图片',error.exception.read().decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
