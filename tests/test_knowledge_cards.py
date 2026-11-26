import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import json
import base64
import sqlite3
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server
from ollama_engine import knowledge_units, balanced_knowledge_units, organize_knowledge
import ollama_engine
import app_config
import cpolar_client


class KnowledgeCardsTest(unittest.TestCase):
    def test_manual_image_saves_annotation_and_can_be_studied(self):
        with server.connect() as db:
            db.execute("INSERT INTO notes(name,digest,updated,content) VALUES ('网络.md','digest','now','## 结构图\n')")
        png = b'\x89PNG\r\n\x1a\n' + b'image bytes'
        result = server.add_manual_image('网络.md', '结构图', '示意.png', 'image/png',
                                         base64.b64encode(png).decode(), '这是图片说明')
        card = next(item for item in server.cards() if item['id'] == result['id'])
        self.assertEqual(card['kind'], 'image')
        self.assertEqual(card['annotation'], '这是图片说明')
        self.assertEqual(card['h2'], '结构图')
        self.assertEqual(server.rows('SELECT data FROM media WHERE id=?', (card['media_id'],))[0]['data'], png)
        self.assertEqual(server.rows('SELECT manual FROM cards WHERE id=?', (card['id'],))[0]['manual'], 1)
        self.assertEqual(server.answer(card['id'], '不了解', 'random')['correct'], False)
        self.assertTrue(next(item for item in server.cards() if item['id'] == card['id'])['wrong_marked'])
        with self.assertRaisesRegex(ValueError, '格式不符'):
            server.add_manual_image('网络.md', '结构图', '错误.png', 'image/png',
                                    base64.b64encode(b'not an image').decode())

    def test_manual_svg_image_is_saved_and_unsafe_svg_is_rejected(self):
        with server.connect() as db:
            db.execute("INSERT INTO notes(name,digest,updated,content) VALUES ('图示.md','digest','now','## 图示\n')")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="url(#shade)"/></svg>'
        result = server.add_manual_image('图示.md', '结构图', '结构.svg', 'image/svg+xml',
                                         base64.b64encode(svg).decode())
        card = next(item for item in server.cards() if item['id'] == result['id'])
        media = server.rows('SELECT mime,data FROM media WHERE id=?', (card['media_id'],))[0]
        self.assertEqual((media['mime'], media['data']), ('image/svg+xml', svg))
        for bad_svg in (b'<svg><script>alert(1)</script></svg>',
                        b'<svg onload="alert(1)"/>',
                        b'<svg><image href="https://example.com/pixel.png"/></svg>',
                        b'<!DOCTYPE svg><svg/>'):
            with self.assertRaisesRegex(ValueError, '格式不符'):
                server.add_manual_image('图示.md', '结构图', '错误.svg', 'image/svg+xml',
                                        base64.b64encode(bad_svg).decode())

    def test_exam_profile_keeps_question_banks_and_progress_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / 'config'
            config_dir.mkdir()
            profiles = []
            with patch.object(app_config, 'PROJECT_ROOT', root), patch.object(app_config, 'CONFIG_DIR', config_dir):
                for exam_id in ('first', 'second'):
                    (config_dir / 'exam.json').write_text(json.dumps({
                        'id': exam_id, 'title': exam_id,
                        'notes_dir': f'notes/{exam_id}',
                        'database': f'data/{exam_id}/study.sqlite3'}), encoding='utf-8')
                    profiles.append(app_config.exam_config())
                (config_dir / 'exam.json').write_text(json.dumps({
                    'id': 'invalid', 'title': '非法路径', 'notes_dir': '../outside',
                    'database': 'data/invalid/study.sqlite3'}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '相对路径'):
                    app_config.exam_config()
                (config_dir / 'exam.json').write_text(json.dumps({
                    'id': 'invalid', 'title': '非法数据库', 'notes_dir': 'notes/invalid',
                    'database': 'web/study.sqlite3'}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'data 目录'):
                    app_config.exam_config()
            for profile in profiles:
                with patch.object(server, 'DB', profile['database']):
                    server.init()
                    server.save_cards('相同文件名.md', '## 章节\n' + profile['id'] + ' 内容。\n', [
                        {'kind': 'knowledge', 'identity': profile['id'],
                         'original': profile['id'] + ' 内容。', 'answer': '理解',
                         'options': ['理解', '不理解'], 'section': '章节',
                         'h2': '章节', 'line': 2}])
                    if profile['id'] == 'first':
                        server.answer(server.cards()[0]['id'], '不理解', 'random')
            with patch.object(server, 'DB', profiles[0]['database']):
                first = server.cards()[0]
                self.assertEqual(first['original'], 'first 内容。')
                self.assertEqual(first['attempts'], 1)
            with patch.object(server, 'DB', profiles[1]['database']):
                second = server.cards()[0]
                self.assertEqual(second['original'], 'second 内容。')
                self.assertEqual(second['attempts'], 0)
            self.assertEqual(server.EXAM['database'], self.original_db)

    def test_note_lookup_prefers_active_bank_and_keeps_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / 'notes' / 'active'
            other = root / 'notes' / 'other'
            active.mkdir(parents=True)
            other.mkdir(parents=True)
            markdown = '## 标题\n相同的内容。\n'
            (active / '相同.md').write_text(markdown, encoding='utf-8')
            (other / '相同.md').write_text(markdown, encoding='utf-8')
            (other / '旧笔记.md').write_text(markdown, encoding='utf-8')
            with patch.object(server, 'ROOT', root), patch.object(server, 'NOTES_DIR', active):
                self.assertEqual(server.resolve_note_path('相同.md', markdown), (active / '相同.md').resolve())
                self.assertEqual(server.resolve_note_path('旧笔记.md', markdown), (other / '旧笔记.md').resolve())

    def test_host_backup_is_saved_on_server_and_is_independent_snapshot(self):
        markdown = '## 命令\n第一点。\n'
        server.save_cards('备份.md', markdown, [{'kind': 'knowledge', 'identity': 'first',
            'original': '第一点。', 'answer': '理解', 'options': ['理解', '不理解'],
            'section': '命令', 'h2': '命令', 'line': 2}])
        card_id = server.cards()[0]['id']
        first = server.save_host_backup()
        backup_path = Path(first['path'])
        self.assertEqual(backup_path.parent, server.DB.parent / 'backups')
        self.assertEqual(first['bytes'], backup_path.stat().st_size)
        with closing(sqlite3.connect(backup_path)) as saved:
            self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(saved.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 1)
            self.assertEqual(saved.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        server.answer(card_id, '不理解', 'random')
        second = server.save_host_backup()
        self.assertNotEqual(first['path'], second['path'])
        with closing(sqlite3.connect(backup_path)) as saved:
            self.assertEqual(saved.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        with closing(sqlite3.connect(second['path'])) as saved:
            self.assertEqual(saved.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_sqlite_backup_can_restore_cards_and_progress(self):
        server.save_cards('恢复.md', '## 命令\n第一点。\n', [{'kind': 'knowledge', 'identity': 'first',
            'original': '第一点。', 'answer': '理解', 'options': ['理解', '不理解'],
            'section': '命令', 'h2': '命令', 'line': 2}])
        card_id = server.cards()[0]['id']
        server.answer(card_id, '不理解', 'random')
        snapshot = Path(server.save_host_backup()['path']).read_bytes()
        server.delete_targets([{'note': '恢复.md'}], 'both')
        self.assertEqual(server.cards(), [])
        with self.assertRaisesRegex(ValueError, '有效 .sqlite3'):
            server.import_sqlite_backup(b'not a database')
        self.assertEqual(server.cards(), [])
        result = server.import_sqlite_backup(snapshot)
        self.assertEqual((result['notes'], result['cards'], result['attempts']), (1, 1, 1))
        self.assertTrue(Path(result['previous_database']).exists())
        self.assertEqual(server.cards()[0]['id'], card_id)
        self.assertEqual(server.cards()[0]['attempts'], 1)

    def test_merge_adjacent_images_keeps_flat_parts_and_annotations(self):
        with server.connect() as db:
            db.execute("INSERT INTO notes(name,digest,updated,content) VALUES ('合并.md','digest','now','')")
            for number in range(1, 4):
                db.execute('''INSERT INTO media(id,note,path,title,mime,data,annotation,created)
                              VALUES(?,?,?,?,?,?,?,?)''',
                           (f'm{number}', '合并.md', f'{number}.png', f'图 {number}', 'image/png',
                            b'png', f'注释 {number}', 'now'))
            for card_id, kind, line, media_id in [('text', 'knowledge', 1, None),
                                                   ('image-1', 'image', 2, 'm1'),
                                                   ('image-2', 'image', 3, 'm2'),
                                                   ('image-3', 'image', 4, 'm3'),
                                                   ('next-text', 'knowledge', 5, None)]:
                db.execute('''INSERT INTO cards(id,note,section,h2,original,answer,options,line,topic,
                              created,kind,media_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (card_id, '合并.md', '标题', '章节', card_id, '理解' if kind == 'knowledge' else '了解',
                            '["理解","不理解"]' if kind == 'knowledge' else '["了解","不了解"]',
                            line, '章节', 'now', kind, media_id))
        with self.assertRaisesRegex(ValueError, '上一张卡片'):
            server.merge_image_into_previous('image-2', 'text')
        server.merge_image_into_previous('image-2', 'image-1')
        server.merge_image_into_previous('image-1', 'text')
        server.merge_image_into_previous('image-3', 'text')
        visible = server.cards()
        self.assertEqual([card['id'] for card in visible], ['text', 'next-text'])
        self.assertEqual([part['id'] for part in visible[0]['merged_parts']],
                         ['text', 'image-1', 'image-2', 'image-3'])
        self.assertEqual([part['annotation'] for part in visible[0]['merged_parts'][1:]],
                         ['注释 1', '注释 2', '注释 3'])
        self.assertFalse(any('merged_parts' in part for part in visible[0]['merged_parts']))
        server.update_image_annotation('image-2', '更新后的注释')
        self.assertEqual(server.cards()[0]['merged_parts'][2]['annotation'], '更新后的注释')
        with self.assertRaisesRegex(ValueError, '只能合并'):
            server.merge_image_into_previous('next-text', 'text')
        server.merge_knowledge_into_previous('next-text', 'text')
        self.assertEqual([part['id'] for part in server.cards()[0]['merged_parts']],
                         ['text', 'image-1', 'image-2', 'image-3', 'next-text'])
        updates = [{'id': 'text', 'content': '修改后的文字', 'title': '新标题'},
                   {'id': 'image-1', 'annotation': '新注释一'},
                   {'id': 'image-2', 'annotation': '新注释二'},
                   {'id': 'image-3', 'annotation': ''},
                   {'id': 'next-text', 'content': '修改后的末段', 'title': '末段标题'}]
        server.edit_card_group('text', updates)
        edited = server.cards()[0]['merged_parts']
        self.assertEqual([edited[0]['original'], edited[1]['annotation'], edited[4]['original']],
                         ['修改后的文字', '新注释一', '修改后的末段'])
        invalid = [dict(part) for part in updates]
        invalid[0]['content'] = '不应保存'
        invalid[-1]['content'] = ''
        with self.assertRaisesRegex(ValueError, '需要内容'):
            server.edit_card_group('text', invalid)
        self.assertEqual(server.cards()[0]['original'], '修改后的文字')
        backup = Path(server.save_host_backup()['path']).read_bytes()
        self.assertEqual(len(server.rows('SELECT * FROM card_group_members')), 5)
        server.import_sqlite_backup(backup)
        self.assertEqual(len(server.cards()[0]['merged_parts']), 5)
        server.unmerge_card_group('text')
        self.assertEqual([card['id'] for card in server.cards()],
                         ['text', 'image-1', 'image-2', 'image-3', 'next-text'])
        server.merge_image_into_previous('image-1', 'text')
        server.merge_image_into_previous('image-2', 'text')
        server.merge_image_into_previous('image-3', 'text')
        server.merge_knowledge_into_previous('next-text', 'text')
        server.delete_item('text', 'knowledge')
        self.assertEqual(server.cards(), [])
        self.assertEqual(server.rows('SELECT id FROM media'), [])

    def test_configured_model_and_cpolar_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / 'model.json').write_text(json.dumps({
                'ollama_base_url': 'http://127.0.0.1:12345', 'default_model': 'example:latest'}), encoding='utf-8')
            (config_dir / 'cpolar.json').write_text(json.dumps({
                'api_base_url': 'http://127.0.0.1:9200', 'login_path': '/api/v1/user/login',
                'tunnels_path': '/api/v1/tunnels', 'username': 'user@example.com',
                'password': 'secret', 'tunnel_name': '软考'}), encoding='utf-8')
            with patch.object(app_config, 'CONFIG_DIR', config_dir), patch.object(ollama_engine, 'request_json', return_value={'models': [{'name': 'example:latest'}]}) as request:
                self.assertEqual(ollama_engine.models(), ['example:latest'])
                self.assertEqual(request.call_args.args[0], 'http://127.0.0.1:12345/api/tags')
            with patch.object(app_config, 'CONFIG_DIR', config_dir), patch.object(cpolar_client, '_request', side_effect=[
                {'data': {'token': 'test-token'}},
                {'data': {'items': [{'name': 'other', 'public_url': 'https://other.example'},
                                    {'name': '软考', 'public_url': 'http://study.example'},
                                    {'name': '软考', 'public_url': 'https://study.example'}]}}
            ]) as request:
                self.assertEqual(cpolar_client.tunnel_status()['public_url'], 'https://study.example')
                self.assertEqual(request.call_args.kwargs['token'], 'test-token')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = server.DB
        server.DB = Path(self.temp.name) / 'study.sqlite3'
        server.init()

    def tearDown(self):
        server.DB = self.original_db
        self.temp.cleanup()

    def test_bulk_card_removal_cleans_records_across_batches(self):
        ids = [f'bulk-{number}' for number in range(405)]
        with server.connect() as db:
            db.execute("INSERT INTO notes(name,digest,updated,content) VALUES ('批量.md','digest','now','')")
            db.executemany('''INSERT INTO cards(id,note,section,original,answer,options,topic,created)
                              VALUES (?, '批量.md', '章节', '内容', '理解', '["理解","不理解"]', '章节', 'now')''',
                           [(card_id,) for card_id in ids])
            db.executemany('INSERT INTO reviews(card_id,attempts) VALUES (?,1)', [(card_id,) for card_id in ids])
            db.executemany("INSERT INTO attempts(card_id,selected,correct,created) VALUES (?,'理解',1,'now')",
                           [(card_id,) for card_id in ids])
            db.execute("INSERT INTO app_meta(key,value) VALUES ('pinned_card_id',?)", (ids[-1],))
        self.assertEqual(server.delete_targets([{'note': '批量.md'}], 'both')['deleted_cards'], 405)
        self.assertEqual(server.rows('SELECT id FROM cards'), [])
        self.assertEqual(server.rows('SELECT card_id FROM reviews'), [])
        self.assertEqual(server.rows('SELECT card_id FROM attempts'), [])
        self.assertEqual(server.rows('SELECT name FROM notes'), [])
        self.assertIsNone(server.pinned_card_id())

    def test_h2_paragraphs_and_numbered_list_keep_all_content(self):
        markdown = ('# 网络\n## 网络命令\n这里介绍命令。\n\n'
                    'ipconfig 显示地址。\n\nipconfig /all 显示详细信息。\n\n'
                    '命令选项：\n\n1. 查看地址\n\n2. 查看网关\n\n3. 查看 DNS\n'
                    '## 第二部分\n一句话也要保留。\n')
        units = knowledge_units(markdown, '网络.md')
        self.assertEqual({u['h2'] for u in units}, {'网络命令', '第二部分'})
        self.assertEqual(len([u for u in units if u['h2'] == '第二部分']), 1)
        self.assertTrue(any(all(s in u['original'] for s in ('1. 查看地址', '2. 查看网关', '3. 查看 DNS')) for u in units))
        joined = '\n'.join(u['original'] for u in units)
        for phrase in ('这里介绍命令。', 'ipconfig 显示地址。', 'ipconfig /all 显示详细信息。', '一句话也要保留。'):
            self.assertIn(phrase, joined)

    def test_mermaid_fence_stays_whole_across_blank_lines(self):
        markdown = ("## 后缀式\n- 表达式：\n"
                    "    ```mermaid\n    flowchart TD\n        Root('+')\n\n"
                    "        Root --- NodeA\n    ```\n")
        units = knowledge_units(markdown, '语言.md')
        self.assertEqual(len(units), 1)
        self.assertIn("Root('+')\n\n        Root --- NodeA\n    ```", units[0]['original'])

    def test_import_and_review_without_ollama(self):
        markdown = '# 网络\n## IP 命令\nipconfig 显示本机网络配置。\n'
        started = server.start_generation('网络.md', markdown, None, False)
        for _ in range(100):
            state = server.JOBS[started['job_id']]
            if state['done']:
                break
            time.sleep(.01)
        self.assertTrue(state['done'])
        self.assertIsNone(state['error'])
        card = server.cards()[0]
        self.assertEqual(card['kind'], 'knowledge')
        self.assertEqual(card['original'], 'ipconfig 显示本机网络配置。')
        self.assertEqual(card['options'], ['理解', '不理解'])
        answered = server.answer(card['id'], '不理解', 'random')
        self.assertFalse(answered['correct'])
        self.assertEqual(answered['card']['id'], card['id'])
        self.assertEqual(answered['card']['attempts'], 1)
        self.assertEqual(answered['stats']['studied'], 1)
        self.assertTrue(server.cards()[0]['wrong_marked'])
        server.answer(card['id'], '不理解', 'wrong')
        self.assertTrue(server.cards()[0]['round1_done'])
        server.answer(card['id'], '不理解', 'retry')
        self.assertTrue(server.cards()[0]['persistent_unknown'])
        self.assertTrue(server.cards()[0]['retry_deferred'])
        self.assertEqual(server.release_retry_batch(), {'released': 1})
        self.assertFalse(server.cards()[0]['retry_deferred'])
        server.update_flags(card['id'], {'wrong_marked': False, 'favorite': False})
        self.assertFalse(server.cards()[0]['persistent_unknown'])

    def test_review_downgrades_one_stage_and_returns_to_random(self):
        item = {'kind': 'knowledge', 'identity': 'review-flow', 'original': '地址配置。',
                'answer': '理解', 'options': ['理解', '不理解'], 'section': '命令',
                'h2': '命令', 'line': 2}
        server.save_cards('网络.md', '## 命令\n地址配置。', [item])
        card_id = server.cards()[0]['id']
        server.answer(card_id, '不理解', 'random')
        self.assertTrue(server.cards()[0]['wrong_marked'])
        server.answer(card_id, '不理解', 'wrong')
        self.assertTrue(server.cards()[0]['round1_done'])
        server.answer(card_id, '理解', 'retry')
        card = server.cards()[0]
        self.assertFalse(card['round1_done'])
        self.assertTrue(card['wrong_marked'])
        self.assertFalse(card['random_return_pending'])
        server.answer(card_id, '理解', 'wrong')
        card = server.cards()[0]
        self.assertFalse(card['wrong_marked'])
        self.assertFalse(card['favorite'])
        self.assertTrue(card['random_return_pending'])
        server.answer(card_id, '理解', 'random')
        self.assertFalse(server.cards()[0]['random_return_pending'])
        flagged = server.update_flags(card_id, {'favorite': True})
        self.assertTrue(flagged['card']['favorite'])
        self.assertEqual(flagged['stats']['favorites'], 1)
        self.assertTrue(server.cards()[0]['favorite'])
        server.update_flags(card_id, {'favorite': False})
        self.assertTrue(server.cards()[0]['random_return_pending'])

    def test_long_paragraph_is_split_without_losing_sentences(self):
        sentences = [f'第{i}项内容必须保留。' for i in range(300)]
        units = knowledge_units('## 长段落\n' + ''.join(sentences), '长文.md')
        self.assertGreater(len(units), 1)
        joined = ''.join(u['original'].replace('\n', '') for u in units)
        self.assertEqual(joined, ''.join(sentences))

    def test_balancing_reduces_extreme_lengths_and_keeps_every_source_character(self):
        note = Path(__file__).resolve().parents[1] / 'notes' / '软考笔记' / '结构化开发与方法.md'
        markdown = note.read_text(encoding='utf-8-sig')
        raw = knowledge_units(markdown, note.name)
        balanced, warnings = balanced_knowledge_units(markdown, note.name)
        self.assertEqual(warnings, [])
        self.assertLessEqual(sum(len(u['original']) < 100 for u in balanced),
                             sum(len(u['original']) < 100 for u in raw))
        compact = lambda items: ''.join(''.join(u['original'].split()) for u in items)
        self.assertEqual(compact(raw), compact(balanced))

    def test_model_returns_only_boundaries_and_failure_falls_back(self):
        markdown = '## 主题\n' + '\n'.join(f'- 内容{i}：' + '详细说明。' * 40 for i in range(8))
        with patch.object(ollama_engine, 'request_json', return_value={'message': {'content': '{"break_after":[1,3,5]}'}}):
            modelled, warnings = balanced_knowledge_units(markdown, '测试.md', 'qwen3:8b')
        self.assertEqual(warnings, [])
        self.assertGreater(len(modelled), 1)
        with patch.object(ollama_engine, 'request_json', side_effect=TimeoutError('model timeout')):
            fallback, warnings = balanced_knowledge_units(markdown, '测试.md', 'qwen3:8b')
        self.assertEqual(len(warnings), 1)
        compact = lambda items: ''.join(''.join(u['original'].split()) for u in items)
        self.assertEqual(compact(modelled), compact(fallback))

    def test_local_file_picker_ignores_selected_images(self):
        directory = Path(self.temp.name)
        md = directory / '网络.md'
        image = directory / '图.png'
        md.write_text('## 网络\nipconfig 显示地址。\n', encoding='utf-8')
        image.write_bytes(b'picture')
        response = type('Result', (), {'returncode': 0, 'stdout': json.dumps([str(md), str(image)]), 'stderr': ''})()
        with patch.object(server.subprocess, 'run', return_value=response):
            picked = server.choose_notes()
        self.assertEqual(picked['files'], ['网络.md'])
        self.assertEqual(picked['ignored'], 1)

    def test_import_queue_persists_and_completes_each_file(self):
        directory = Path(self.temp.name)
        paths = []
        for name in ('网络.md', '操作系统.md'):
            path = directory / name
            path.write_text(f'## {path.stem}\n{name} 的完整知识点。\n', encoding='utf-8')
            paths.append(path)
        server.start_import_queue([{'name': p.name, 'path': str(p)} for p in paths], None, False)
        for _ in range(100):
            state = server.import_queue_status()
            if not state['pending']:
                break
            time.sleep(.02)
        self.assertEqual(state['pending'], [])
        self.assertEqual(len(state['completed']), 2)
        self.assertEqual({c['note'] for c in server.cards()}, {p.name for p in paths})

    def test_changed_split_version_regenerates_unchanged_note(self):
        markdown = '## 网络\nipconfig 显示地址。\n'
        first = server.start_generation('网络.md', markdown, None, False)
        for _ in range(100):
            if server.JOBS[first['job_id']]['done']:
                break
            time.sleep(.01)
        self.assertIsNone(server.JOBS[first['job_id']]['error'])
        self.assertTrue(server.start_generation('网络.md', markdown, None, True)['skipped'])
        with server.connect() as db:
            db.execute("UPDATE notes SET processor='old-version' WHERE name='网络.md'")
        third = server.start_generation('网络.md', markdown, None, True)
        self.assertIn('job_id', third)
        for _ in range(100):
            if server.JOBS[third['job_id']]['done']:
                break
            time.sleep(.01)
        self.assertIsNone(server.JOBS[third['job_id']]['error'])

    def test_image_is_separate_card_and_edit_delete_card(self):
        directory = Path(self.temp.name)
        (directory / 'img').mkdir()
        (directory / 'img' / 'diagram.png').write_bytes(b'\x89PNG\r\n\x1a\nexample')
        note = directory / '网络.md'
        markdown = '# 网络\n## 结构图\n这是一张结构图。\n<img src="./img/diagram.png">\n'
        note.write_text(markdown, encoding='utf-8')
        generated = organize_knowledge(markdown, note.name)
        extras, media, warnings = server.material_cards(markdown, note.name, note)
        self.assertEqual(warnings, [])
        server.save_cards(note.name, markdown, generated['cards'] + extras, media_items=media, source_path=note)
        cards = server.cards()
        self.assertEqual({c['kind'] for c in cards}, {'knowledge', 'image'})
        image = next(c for c in cards if c['kind'] == 'image')
        self.assertEqual(image['h2'], '结构图')
        self.assertEqual(image['options'], ['了解', '不了解'])
        server.update_image_annotation(image['id'], '结构说明：$R+1$')
        self.assertEqual(next(c for c in server.cards() if c['id'] == image['id'])['annotation'], '结构说明：$R+1$')
        server.save_cards(note.name, markdown, generated['cards'] + extras, media_items=media, source_path=note)
        self.assertEqual(next(c for c in server.cards() if c['id'] == image['id'])['annotation'], '结构说明：$R+1$')
        item = next(c for c in cards if c['kind'] == 'knowledge')
        server.answer(item['id'], '不理解', 'random')
        server.edit_knowledge_card(item['id'], '修改后的完整知识点。', '新的卡片标题')
        self.assertEqual(server.knowledge()[0]['content'], '修改后的完整知识点。')
        updated = next(c for c in server.cards() if c['id'] == item['id'])
        self.assertEqual((updated['section'], updated['h2'], updated['attempts']), ('新的卡片标题', '结构图', 1))
        server.save_cards(note.name, markdown, generated['cards'] + extras, media_items=media, source_path=note)
        self.assertEqual(next(c for c in server.cards() if c['id'] == item['id'])['section'], '新的卡片标题')
        with self.assertRaisesRegex(ValueError, '标题不能为空'):
            server.edit_knowledge_card(item['id'], '仍有内容', '  ')
        with self.assertRaisesRegex(ValueError, '卡片类型已变化'):
            server.delete_item(image['id'], 'knowledge')
        server.delete_item(item['id'], 'knowledge')
        self.assertEqual([c['kind'] for c in server.cards()], ['image'])

        self.assertEqual(server.toggle_pin(image['id']), {'pinned_id': image['id']})
        server.init()
        self.assertEqual(server.pinned_card_id(), image['id'])
        self.assertFalse(server.answer(image['id'], '不了解')['correct'])
        self.assertEqual(server.cards()[0]['wrong'], 1)
        server.update_flags(image['id'], {'favorite': True})
        self.assertEqual(server.cards()[0]['favorite'], 1)
        self.assertEqual(server.toggle_pin(image['id']), {'pinned_id': None})
        server.toggle_pin(image['id'])
        server.delete_item(image['id'])
        self.assertIsNone(server.pinned_card_id())

    def test_image_uses_nearest_active_heading_and_updates_existing_card(self):
        directory = Path(self.temp.name)
        (directory / 'img').mkdir()
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            (directory / 'img' / name).write_bytes(b'PNG image')
        note = directory / '图示.md'
        markdown = ('## 第二级\n![a](./img/a.png)\n### 第三级\n#### 第四级\n'
                    '##### 第五级\n<img src="./img/b.png">\n#### 新第四级\n'
                    '![c](./img/c.png)\n## 新第二级\n![d](./img/d.png)\n')
        note.write_text(markdown, encoding='utf-8')
        refs = ollama_engine.image_references(markdown, note.name)
        self.assertEqual([r['title'] for r in refs], ['第二级', '第五级', '新第四级', '新第二级'])
        self.assertEqual([r['h2'] for r in refs], ['第二级', '第二级', '第二级', '新第二级'])
        extras, media, warnings = server.material_cards(markdown, note.name, note)
        self.assertEqual(warnings, [])
        server.save_cards(note.name, markdown, extras, media_items=media, source_path=note)
        image = next(c for c in server.cards() if c['media_path'] == './img/b.png')
        self.assertEqual((image['original'], image['h2']), ('第五级', '第二级'))
        server.answer(image['id'], '不了解', 'random')
        with server.connect() as db:
            db.execute("UPDATE cards SET original='旧标题',section='旧标题',h2='旧标题' WHERE id=?", (image['id'],))
        server.init()
        refreshed = next(c for c in server.cards() if c['id'] == image['id'])
        self.assertEqual((refreshed['original'], refreshed['h2'], refreshed['attempts']),
                         ('第五级', '第二级', 1))

    def test_split_card_at_multiple_marks_with_optional_titles_and_history(self):
        markdown = '## 网络命令\nipconfig、ping、tracert 都是常用命令。\n'
        generated = organize_knowledge(markdown, '网络.md')['cards']
        server.save_cards('网络.md', markdown, generated)
        card = server.cards()[0]
        server.answer(card['id'], '不理解', 'random')
        original = card['original']
        parts = [
            {'content': 'ipconfig、', 'title': '地址配置'},
            {'content': 'ping、', 'title': ''},
            {'content': 'tracert 都是常用命令。', 'title': '路由追踪'},
        ]
        self.assertEqual(''.join(p['content'] for p in parts), original)
        result = server.split_knowledge_card(card['id'], original, parts)
        self.assertEqual(len(result['new_ids']), 2)
        cards = {c['id']: c for c in server.cards()}
        self.assertEqual(cards[card['id']]['section'], '地址配置')
        self.assertEqual(cards[card['id']]['attempts'], 1)
        self.assertTrue(cards[card['id']]['wrong_marked'])
        self.assertEqual([cards[i]['section'] for i in result['new_ids']], ['网络命令', '路由追踪'])
        self.assertTrue(all(cards[i]['attempts'] == 0 for i in result['new_ids']))
        self.assertTrue(all(cards[i]['wrong_marked'] and not cards[i]['round1_done'] for i in result['new_ids']))
        server.save_cards('网络.md', markdown, generated)
        self.assertEqual(len(server.cards()), 3)
        self.assertEqual(next(c for c in server.cards() if c['id'] == card['id'])['section'], '地址配置')
        with self.assertRaises(ValueError):
            server.split_knowledge_card(card['id'], original, parts)

    def test_split_card_preserves_review_stage_and_favorite_on_new_parts(self):
        markdown = '## 网络命令\n第一句。第二句。\n'
        server.save_cards('网络.md', markdown, organize_knowledge(markdown, '网络.md')['cards'])
        card = server.cards()[0]
        server.answer(card['id'], '不理解', 'random')
        server.update_flags(card['id'], {'favorite': True})
        server.answer(card['id'], '不理解', 'wrong')
        result = server.split_knowledge_card(card['id'], card['original'], [
            {'content': '第一句。', 'title': '第一部分'},
            {'content': '第二句。', 'title': '第二部分'},
        ])
        original, new_part = (next(item for item in server.cards() if item['id'] == item_id)
                              for item_id in (card['id'], result['new_ids'][0]))
        self.assertEqual(new_part['attempts'], 0)
        for field in ('favorite', 'wrong_marked', 'round1_done', 'favorite_at', 'wrong_marked_at'):
            self.assertEqual(new_part[field], original[field])

    def test_split_card_can_edit_text_while_splitting(self):
        markdown = '## 命令\n第一句。第二句。\n'
        server.save_cards('网络.md', markdown, organize_knowledge(markdown, '网络.md')['cards'])
        card = server.cards()[0]
        server.answer(card['id'], '不理解', 'random')
        parts = [
            {'content': '第一句已补充。', 'title': '第一部分'},
            {'content': '第二句已改写。', 'title': '第二部分'},
        ]
        result = server.split_knowledge_card(card['id'], card['original'], parts)
        cards = {item['id']: item for item in server.cards()}
        self.assertEqual(cards[card['id']]['original'], '第一句已补充。')
        self.assertEqual(cards[card['id']]['attempts'], 1)
        self.assertEqual(cards[result['new_ids'][0]]['original'], '第二句已改写。')
        self.assertEqual(cards[result['new_ids'][0]]['attempts'], 0)
        with self.assertRaisesRegex(ValueError, '内容已变化'):
            server.split_knowledge_card(card['id'], card['original'], parts)

    def test_repeated_split_keeps_source_order(self):
        text = '第一句。第二句。第三句。'
        server.save_cards('顺序.md', '## 标题\n'+text,
                          [{'kind': 'knowledge', 'identity': 'whole', 'original': text,
                            'answer': '理解', 'options': ['理解', '不理解'],
                            'section': '标题', 'h2': '标题', 'line': 2}])
        original = server.cards()[0]
        server.split_knowledge_card(original['id'], text, [
            {'content': '第一句。', 'title': '标题'},
            {'content': '第二句。', 'title': '标题'},
            {'content': '第三句。', 'title': '标题'},
        ])
        server.split_knowledge_card(original['id'], '第一句。', [
            {'content': '第一', 'title': '标题'},
            {'content': '句。', 'title': '标题'},
        ])
        self.assertEqual([c['original'] for c in server.cards()],
                         ['第一', '句。', '第二句。', '第三句。'])
        with server.connect() as db:
            for order, card in enumerate(reversed(server.cards())):
                db.execute('UPDATE cards SET sort_order=NULL,created=? WHERE id=?',
                           (f'2020-01-01T00:00:0{order}', card['id']))
        server.init()
        self.assertEqual([c['original'] for c in server.cards()],
                         ['第一', '句。', '第二句。', '第三句。'])

    def test_merge_into_previous_keeps_text_marks_and_attempts(self):
        markdown = '## 命令\n第一点。\n第二点。\n第三点。\n'
        items = [{'kind': 'knowledge', 'identity': f'item:{i}', 'original': text,
                  'answer': '理解', 'options': ['理解', '不理解'], 'section': '命令',
                  'h2': '命令', 'line': i + 2}
                 for i, text in enumerate(('第一点。', '第二点。', '第三点。'))]
        server.save_cards('网络.md', markdown, items)
        first, second, third = server.cards()
        server.answer(first['id'], '理解', 'random')
        server.answer(second['id'], '不理解', 'random')
        server.update_flags(second['id'], {'favorite': True})
        server.answer(second['id'], '不理解', 'wrong')
        with self.assertRaises(ValueError):
            server.merge_knowledge_into_previous(third['id'], first['id'])
        result = server.merge_knowledge_into_previous(second['id'], first['id'])
        self.assertEqual(result['id'], first['id'])
        cards = server.cards()
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]['original'], '第一点。\n\n第二点。')
        self.assertEqual((cards[0]['attempts'], cards[0]['wrong']), (3, 2))
        self.assertTrue(cards[0]['favorite'])
        self.assertTrue(cards[0]['wrong_marked'])
        self.assertTrue(cards[0]['round1_done'])
        self.assertEqual(server.rows('SELECT COUNT(*) AS n FROM attempts WHERE card_id=?', (first['id'],))[0]['n'], 3)
        self.assertEqual(cards[1]['id'], third['id'])

    def test_merge_uses_any_child_mark_and_retry_progress(self):
        with server.connect() as db:
            db.execute("INSERT INTO notes(name,digest,updated,content) VALUES ('进度.md','digest','now','')")
            db.execute("INSERT INTO media(id,note,path,title,mime,data,created) VALUES ('media-1','进度.md','1.png','图一','image/png',X'89504E47','now')")
            for card_id, kind, line, media_id in [('root', 'knowledge', 1, None),
                                                   ('marked-image', 'image', 2, 'media-1')]:
                db.execute('''INSERT INTO cards(id,note,section,h2,original,answer,options,line,topic,
                              created,kind,media_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (card_id, '进度.md', '章节', '章节', card_id,
                            '理解' if kind == 'knowledge' else '了解',
                            '["理解","不理解"]' if kind == 'knowledge' else '["了解","不了解"]',
                            line, '章节', 'now', kind, media_id))
        server.answer('marked-image', '不了解', 'random')
        server.update_flags('marked-image', {'favorite': True})
        server.answer('marked-image', '不了解', 'wrong')
        server.answer('marked-image', '不了解', 'retry')
        server.merge_image_into_previous('marked-image', 'root')
        merged = server.cards()[0]
        self.assertTrue(merged['wrong_marked'])
        self.assertTrue(merged['favorite'])
        self.assertTrue(merged['round1_done'])
        self.assertTrue(merged['persistent_unknown'])
        self.assertFalse(merged['random_return_pending'])
        self.assertEqual(merged['attempts'], 3)
        self.assertTrue(merged['wrong_marked_at'])
        self.assertTrue(merged['favorite_at'])

    def test_merge_keeps_unstudied_unmarked_content_in_random_pool(self):
        markdown = '## 命令\n第一点。\n第二点。\n'
        items = [{'kind': 'knowledge', 'identity': f'item:{i}', 'original': text,
                  'answer': '理解', 'options': ['理解', '不理解'], 'section': '命令',
                  'h2': '命令', 'line': i + 2}
                 for i, text in enumerate(('第一点。', '第二点。'))]
        server.save_cards('网络.md', markdown, items)
        first, second = server.cards()
        server.answer(first['id'], '理解', 'random')
        server.merge_knowledge_into_previous(second['id'], first['id'])
        merged = server.cards()[0]
        self.assertEqual(merged['attempts'], 1)
        self.assertTrue(merged['random_return_pending'])
        self.assertFalse(merged['wrong_marked'])
        self.assertFalse(merged['favorite'])

    def test_sqlite_backup_restores_notes_images_annotations_and_history(self):
        markdown = '## 结构图\n这里是知识点。\n![示意图](./img/demo.png)\n'
        items = [
            {'kind': 'knowledge', 'identity': 'text', 'original': '这里是知识点。',
             'answer': '理解', 'options': ['理解', '不理解'], 'section': '结构图', 'h2': '结构图', 'line': 2},
            {'kind': 'image', 'identity': 'image', 'original': '示意图',
             'answer': '了解', 'options': ['了解', '不了解'], 'section': '结构图', 'h2': '结构图',
             'line': 3, 'media_id': 'media-demo'},
        ]
        server.save_cards('网络.md', markdown, items,
                          media_items=[('media-demo', '网络.md', './img/demo.png', '示意图',
                                        'image/png', b'picture bytes', server.now())])
        with server.connect() as db:
            db.execute("UPDATE media SET annotation='图片说明' WHERE id='media-demo'")
        text_card = next(item for item in server.cards() if item['kind'] == 'knowledge')
        server.answer(text_card['id'], '不理解', 'random')
        server.toggle_pin(text_card['id'])
        backup = Path(server.save_host_backup()['path']).read_bytes()
        with server.connect() as db:
            db.execute('DELETE FROM attempts')
            db.execute('DELETE FROM reviews')
            db.execute('DELETE FROM cards')
            db.execute('DELETE FROM media')
            db.execute('DELETE FROM notes')
        result = server.import_sqlite_backup(backup)
        self.assertEqual(result['cards'], 2)
        self.assertEqual(result['media'], 1)
        self.assertTrue(Path(result['previous_database']).is_file())
        self.assertEqual(server.rows('SELECT content FROM notes')[0]['content'], markdown)
        self.assertEqual(server.rows('SELECT annotation,data FROM media')[0],
                         {'annotation': '图片说明', 'data': b'picture bytes'})
        self.assertEqual(server.cards()[0]['attempts'], 1)
        self.assertEqual(server.pinned_card_id(), text_card['id'])
        self.assertEqual({item['note'] for item in server.cards()}, {'网络.md'})
        self.assertEqual({item['h2'] for item in server.cards()}, {'结构图'})



if __name__ == '__main__':
    unittest.main()
