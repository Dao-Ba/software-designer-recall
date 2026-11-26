#!/usr/bin/env python3
"""No-dependency Markdown cloze extractor; heuristic baseline, reviewed AI JSON can override."""
import argparse, hashlib, html, json, re
from pathlib import Path

STOP = {'功能', '组成', '分类', '控制', '过程', '地址', '数据', '指令', '内容', '方式', '系统', '用于', '包括', '以及',
        '相关', '基本'}


def plain (t):
	t = html.unescape (re.sub (r'&#x([0-9A-Fa-f]+);', lambda m: chr (int (m.group (1), 16)), t))
	t = re.sub (r'!?\[([^]]*)\]\([^)]*\)', r'\1', t)
	t = re.sub (r'<[^>]*>', '', t)
	t = re.sub (r'(?<!\w)(?:\*\*|__|==|`)(.*?)(?:\*\*|__|==|`)', r'\1', t)
	t = re.sub (r'\*\*|==|`|__', '', t)
	return re.sub (r'\s+', ' ', t).strip ()


def spans (t):
	hits = []
	for p in [r'\*\*(.+?)\*\*', r'==(.+?)==', r'`([^`]+)`']:
		hits += [plain (m.group (1)) for m in re.finditer (p, t)]
	return hits


def parse_md (text, source):
	section = source.stem;
	entries = [];
	code = False
	for ln, line in enumerate (text.splitlines (), 1):
		s = line.strip ()
		if s.startswith ('```') or s.startswith ('~~~'): code = not code;continue
		if code or not s or s.startswith ('![') or re.match (r'^[-|: ]{3,}$', s): continue
		h = re.match (r'^(#{1,6})\s+(.+)', s)
		if h: section = plain (h.group (2)); continue
		if s.startswith ('>'): s = s.lstrip ('> ').strip ()
		s = re.sub (r'^\s*(?:[-*+]\s+|\d+[.)]\s+)', '', s)
		if not s or len (s) < 10: continue
		if s.startswith ('|') and s.endswith ('|'):
			cells = [c.strip () for c in s.strip ('|').split ('|')]
			if len (cells) < 2 or all (re.fullmatch ('[:-]+', c or '-') for c in cells): continue
			s = '：'.join (cells[:2])
		body = plain (s)
		if len (body) < 14 or len (body) > 310: continue
		candidates = []
		pair = re.match (r'^(.{2,35}?)\s*[：:]\s*(.{5,})', body)
		if pair: candidates.append (pair.group (1).strip ())
		candidates += spans (s)
		candidates += re.findall (r'[\u4e00-\u9fffA-Za-z]{2,16}（[A-Za-z0-9]{2,8}）', body)
		ans = None
		for a in candidates:
			if 2 <= len (a) <= 26 and a in body and a not in STOP and not a.isdigit (): ans = a;break
		if not ans: continue
		start = body.find (ans);
		sentence = body[:start] + '【' + ans + '】' + body[start + len (ans):]
		key = hashlib.sha1 ((str (source.as_posix ()) + '|' + section + '|' + body + '|' + ans).encode ()).hexdigest ()[
			:16]
		entries.append (dict (id='note-' + key, section=section, answer=ans, sentence=sentence, source=str (source.as_posix ()), line=ln))
	return entries


def create_cards (paths):
	found = []
	for p in paths:
		try:
			found += parse_md (p.read_text (encoding='utf-8-sig'), p)
		except UnicodeDecodeError:
			print ('跳过非 UTF-8 文件:', p)
	unique = {x['id']: x for x in found};
	cards = list (unique.values ())
	pool = list (dict.fromkeys (c['answer'] for c in cards))
	for c in cards:
		# deterministic same-length, section-first distractor search; never claim semantic quality
		others = [x for x in cards if x['section'] == c['section'] and x['answer'] != c['answer']]
		others += [x for x in cards if x['section'] != c['section'] and x['answer'] != c['answer']]
		seen = {c['answer']};
		choices = []
		for x in sorted (others, key=lambda x: (abs (len (x['answer']) - len (c['answer'])), x['answer'])):
			a = x['answer']
			if a not in seen: choices.append (a);seen.add (a)
			if len (choices) == 3: break
		if len (choices) < 3: continue
		c['options'] = [c['answer']] + choices
		c['needs_review'] = True
	return [c for c in cards if len (c.get ('options', [])) == 4]


def main ():
	ap = argparse.ArgumentParser (description='从Typora Markdown生成离线挖空题库；干扰项需人工审核')
	ap.add_argument ('path', nargs='?', default='notes', help='.md 文件或目录')
	ap.add_argument ('--output', default='data/questions.json')
	a = ap.parse_args ();
	p = Path (a.path)
	paths = sorted (p.rglob ('*.md')) if p.is_dir () else ([p] if p.is_file () else [])
	if not paths: ap.error ('没有找到 Markdown 文件')
	cards = create_cards (paths);
	out = Path (a.output);
	out.parent.mkdir (parents=True, exist_ok=True)
	out.write_text (json.dumps ({'version': 2, 'generator': 'heuristic',
	                             'cards': cards}, ensure_ascii=False, indent=2), encoding='utf-8')
	print (f'已生成 {len (cards)} 条初稿 -> {out}；导入网页前请审查答案和干扰项。')


if __name__ == '__main__': main ()
