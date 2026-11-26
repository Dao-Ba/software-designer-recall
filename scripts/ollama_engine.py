"""Offline local Ollama cloze generator, Python standard library only."""
import json, re, hashlib, urllib.request, urllib.error
from pathlib import Path
from build import plain
from app_config import model_config

BATCH_SIZE = 6
CHECKPOINT_VERSION = 'knowledge-boundaries-v2'
SCHEMA = {"type": "object", "properties": {"cards": {"type": "array", "items": {"type": "object", "properties": {
	"original": {"type": "string"}, "answer": {"type": "string"},
	"distractors": {"type": "array", "items": {"type": "string"}}, "section": {"type": "string"}},
                                                                                "required": ["original", "answer",
                                                                                             "distractors",
                                                                                             "section"]}}},
          "required": ["cards"]}
SYSTEM = '''你是软件资格考试出题人。根据笔记原文生成挖空选择题。每段【原文】是同一知识点的完整上下文，可能包含多句或相邻列表项。题干必须保留整个【原文】，只挖掉其中一个连续的答案片段；不能只截取尾句、半句或单独公式，不能改写、概括、合并不同【原文】或补充事实。原文中的“其”“该”“前款”“上述”等指代必须能从同一道题的其余文字找到所指对象，否则跳过该考点。挖空后，题干仍须有足够上下文，让读者不看笔记也知道问什么。逐个考查术语、条件、步骤、数字、作用或对比点；内容多的段落可以用同一完整原文出多道题，每题只挖一处。original必须与一个【原文】字段完全一致，answer必须在original中恰好出现一次。为每个答案提供三个不同、同类且合理的干扰项。无法形成完整、明确、四选一题目的内容直接跳过。仅输出JSON。'''
POINT_SCHEMA = {'type': 'object', 'properties': {'points': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['points']}


def analyze_points (text, model):
	prompt = '从以下笔记段落提取可独立复习的知识点。每条用完整、准确的一句话表达，只使用原文的信息，不补充推测或新事实；保留关键术语。返回 JSON 的 points 数组，最多 20 条。\n\n' + text
	reply = request_json (model_config ()['ollama_base_url'] + '/api/chat', method='POST', data={'model': model, 'stream': False,
	                                                               'format': POINT_SCHEMA,
	                                                               'options': {'temperature': 0.1}, 'messages': [
			{'role': 'user', 'content': prompt}]}, timeout=240)
	try:
		raw = json.loads (reply.get ('message', {}).get ('content', '{}')).get ('points', [])
	except (ValueError, TypeError):
		raw = []
	points = []
	for value in raw if isinstance (raw, list) else []:
		if isinstance (value, str):
			point = ' '.join (value.split ())
			if 8 <= len (point) <= 500 and point not in points: points.append (point)
	if not points: raise ValueError ('模型未提取到有效知识点，请检查输入文本或更换模型')
	return points[:20]


def request_json (url, method='GET', data=None, timeout=120):
	payload = None if data is None else json.dumps (data, ensure_ascii=False).encode ('utf-8')
	req = urllib.request.Request (url, data=payload, headers={'Content-Type': 'application/json'}, method=method)
	try:
		with urllib.request.urlopen (req, timeout=timeout) as r:
			return json.loads (r.read ().decode ('utf-8'))
	except urllib.error.HTTPError as e:
		raise RuntimeError (f'Ollama HTTP {e.code}: {e.read ().decode ("utf-8", "replace")[:240]}') from e
	except urllib.error.URLError as e:
		raise RuntimeError ('无法连接 Ollama，请检查 config/model.json 中的 ollama_base_url 并启动服务') from e


def models ():
	data = request_json (model_config ()['ollama_base_url'] + '/api/tags', timeout=5)
	return [x.get ('name', '') for x in data.get ('models', []) if x.get ('name')]


def source_rows (markdown, filename):
	section = Path (filename).stem;
	h2 = '未分组';
	h2_start = 0;
	result = [];
	code = False
	example_lines = {n for block in example_blocks (markdown, filename) for n in
	                 range (block['start'], block['end'] + 1)}
	for line_no, line in enumerate (markdown.splitlines (), 1):
		if line_no in example_lines: continue
		s = line.strip ()
		if s.startswith (('```', '~~~')): code = not code;continue
		if code or not s or s.startswith ('![') or '<img' in s.lower () or re.match (r'^\|?\s*:?-{3,}', s): continue
		if re.fullmatch (r'[\s:：|\-]+', s) or re.search (r'data:image|%3Csvg|viewBox%3D', s, re.I): continue
		head = re.match (r'^(#{1,6})\s+(.+)', s)
		if head:
			section = plain (head.group (2))
			if len (head.group (1)) == 1:
				h2 = '未分组';h2_start = 0
			elif len (head.group (1)) == 2:
				h2 = section;h2_start = line_no
			continue
		s = re.sub (r'^>\s*', '', s)
		s = re.sub (r'^(?:[-*+]\s+|\d+[.)]\s+)', '', s)
		if s.startswith ('|') and s.endswith ('|'):
			cells = [plain (c.strip ()) for c in s.strip ('|').split ('|')]
			if len (cells) >= 2: s = cells[0] + '：' + cells[1]
		s = plain (s)
		if len (s) >= 6: result.append ({'section': section, 'h2': h2, 'h2_start': h2_start, 'original': s,
		                                 'line': line_no, 'file': filename})
	return result


def question_rows (markdown, filename):
	result = [];
	group = []
	
	def flush ():
		if group:
			first = group[0]
			text = ' '.join (item['original'] for item in group)
			if 15 <= len (text) <= 1000: result.append (dict (first, original=text))
			group.clear ()
	
	for row in source_rows (markdown, filename):
		if group and (row['h2_start'] != group[-1]['h2_start'] or row['line'] > group[-1]['line'] + 2 or
		              len (' '.join (item['original'] for item in group)) + len (row['original']) + 1 > 700):
			flush ()
		group.append (row)
	flush ()
	return result


def knowledge_units(markdown, filename):
	"""Keep every note line while grouping adjacent paragraphs and enumerated lists."""
	sections=[]; title='未分组'; body=[]; fenced=False
	def finish_section():
		if body: sections.append((title,list(body)))
		body.clear()
	for line_no, raw in enumerate(markdown.splitlines(),1):
		if raw.strip().startswith(('```','~~~')): fenced=not fenced
		match=None if fenced else re.match(r'^##\s+(.+)',raw.strip())
		if match:
			finish_section(); title=plain(match.group(1))
			continue
		if not fenced and re.match(r'^#\s+',raw.strip()):continue
		# An image is saved as its own card by material_cards.
		text=re.sub(r'<img\b[^>]*>', '',raw,flags=re.I)
		text=re.sub(r'!\[[^\]]*\]\((?:<[^>]+>|[^)]*)\)', '',text).rstrip()
		body.append((line_no,text))
	finish_section()
	result=[]
	list_pattern=re.compile(r'^\s*(?:[-*+]\s+|\d+[.)、]\s*|[（(][一二三四五六七八九十\d]+[）)]\s*)')
	for heading,lines in sections:
		paragraphs=[];paragraph=[];inside_fence=False
		for line_no,text in lines:
			if not text.strip() and not inside_fence:
				if paragraph:paragraphs.append(paragraph);paragraph=[]
			else:paragraph.append((line_no,text))
			if text.strip().startswith(('```','~~~')):inside_fence=not inside_fence
		if paragraph:paragraphs.append(paragraph)
		# Only long prose is divided further; numbered lists remain one knowledge unit.
		expanded=[]
		for para in paragraphs:
			if sum(len(text) for _,text in para)<=1500 or list_pattern.match(para[0][1]):
				expanded.append(para);continue
			part=[];size=0
			for line_no,text in para:
				pieces=re.split(r'(?<=[。！？；;])',text) if len(text)>1200 else [text]
				for piece in pieces:
					if not piece:continue
					if part and size+len(piece)>1200:
						expanded.append(part);part=[];size=0
					part.append((line_no,piece));size+=len(piece)
			if part:expanded.append(part)
		paragraphs=expanded
		group=[]
		def emit():
			if not group:return
			flatten=[item for paragraph in group for item in paragraph]
			content='\n'.join(text for _,text in flatten).strip()
			if content:
				result.append({'section':heading,'h2':heading,'line':flatten[0][0],
				               'line_map':[line_no for line_no,_ in flatten],
				               'original':content,'file':filename})
			group.clear()
		for para in paragraphs:
			para_text='\n'.join(text for _,text in para)
			is_list=bool(list_pattern.match(para[0][1]))
			prior_text='\n'.join(text for block in group for _,text in block)
			prior_list=bool(group and list_pattern.match(group[-1][0][1]))
			join=bool(group and (is_list or prior_list or prior_text.rstrip().endswith(('：',':')) or len(prior_text)<20))
			if group and (not join or (len(prior_text)+len(para_text)>1500 and not (is_list or prior_list))):emit()
			group.append(para)
		emit()
	return result


def split_large_unit(unit):
	"""Expose top-level Markdown items as movable blocks without rewriting them."""
	text=unit['original']
	if len(text)<=1600 or text.startswith(('```','~~~')):return [unit]
	numbers=[int(n) for n in re.findall(r'(?m)^\s*(\d+)[.)、]\s+',text)]
	if len(text)<=4000 and len(numbers)>=2 and numbers==list(range(numbers[0],numbers[0]+len(numbers))):
		return [unit]
	lines=text.split('\n'); blocks=[]; block=[]
	item_start=re.compile(r'^(?:[-*+]\s+|\d+[.)、]\s+|###\s+)')
	for offset,line in enumerate(lines):
		if block and item_start.match(line):
			blocks.append(block);block=[]
		block.append((offset,line))
	if block:blocks.append(block)
	if len(blocks)==1:
		blocks=[];block=[];size=0
		for offset,line in enumerate(lines):
			if block and size+len(line)>1000:
				blocks.append(block);block=[];size=0
			block.append((offset,line));size+=len(line)
		if block:blocks.append(block)
	if len(blocks)==1:return [unit]
	return [dict(unit,original='\n'.join(line for _,line in block),
	             line=unit.get('line_map',[unit['line']+i for i in range(len(lines))])[block[0][0]]) for block in blocks]


def join_units(group):
	return dict(group[0],original='\n\n'.join(item['original'] for item in group))


def rule_groups(atoms):
	"""Balance short blocks while avoiding giant cards when no model is selected."""
	groups=[];current=[];size=0
	for atom in atoms:
		length=len(atom['original'])
		if current and size>=300 and size+length>1100:
			groups.append(current);current=[];size=0
		current.append(atom);size+=length
		if size>=1800:
			groups.append(current);current=[];size=0
	if current:
		if groups and size<160 and sum(len(a['original']) for a in groups[-1])+size<=1500:
			groups[-1].extend(current)
		else:groups.append(current)
	return groups


BOUNDARY_SCHEMA={'type':'object','properties':{'break_after':{'type':'array','items':{'type':'integer'}}},'required':['break_after']}


def model_groups(atoms, heading, model):
	if len(atoms)<2:return [atoms]
	material='\n'.join(f'[{i}] {atom["original"]}' for i,atom in enumerate(atoms))
	prompt=('你只负责将同一二级标题下的相邻原文块分成适合复习的知识点。'
	        '不能改写或删去任何块，不能调换顺序。短的相关块合并；不同主题分开；'
	        '带说明的编号列表尽量保持完整。单张通常约 200—1200 字，必要时可以更长。'
	        '仅返回 JSON 对象 {"break_after":[索引,...]}，索引表示在该编号原文块之后切开；'
	        '不要包含最后一个块的索引。二级标题：'+heading+'\n'+material)
	reply=request_json(model_config ()['ollama_base_url']+'/api/chat',method='POST',data={'model':model,'stream':False,'think':False,
		'format':BOUNDARY_SCHEMA,'options':{'temperature':0.1,'num_predict':160},
		'messages':[{'role':'user','content':prompt}]},timeout=90)
	try:breaks=json.loads(reply.get('message',{}).get('content','{}'))['break_after']
	except (ValueError,TypeError,KeyError) as ex:raise ValueError('模型未返回有效的切分边界') from ex
	if not isinstance(breaks,list) or any(type(i) is not int or i<0 or i>=len(atoms) for i in breaks):
		raise ValueError('模型返回的切分边界无效')
	# Some models include the final block as a boundary; it has no effect.
	breaks=sorted(set(i for i in breaks if i<len(atoms)-1))
	groups=[];start=0
	for end in breaks+[len(atoms)-1]:
		group=atoms[start:end+1]
		if len(group)>1 and sum(len(atom['original']) for atom in group)>4000:
			raise ValueError('模型把过多内容合在一张卡片中')
		groups.append(group);start=end+1
	return groups


def balanced_knowledge_units(markdown,filename,model=None,progress=None):
	base=knowledge_units(markdown,filename)
	sections=[]
	for unit in base:
		if not sections or sections[-1][0]!=unit['h2']:
			sections.append((unit['h2'],[]))
		sections[-1][1].extend(split_large_unit(unit))
	result=[];warnings=[];model_available=bool(model)
	for section_no,(heading,atoms) in enumerate(sections,1):
		if progress:progress(section_no-1,len(sections),0,0,len(result),0)
		groups=None
		if model_available and len(atoms)>1 and sum(len(a['original']) for a in atoms)>1200:
			try:
				# Keep each request small enough for a local model and leave all source text in the cards.
				windows=[];window=[];size=0
				for atom in atoms:
					if window and size+len(atom['original'])>5000:
						windows.append(window);window=[];size=0
					window.append(atom);size+=len(atom['original'])
				if window:windows.append(window)
				groups=[group for part in windows for group in model_groups(part,heading,model)]
			except Exception as ex:
				warnings.append(f'{heading}：模型切分失败，已使用保留原文的规则切分（{str(ex)[:120]}）')
				model_available=False
		if groups is None:groups=rule_groups(atoms)
		result.extend(join_units(group) for group in groups)
	if progress:progress(len(sections),len(sections),0,0,len(result),0)
	return result,warnings


def organize_knowledge(markdown, filename, model=None, max_rows=100000, progress=None,
		line_range=None, saved_batches=None, on_batch=None):
	units,warnings=balanced_knowledge_units(markdown,filename,model,progress)
	if line_range:units=[u for u in units if line_range[0]<=u['line']<=line_range[1]]
	cards=[];batches=[units[i:i+BATCH_SIZE] for i in range(0,len(units),BATCH_SIZE)]
	if progress:progress(0,len(units),0,len(batches),0,0)
	for batch_no,batch in enumerate(batches,1):
		generated=[]
		for unit in batch:
			generated.append({'kind':'knowledge','identity':'knowledge:'+unit['h2']+':'+str(unit['line'])+':'+unit['original'],
			                  'original':unit['original'],'answer':'理解','options':['理解','不理解'],
			                  'section':unit['section'],'h2':unit['h2'],'line':unit['line']})
		if on_batch:on_batch(batch_no,generated,0)
		cards.extend(generated)
		if progress:progress(min(batch_no*BATCH_SIZE,len(units)),len(units),batch_no,len(batches),len(cards),0)
	return {'version':4,'generator':'ollama-boundaries:'+model if model else 'balanced-rules',
	        'cards':cards,'rejected':0,'scanned_rows':len(units),'resumed_batches':0,'warnings':warnings}


def h2_ranges (markdown):
	result = [];
	current = None;
	code = False
	lines = markdown.splitlines ()
	for line_no, line in enumerate (lines, 1):
		s = line.strip ()
		if s.startswith (('```', '~~~')): code = not code;continue
		if code: continue
		head = re.match (r'^(#{1,2})\s+(.+)', s)
		if not head: continue
		if current:
			current['end'] = line_no - 1
			result.append (current)
			current = None
		if len (head.group (1)) == 2: current = {'title': plain (head.group (2)), 'start': line_no, 'end': len (lines)}
	if current: result.append (current)
	return result


def example_blocks (markdown, filename):
	lines = markdown.splitlines ();
	blocks = [];
	sections = h2_ranges (markdown)
	
	def title_at (n):
		return next ((s['title'] for s in sections if s['start'] <= n <= s['end']), Path (filename).stem)
	
	i = 0;
	code = False
	while i < len (lines):
		line = lines[i].strip ()
		if line.startswith (('```', '~~~')): code = not code;i += 1;continue
		if code: i += 1;continue
		head = re.match (r'^(#{1,6})\s+(.+)', line)
		heading_example = head and re.search (r'例题|真题|练习题|习题', head.group (2))
		inline_example = re.match (r'^(?:【(?:例题|真题|练习题)[^】]*】|(?:例题|真题|练习题)\s*\d*\s*[：:.、])', line)
		if not heading_example and not inline_example: i += 1;continue
		start = i + 1;
		level = len (head.group (1)) if heading_example else 7;
		i += 1
		while i < len (lines):
			next_line = lines[i].strip ()
			next_head = re.match (r'^(#{1,6})\s+', next_line)
			if next_head and len (next_head.group (1)) <= level: break
			if not heading_example and (
					not next_line or re.match (r'^(?:【(?:例题|真题|练习题)|(?:例题|真题|练习题)\s*\d*\s*[：:.、])', next_line)): break
			i += 1
		body = '\n'.join (lines[start - 1:i]).strip ()
		if body: blocks.append ({'note': filename, 'title': title_at (start), 'start': start, 'end': i, 'body': body})
	return blocks


def image_references (markdown, filename):
	result = [];
	code = False
	headings = {}
	for line_no, line in enumerate (markdown.splitlines (), 1):
		s = line.strip ()
		if s.startswith (('```', '~~~')): code = not code;continue
		if code: continue
		head = re.match (r'^(#{1,6})\s+(.+)', s)
		if head:
			level = len (head.group (1))
			headings = {n: title for n, title in headings.items () if n < level}
			if level <= 5: headings[level] = plain (head.group (2))
		paths = []
		for match in re.finditer (r'!\[[^\]]*\]\((?:<([^>]+)>|([^)]*))\)', line):
			paths.append ((match.group (1) or match.group (2)).split (' "', 1)[0].strip ())
		paths += [m.group (2).strip () for m in re.finditer (r'<img\b[^>]*\bsrc\s*=\s*(["\'])(.*?)\1', line, re.I)]
		for path in paths:
			if not path: continue
			title = headings[max (n for n in headings if n >= 2)] if any (n >= 2 for n in headings) else Path (filename).stem
			result.append ({'note': filename, 'path': path, 'title': title,
			                'h2': headings.get (2, Path (filename).stem), 'line': line_no})
	return result


def meaningful_cloze (original, answer, min_context=2):
	if not isinstance (original, str) or not isinstance (answer, str) or original.count (answer) != 1: return False
	context = original.replace (answer, '', 1)
	return len (re.sub (r'[\W_]+', '', context)) >= min_context


def generate (markdown, filename, model, max_rows=120, progress=None, line_range=None, saved_batches=None, on_batch=None):
	rows = question_rows (markdown, filename)
	if line_range: rows = [r for r in rows if line_range[0] <= r['line'] <= line_range[1]]
	rows = rows[:max_rows]
	if not rows:
		if progress: progress (0, 0, 0, 0, 0, 0)
		return {'version': 2, 'generator': 'ollama:' + model, 'cards': [], 'rejected': 0, 'scanned_rows': 0}
	cards = [];
	rejected = 0;
	batches = [];
	chunk = [];
	heading = None;
	resumed_batches = 0
	for row in rows:
		if chunk and (len (chunk) == BATCH_SIZE or row['h2_start'] != heading):
			batches.append (chunk);
			chunk = []
		heading = row['h2_start'];
		chunk.append (row)
	if chunk: batches.append (chunk)
	total_batches = len (batches)
	if progress: progress (0, len (rows), 0, total_batches, 0, 0)
	processed = 0
	for batch_number, chunk in enumerate (batches, 1):
		if saved_batches and batch_number in saved_batches:
			saved = saved_batches[batch_number]
			cards.extend (saved['cards']);
			rejected += saved['rejected'];
			resumed_batches += 1
			processed += len (chunk)
			if progress: progress (processed, len (rows), batch_number, total_batches, len (cards), rejected)
			continue
		by_text = {r['original']: r for r in chunk}
		material = '\n'.join (f'【原文】{r["original"]} 【小标题】{r["section"]}' for r in chunk)
		prompt = '以下原文都属于同一个二级标题「' + chunk[0][
			'h2'] + '」。每条【原文】已经包含相邻句子和列表项，请把整条原文保留为每道题的题干，仅挖去一个答案片段；不要再按句子、逗号或换行拆分题干。多考点可以复用同一完整原文出多道题。\n' + material
		try:
			reply = request_json (model_config ()['ollama_base_url'] + '/api/chat', method='POST', data={'model': model, 'stream': False,
			                                                               'format': SCHEMA,
			                                                               'options': {'temperature': 0.1},
			                                                               'messages': [
				                                                               {'role': 'system', 'content': SYSTEM},
				                                                               {'role': 'user',
				                                                                'content': prompt}]}, timeout=240)
		except Exception as ex:
			raise RuntimeError (f'{filename} / {chunk[0]["h2"]}：第 {batch_number}/{total_batches} 批生成失败：{ex}') from ex
		try:
			raw = json.loads (reply.get ('message', {}).get ('content', '{}'))['cards']
		except (ValueError, TypeError, KeyError) as ex:
			raise RuntimeError (f'{filename} / {chunk[0]["h2"]}：第 {batch_number}/{total_batches} 批返回了无效 JSON') from ex
		if not isinstance (raw, list): raise RuntimeError (f'{filename} / {chunk[0]["h2"]}：第 {batch_number}/{total_batches} 批题目列表无效')
		batch_cards = [];
		batch_rejected = 0
		for v in raw:
			if not isinstance (v, dict): batch_rejected += 1;continue
			original = v.get ('original', '');
			answer = v.get ('answer', '');
			d = v.get ('distractors', [])
			if original not in by_text or not isinstance (answer, str) or len (answer) < 2 or len (answer) > 50 or not meaningful_cloze (original, answer, 8) or not isinstance (d, list) or len (d) != 3 or any (
				not isinstance (x, str) or not x.strip () for x in d): batch_rejected += 1;continue
			opts = [answer] + d
			if len (set (opts)) != 4 or any (len (x) > 60 for x in opts): batch_rejected += 1;continue
			row = by_text[original];
			i = original.index (answer)
			cid = 'note-' + hashlib.sha1 ((
						filename + '|' + row['section'] + '|' + original + '|' + answer).encode ()).hexdigest ()[:16]
			batch_cards.append ({'id': cid, 'section': row['section'], 'h2': row['h2'], 'answer': answer,
			                     'sentence': original[:i] + '【' + answer + '】' + original[i + len (answer):],
			                     'options': opts, 'source': filename, 'line': row['line'], 'needs_review': True,
			                     'generator': 'ollama:' + model})
		if on_batch: on_batch (batch_number, batch_cards, batch_rejected)
		cards.extend (batch_cards);
		rejected += batch_rejected
		processed += len (chunk)
		if progress: progress (processed, len (rows), batch_number, total_batches, len (cards), rejected)
	unique = list ({c['id']: c for c in cards}.values ())
	return {'version': 2, 'generator': 'ollama:' + model, 'cards': unique, 'rejected': rejected,
	        'scanned_rows': len (rows), 'resumed_batches': resumed_batches}
