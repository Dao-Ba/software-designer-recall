const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const katex = require('../web/vendor/katex/katex.min.js');
const markdownit = require('../web/vendor/markdown-it/markdown-it.min.js');

const html = fs.readFileSync('web/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(x => x[1]);
const elements = new Map(ids.map(id => [id, {
  value: '', textContent: '', innerHTML: '', dataset: {}, style:{}, disabled: false, files: [],
  focus() {},
  removeAttribute() {},
  classList: {add() {}, remove() {}, toggle() {}},
  querySelectorAll() {return []}
}]));
elements.get('studyOrder').value='random';
const recentBackButton={onclick:null};
elements.get('recentAnswer').querySelectorAll=selector=>selector==='[data-recent-back]'?[recentBackButton]:[];
const navigatorListeners={};
Object.assign(elements.get('pageNavigator'),{
  clientHeight:600,
  addEventListener(type,callback) {navigatorListeners[type]=callback},
  getBoundingClientRect() {return {top:100,height:600}},
  setAttribute() {},
  setPointerCapture() {}
});
const tabs = ['random', 'wrong', 'retry', 'cards', 'manage', 'backup'].map(view => ({
  dataset: {view}, classList: {toggle() {}}
}));
const cards = [
  {id:'fresh',kind:'knowledge',note:'网络.md',h2:'命令',section:'命令',line:3,original:'ipconfig 显示网络配置。',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0},
  {id:'image',kind:'image',note:'网络.md',h2:'结构图',section:'结构图',line:7,original:'结构图',answer:'了解',options:['了解','不了解'],media_id:'m1',media_created:'v1',attempts:0,wrong:0,wrong_marked:0,favorite:0}
];
let queueRequests = 0;
let fullSyncRequests = 0;
let mathRenders = 0;
let hostBackups = 0;
let sqliteRestores = 0;
let storedPin = null;
const context = {
  window:{location:{origin:'http://127.0.0.1:8765',hostname:'127.0.0.1'},innerHeight:1000,scrollY:0,
    addEventListener() {},scrollTo(x,y) {this.scrollY=y}},
  document:{
    documentElement:{scrollHeight:5000},
    getElementById(id) {return elements.get(id)},
    querySelectorAll(selector) {return selector==='[data-view]'?tabs:[]},
    querySelector() {return {value:'replace'}}
  },
  fetch:async (path, request={})=>({ok:true,json:async()=>{
    if(path==='/api/answer') {
      const {id,selected,mode} = JSON.parse(request.body);
      const card=cards.find(c=>c.id===id);
      card.attempts++;
      if(mode==='random') card.random_return_pending=0;
      if(selected!==card.answer) {card.wrong++;card.wrong_marked=1}
      if(mode==='wrong') card.round1_done=1;
      if(mode==='retry') {card.retry_deferred=selected!==card.answer?1:0;if(selected===card.answer)card.round1_done=0}
      return {correct:selected===card.answer,card:{...card},stats:{
        total:cards.length,studied:cards.filter(item=>item.attempts>0).length,
        wrong:cards.filter(item=>item.wrong_marked).length,
        favorites:cards.filter(item=>item.favorite).length
      }};
    }
    if(path==='/api/retry/next-batch') {
      const {note}=JSON.parse(request.body);
      let released=0;
      for(const card of cards) if(card.retry_deferred && (!note || card.note===note)) {card.retry_deferred=0;released++}
      return {released};
    }
    if(path==='/api/flags') {
      const data=JSON.parse(request.body);
      const card=cards.find(c=>c.id===data.id);
      Object.assign(card,data);
      return {ok:true,card:{...card},stats:{
        total:cards.length,studied:cards.filter(item=>item.attempts>0).length,
        wrong:cards.filter(item=>item.wrong_marked).length,
        favorites:cards.filter(item=>item.favorite).length
      }};
    }
    if(path==='/api/image/annotation') {
      const {id,annotation}=JSON.parse(request.body);
      const card=cards.find(c=>c.id===id);
      card.annotation=annotation.trim();
      return {ok:true,card:{...card},stats:{
        total:cards.length,studied:cards.filter(item=>item.attempts>0).length,
        wrong:cards.filter(item=>item.wrong_marked).length,
        favorites:cards.filter(item=>item.favorite).length
      }};
    }
    if(path==='/api/manual/image') {
      const data=JSON.parse(request.body);
      assert.ok(['image/png','image/svg+xml'].includes(data.mime));
      assert.ok(data.data.length>0);
      const id=data.mime==='image/svg+xml'?'manual-svg':'manual-image';
      cards.push({id,kind:'image',note:data.note,h2:data.title,section:data.title,line:0,
        original:data.title,answer:'了解',options:['了解','不了解'],media_id:'manual-media',
        annotation:data.annotation,attempts:0,wrong:0,wrong_marked:0,favorite:0});
      return {saved:1,id};
    }
    if(path==='/api/knowledge-card/edit') {
      const {id,title,content}=JSON.parse(request.body);
      Object.assign(cards.find(c=>c.id===id),{section:title,original:content});
      return {id,title,content};
    }
    if(path==='/api/cards/pin') {
      const {id}=JSON.parse(request.body);
      storedPin=storedPin===id?null:id;
      return {pinned_id:storedPin};
    }
    if(path==='/api/cards/delete-item') {
      const {id}=JSON.parse(request.body);
      cards.splice(cards.findIndex(c=>c.id===id),1);
      if(storedPin===id)storedPin=null;
      return {deleted:1};
    }
    if(path==='/api/cards') {fullSyncRequests++;return {cards,pinned_id:storedPin,exam:{id:'example',title:'示例考试复习系统',database:'data/example/study.sqlite3',backup_dir:'data/example/backups'},stats:{total:cards.length,studied:0,wrong:0,favorites:0}}}
    if(path==='/api/backup/save') {hostBackups++;return {path:`D:\\Dev\\soft_exam_memory\\data\\backups\\study-${hostBackups}.sqlite3`,filename:`study-${hostBackups}.sqlite3`,bytes:4096}}
    if(path==='/api/backup/import-sqlite') {
      assert.ok(request.body instanceof Blob);
      sqliteRestores++;
      return {notes:1,cards:2,media:0,attempts:1,previous_database:'before.sqlite3'};
    }
    if(path==='/api/knowledge') {fullSyncRequests++;return {knowledge:cards.filter(c=>c.kind==='knowledge').map(c=>({id:c.id,note:c.note,h2:c.h2,section:c.section,line:c.line,content:c.original}))}}
    if(path==='/api/models') return {models:['qwen3-vl:4b','qwen3:8b'],default_model:'qwen3-vl:4b'};
    if(path==='/api/config') return {server:{port:8765},default_model:'qwen3-vl:4b'};
    if(path==='/api/cpolar') return {tunnel_name:'软考',public_url:'https://study.example',found:true};
    if(path==='/api/import-queue/status') {
      queueRequests++;
      if(queueRequests===1) throw Error('Failed to fetch');
      return {id:'old-import',pending:[],completed:[{name:'网络.md',result:{saved:1,images:0}}]};
    }
    if(path==='/api/notes') return {notes:[]};
    return {ok:true};
  }}),
  Date,Math:{random:()=>.99,floor:Math.floor,round:Math.round,min:Math.min,max:Math.max},Promise,btoa,Uint8Array,alert() {},confirm() {return true},
  markdownit,
  katex,
  renderMathInElement() {mathRenders++},
  setTimeout(callback) {return setTimeout(callback,0)}
};
context.Blob=Blob;

async function main() {
  vm.createContext(context);
  vm.runInContext(script,context);
  await new Promise(resolve=>setTimeout(resolve,20));
  let contentHeight=700, heightResets=0;
  const editingArea={
    style:{
      _height:'',
      get height() {return this._height},
      set height(value) {if(value==='auto')heightResets++;this._height=value}
    },
    get clientHeight() {return (parseFloat(this.style.height)||260)-2},
    get offsetHeight() {return parseFloat(this.style.height)||260},
    get scrollHeight() {return Math.max(contentHeight,this.clientHeight)}
  };
  context.editingArea=editingArea;
  vm.runInContext('fitCardTextarea(editingArea,260,true)',context);
  const initialHeight=parseFloat(editingArea.style.height);
  vm.runInContext('fitCardTextarea(editingArea)',context);
  assert.strictEqual(parseFloat(editingArea.style.height),initialHeight);
  assert.strictEqual(heightResets,1);
  contentHeight=900;
  vm.runInContext('fitCardTextarea(editingArea)',context);
  assert.ok(parseFloat(editingArea.style.height)>initialHeight);
  assert.strictEqual(heightResets,1);
  assert.strictEqual(elements.get('appTitle').textContent,'示例考试复习系统');
  assert.strictEqual(context.document.title,'示例考试复习系统');
  assert.ok(queueRequests>=2);
  assert.strictEqual(elements.get('model').value,'qwen3-vl:4b');
  const styledTitle=vm.runInContext("renderTitleHtml('创建型设计模式`主要处理创建对象` **重点** ==高亮==')",context);
  assert.match(styledTitle,/<code>主要处理创建对象<\/code>/);
  assert.match(styledTitle,/<strong>重点<\/strong>/);
  assert.match(styledTitle,/<mark>高亮<\/mark>/);
  assert.match(elements.get('generateStatus').textContent,/处理完成/);
  assert.strictEqual(elements.get('importReport').open,true);
  vm.runInContext("lastImportReportPhase=''",context);
  vm.runInContext("showImportQueue({id:'old-import',pending:[],completed:[{name:'网络.md',result:{saved:1,images:0}}]})",context);
  assert.strictEqual(elements.get('importReport').open,false);
  vm.runInContext("showImportQueue({id:'new-import',pending:['网络.md'],completed:[]})",context);
  assert.strictEqual(elements.get('importReport').open,true);
  vm.runInContext("showImportQueue({id:'new-import',pending:[],completed:[{name:'网络.md',result:{saved:1,images:0}}]})",context);
  assert.strictEqual(elements.get('importReport').open,true);
  assert.doesNotMatch(html,/data-view="mobile"/);
  tabs.find(tab=>tab.dataset.view==='backup').onclick();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.strictEqual(hostBackups,1);
  assert.match(elements.get('backupStatus').textContent,/备份已保存到运行程序的电脑/);
  await elements.get('export').onclick();
  assert.strictEqual(hostBackups,2);
  elements.get('exportDb').onclick();
  assert.match(elements.get('backupStatus').textContent,/data\/example\/study.sqlite3[\s\S]*data\/example\/backups/);
  const savedFile=new Blob(['SQLite format 3\0']);
  savedFile.name='study.sqlite3';
  assert.match(html,/id="backupFile" accept="\.sqlite3/);
  assert.doesNotMatch(html,/id="backupFile"[^>]*\.json/);
  const jsonFile=new Blob(['{}']);
  jsonFile.name='old.json';
  elements.get('backupFile').files=[jsonFile];
  await elements.get('restoreBackup').onclick();
  assert.strictEqual(sqliteRestores,0);
  elements.get('backupFile').files=[savedFile];
  await elements.get('restoreBackup').onclick();
  assert.strictEqual(sqliteRestores,1);
  assert.match(elements.get('importStatus').textContent,/恢复完成/);
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.strictEqual(elements.get('importReport').open,false);
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  assert.strictEqual(elements.get('importReport').open,false);
  cards[0].wrong_marked=1;
  cards[1].favorite=1;
  const attemptsBefore=cards.map(card=>card.attempts);
  elements.get('studyOrder').value='source';
  tabs.find(tab=>tab.dataset.view==='wrong').onclick();
  assert.strictEqual(vm.runInContext('list.length',context),2);
  assert.strictEqual(elements.get('viewTitle').textContent,'不理解与收藏 · 第一轮');
  assert.match(elements.get('options').innerHTML,/理解/);
  assert.strictEqual(elements.get('next').textContent,'跳过 / 下一题 →');
  assert.strictEqual(elements.get('previous').disabled,true);
  const firstOrderedId=vm.runInContext('picked.id',context);
  elements.get('next').onclick();
  assert.strictEqual(elements.get('previous').disabled,false);
  elements.get('previous').onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),firstOrderedId);
  elements.get('next').onclick();
  elements.get('next').onclick();
  assert.strictEqual(vm.runInContext('picked',context),null);
  elements.get('previous').onclick();
  assert.ok(vm.runInContext('picked',context));
  elements.get('next').onclick();
  assert.deepStrictEqual(cards.map(card=>card.attempts),attemptsBefore);
  assert.strictEqual(cards[0].wrong_marked,1);
  assert.strictEqual(cards[1].favorite,1);
  cards[0].wrong_marked=0;
  cards[1].favorite=0;
  elements.get('shuffle').onclick();
  assert.strictEqual(vm.runInContext('list.length',context),0);
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'fresh,image');
  assert.strictEqual(elements.get('shuffle').textContent,'从头开始');
  context.Math.random=()=>0;
  elements.get('studyOrder').value='random';
  vm.runInContext('randomBatchIds=[]',context);
  elements.get('studyOrder').onchange();
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'image,fresh');
  context.Math.random=()=>.99;
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  vm.runInContext('randomBatchIds=[];pick()',context);
  assert.match(elements.get('prompt').innerHTML,/ipconfig 显示网络配置/);
  assert.doesNotMatch(elements.get('prompt').innerHTML,/blank|________/);
  assert.match(elements.get('options').innerHTML,/理解/);
  assert.match(elements.get('options').innerHTML,/不理解/);
  const splitContainer=elements.get('splitCards');
  let splitButtons=[],splitAreas=[];
  splitContainer.querySelectorAll=selector=>selector==='[data-extract-part]'?splitButtons:selector==='[data-part-content]'?splitAreas:[];
  splitContainer.querySelector=selector=>splitAreas[Number(selector.match(/data-part-content="(\d+)"/)[1])];
  Object.defineProperty(splitContainer,'innerHTML',{get(){return this._html||''},set(value){
    this._html=value;
    splitButtons=[...value.matchAll(/data-extract-part="(\d+)"/g)].map(match=>({dataset:{extractPart:match[1]}}));
    splitAreas=[...value.matchAll(/data-part-content="(\d+)" data-part-key="(\d+)"[^>]*>([\s\S]*?)<\/textarea>/g)]
      .map(match=>({dataset:{partContent:match[1],partKey:match[2]},value:match[3],selectionStart:0,selectionEnd:0,style:{},scrollHeight:30,
        setSelectionRange(start,end) {this.selectionStart=start;this.selectionEnd=end}}));
  }});
  const knowledgeBox={scrollTop:120};
  elements.get('knowledgeModal').querySelector=selector=>selector==='.modalBox'?knowledgeBox:null;
  assert.ok(!html.includes('id="knowledgeOriginal"'));
  elements.get('editCurrent').onclick();
  assert.strictEqual(knowledgeBox.scrollTop,0);
  elements.get('splitMode').onclick();
  assert.match(splitContainer.innerHTML,/ipconfig 显示网络配置/);
  assert.doesNotMatch(splitContainer.innerHTML,/<textarea[^>]*readonly/);
  splitAreas[0].value='ipconfig 显示网络配置（已补充）。';
  splitAreas[0].oninput();
  assert.strictEqual(vm.runInContext('splitParts[0].content',context),'ipconfig 显示网络配置（已补充）。');
  splitAreas[0].selectionStart=0;
  splitAreas[0].selectionEnd=8;
  splitButtons[0].onclick();
  assert.strictEqual((splitContainer.innerHTML.match(/class="splitCard"/g)||[]).length,2);
  assert.match(splitContainer.innerHTML,/第 2 张卡片/);
  splitAreas[1].selectionStart=0;
  splitAreas[1].selectionEnd=3;
  splitButtons[1].onclick();
  assert.strictEqual((splitContainer.innerHTML.match(/class="splitCard"/g)||[]).length,3);
  splitAreas[0].selectionStart=0;
  splitAreas[0].selectionEnd=4;
  splitButtons[0].onclick();
  assert.strictEqual((splitContainer.innerHTML.match(/class="splitCard"/g)||[]).length,4);
  assert.match(splitContainer.innerHTML,/拆分第 1 张卡片的选中文字/);
  elements.get('closeKnowledge').onclick();
  elements.get('editCurrent').onclick();
  elements.get('splitMode').onclick();
  const editedBeforeSplit='外部属性可直接测量。\n\nMcCabe度量法：m−n+2。\n\n';
  splitAreas[0].value=editedBeforeSplit;
  splitAreas[0].oninput();
  splitAreas[0].selectionStart=0;
  splitAreas[0].selectionEnd=2;
  splitAreas[0].onselect();
  splitAreas[0].selectionStart=editedBeforeSplit.indexOf('McCabe');
  splitAreas[0].selectionEnd=editedBeforeSplit.indexOf('。',splitAreas[0].selectionStart)+1;
  splitButtons[0].onclick();
  assert.strictEqual((splitContainer.innerHTML.match(/class="splitCard"/g)||[]).length,2);
  assert.match(splitContainer.innerHTML,/McCabe度量法：m−n\+2。/);
  elements.get('closeKnowledge').onclick();
  knowledgeBox.scrollTop=360;
  elements.get('editCurrent').onclick();
  assert.strictEqual(knowledgeBox.scrollTop,0);
  assert.strictEqual(elements.get('knowledgeTitle').value,'命令');
  elements.get('knowledgeTitle').value='新的卡片标题';
  const syncsBeforeEdit=fullSyncRequests;
  await elements.get('saveKnowledge').onclick();
  assert.strictEqual(cards[0].section,'新的卡片标题');
  assert.strictEqual(fullSyncRequests,syncsBeforeEdit);
  assert.strictEqual(vm.runInContext('all.find(card=>card.id==="fresh").section',context),'新的卡片标题');
  assert.ok(mathRenders>=1);
  assert.match(html,/vendor\/katex\/auto-render\.min\.js/);
  assert.match(html,/vendor\/dompurify\/purify\.min\.js/);
  const markdownOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify('- ==**重点**==：关系 $R \\times S$') + ')',context);
  assert.match(markdownOutput,/<ul>[\s\S]*<mark><strong>重点<\/strong><\/mark>/);
  const interruptSource='> - 在计算机中断系统中，==**中断向量**==是指向==**中断服务程序入口地址**==的指针或地址值。当CPU响应中断时，它会根据中断类型号（或向量号）在中断向量表中查找对应的中断向量，从而获得==中断服务程序的起始执行地址==。';
  const italicNumber=vm.runInContext("noteMarkdown.render('==CPU的 *2.*控制器== 和 *2*')",context);
  assert.match(italicNumber,/<mark>CPU的 <em>2\.<\/em>控制器<\/mark> 和 <em>2<\/em>/);
  const interruptOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify(interruptSource) + ')',context);
  assert.match(interruptOutput,/<mark><strong>中断向量<\/strong><\/mark>是指向<mark><strong>中断服务程序入口地址<\/strong><\/mark>的指针或地址值/);
  assert.match(interruptOutput,/从而获得<mark>中断服务程序的起始执行地址<\/mark>。/);
  assert.strictEqual((interruptOutput.match(/<mark>/g)||[]).length,3);
  const assemblyLine='- 汇编语言的指令语句**`必须具有操作码字段`**，`可以没有操作数字段`';
  const assemblyOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify(assemblyLine) + ')',context);
  assert.match(assemblyOutput,/<strong><code>必须具有操作码字段<\/code><\/strong>/);
  assert.doesNotMatch(assemblyOutput,/\*\*/);
  assert.match(vm.runInContext('renderNoteText(' + JSON.stringify(assemblyLine) + ')',context),/<strong><code>必须具有操作码字段<\/code><\/strong>/);
  const deadlockSource=fs.readFileSync('notes/软考笔记/操作系统.md','utf8').split(/\r?\n/).slice(104,112).join('\n');
  const deadlockOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify(deadlockSource) + ')',context);
  assert.match(deadlockOutput,/<mark>\$n\*\(R-1\)\$<\/mark>/);
  assert.match(deadlockOutput,/<mark>\$n\*\(R-1\)\+1\$<\/mark>/);
  assert.doesNotMatch(deadlockOutput,/==\$n/);
  assert.ok(markdownOutput.includes(String.raw`$R \times S$`));
  const displayMath=String.raw`> \[ \boxed{\text{小数点后第 }n\text{ 位的权值为 }2^{-n}} \]`;
  const displayOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify(displayMath) + ')',context);
  assert.ok(displayOutput.includes(String.raw`\[ \boxed{\text{小数点后第 }n\text{ 位的权值为 }2^{-n}} \]`));
  assert.ok(vm.runInContext('noteMarkdown.render(' + JSON.stringify(String.raw`关系 \(R \times S\)`) + ')',context).includes(String.raw`\(R \times S\)`));
  const realNote=fs.readFileSync('notes/软考笔记/计算机网络概述.md','utf8');
  const realExcerpt=realNote.split(/\r?\n/).slice(250,274).join('\n');
  const realOutput=vm.runInContext('noteMarkdown.render(' + JSON.stringify(realExcerpt) + ')',context);
  assert.ok(realOutput.includes(String.raw`\[ 0.1011_2 =2^{-1}+2^{-3}+2^{-4} \]`));
  assert.ok(realOutput.includes(String.raw`\[ \boxed{\text{小数点后第 }n\text{ 位的权值为 }2^{-n}} \]`));
  const diagramNote=fs.readFileSync('notes/软考笔记/程序设计语言的基本概念.md','utf8');
  const diagramExcerpt=diagramNote.split(/\r?\n/).slice(192,209).join('\n').replace('        Root --- NodeA','Root --- NodeA');
  context.DOMPurify={sanitize:html=>html};
  const diagramOutput=vm.runInContext('renderNoteHtml(' + JSON.stringify(diagramExcerpt) + ')',context);
  delete context.DOMPurify;
  assert.match(diagramOutput,/<svg[^>]+aria-label="流程图"/);
  assert.match(diagramOutput,/>\+<\/text>/);
  assert.match(diagramOutput,/>a<\/text>/);
  assert.match(diagramOutput,/>b<\/text>/);
  assert.doesNotMatch(diagramOutput,/flowchart TD|Root --- NodeA|```/);
  const floatNote=fs.readFileSync('notes/软考笔记/计算机网络概述.md','utf8');
  const floatExcerpt=floatNote.split(/\r?\n/).slice(191,214).join('\n');
  context.DOMPurify={sanitize:html=>html};
  const floatOutput=vm.runInContext('renderNoteHtml(' + JSON.stringify(floatExcerpt) + ')',context);
  delete context.DOMPurify;
  assert.strictEqual((floatOutput.match(/class="katex-display"/g)||[]).length,3);
  assert.doesNotMatch(floatOutput,/\$\$|katex-error/);
  assert.doesNotMatch(katex.renderToString(String.raw`\boxed{\text{小数点后第 }n\text{ 位的权值为 }2^{-n}}`,{throwOnError:false,displayMode:true}),/katex-error/);
  const tableCard='- 集线器环境：所有设备在同一个冲突域\n| 设备类型 | 工作层次 |\n| --- | --- |\n| 集线器 | 物理层 |';
  const prepared=vm.runInContext('prepareNoteMarkdown(' + JSON.stringify(tableCard) + ')',context);
  assert.match(markdownit().render(prepared),/<table>[\s\S]*<td>集线器<\/td>/);
  const pastedPython='"""\n爬楼梯。\n"""\n\nclass Solution(object):\n    def climbStairs(self, n):\n        return n + 1';
  context.DOMPurify={sanitize:html=>html};
  const codeOutput=vm.runInContext('renderNoteHtml(' + JSON.stringify(pastedPython) + ')',context);
  const mixedCodeOutput=vm.runInContext('renderNoteHtml(' + JSON.stringify('解法如下：\n\nclass Solution:\n    def solve(self):\n        return 2') + ')',context);
  delete context.DOMPurify;
  assert.match(codeOutput,/<pre><code>[\s\S]*class[\s\S]*    <span class="codeKeyword">def<\/span>/);
  assert.match(codeOutput,/<span class="codeString">&quot;&quot;&quot;[\s\S]*爬楼梯/);
  assert.doesNotMatch(codeOutput,/<p>class Solution/);
  assert.match(mixedCodeOutput,/<p>解法如下：<\/p>[\s\S]*<pre><code>/);
  const formula=String.raw`R \times S = \{ t \mid t = \langle t^n, t^m\rangle \land t^n \in R \land t^m \in S \}`;
  assert.doesNotMatch(katex.renderToString(formula,{throwOnError:false,trust:false}),/katex-error/);
  const mathSource=String.raw`关系 $R \times S$`;
  assert.ok(vm.runInContext('renderNoteHtml(' + JSON.stringify(mathSource) + ')',context).includes(String.raw`$R \times S$`));
  const rendered=vm.runInContext("renderNoteHtml('<table><tr><td rowspan=\"2\">==重点== 和 **强调**</td></tr></table><script>alert(1)</script><img src=x onerror=alert(1)>')",context);
  assert.match(rendered,/<table><tr><td rowspan="2"><mark>重点<\/mark> 和 <strong>强调<\/strong><\/td><\/tr><\/table>/);
  assert.doesNotMatch(rendered,/<script>|<img|onerror=/);
  tabs.find(tab=>tab.dataset.view==='cards').onclick();
  assert.match(elements.get('cardList').innerHTML,/1 条知识点/);
  const statusFilter=elements.get('cardStatusFilter');
  assert.match(html,/<option value="all">全部知识点<\/option>/);
  assert.match(html,/<option value="wrong">不理解与收藏<\/option>/);
  assert.match(html,/<option value="retry">再次查看<\/option>/);
  cards[0].favorite=1;
  cards[1].wrong_marked=1;
  cards[1].round1_done=1;
  cards[1].persistent_unknown=1;
  for(const [status,shown,hidden] of [
    ['wrong','fresh','image'],['retry','image','fresh']
  ]) {
    statusFilter.value=status;
    statusFilter.onchange();
    assert.match(elements.get('cardList').innerHTML,new RegExp(`data-card-id="${shown}"`));
    assert.doesNotMatch(elements.get('cardList').innerHTML,new RegExp(`data-card-id="${hidden}"`));
  }
  statusFilter.value='all';
  statusFilter.onchange();
  assert.match(elements.get('cardList').innerHTML,/data-card-id="fresh"/);
  assert.match(elements.get('cardList').innerHTML,/data-card-id="image"/);
  cards[0].favorite=0;
  cards[1].wrong_marked=0;
  cards[1].round1_done=0;
  cards[1].persistent_unknown=0;
  statusFilter.value='all';
  statusFilter.onchange();
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.ok(vm.runInContext('randomBatchIds.length',context)>0);
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  elements.get('manualNote').value='网络.md';
  elements.get('manualH2').value='命令';
  elements.get('manualText').value='手工新增的知识点';
  elements.get('smartSplit').checked=false;
  elements.get('manualWriteKnowledge').checked=true;
  elements.get('manualWriteQuestions').checked=true;
  const originalManualFetch=context.fetch;
  context.fetch=async(path,request)=>{
    if(path==='/api/ingest/start') {
      cards.push({id:'manual-new',kind:'knowledge',note:'网络.md',h2:'命令',section:'命令',line:10,
        original:'手工新增的知识点',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
      return {ok:true,json:async()=>({saved:1})};
    }
    return originalManualFetch(path,request);
  };
  await elements.get('manualGenerate').onclick();
  context.fetch=originalManualFetch;
  assert.ok(vm.runInContext("randomBatchIds.includes('manual-new')",context));
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.ok(vm.runInContext("list.some(card=>card.id==='manual-new')",context));
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  const manualImage=elements.get('manualImage');
  manualImage.files=[{name:'图示.png',type:'image/png',size:12,
    arrayBuffer:async()=>Uint8Array.from([137,80,78,71,13,10,26,10,0,0,0,0]).buffer}];
  manualImage.onchange();
  assert.strictEqual(elements.get('manualTextLabel').textContent,'图片注释（可留空）');
  elements.get('manualText').value='图片里的重点';
  await elements.get('manualGenerate').onclick();
  assert.ok(vm.runInContext("randomBatchIds.includes('manual-image')",context));
  assert.strictEqual(cards.find(card=>card.id==='manual-image').annotation,'图片里的重点');
  manualImage.files=[{name:'结构.svg',type:'image/svg+xml',size:42,
    arrayBuffer:async()=>new TextEncoder().encode('<svg xmlns="http://www.w3.org/2000/svg"></svg>').buffer}];
  manualImage.onchange();
  await elements.get('manualGenerate').onclick();
  assert.ok(cards.some(card=>card.id==='manual-svg'));
  manualImage.files=[];
  manualImage.onchange();
  cards.splice(cards.findIndex(card=>card.id==='manual-new'),1);
  cards.splice(cards.findIndex(card=>card.id==='manual-image'),1);
  cards.splice(cards.findIndex(card=>card.id==='manual-svg'),1);
  await vm.runInContext('refresh()',context);
  tabs.find(tab=>tab.dataset.view==='cards').onclick();
  let navigatorHidden;
  elements.get('pageNavigator').classList.toggle=(name,hidden)=>{if(name==='hidden')navigatorHidden=hidden};
  vm.runInContext('updatePageNavigator()',context);
  assert.strictEqual(navigatorHidden,false);
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  assert.strictEqual(navigatorHidden,true);
  tabs.find(tab=>tab.dataset.view==='cards').onclick();
  assert.strictEqual(navigatorHidden,false);
  const geometry=vm.runInContext('pageNavigatorMetrics(5000,1000,600,2000)',context);
  assert.strictEqual(geometry.maxScroll,4000);
  assert.strictEqual(geometry.thumbHeight,120);
  assert.strictEqual(geometry.top,240);
  navigatorListeners.pointerdown({clientY:400,pointerId:1,preventDefault() {}});
  assert.ok(context.window.scrollY>0);
  const beforeDrag=context.window.scrollY;
  navigatorListeners.pointermove({clientY:650});
  assert.ok(context.window.scrollY>beforeDrag);
  navigatorListeners.pointerup();
  cards[1].h2=cards[1].section='虚设备和SPOOLING技术';
  elements.get('cardSearch').value='虚设备和 SPOOLING 技术';
  vm.runInContext('showList()',context);
  assert.match(elements.get('cardList').innerHTML,/data-card-id="image"/);
  elements.get('cardSearch').value='';
  cards[1].h2=cards[1].section='结构图';
  cards.push({id:'after-image',kind:'knowledge',note:'网络.md',h2:'结构图',section:'结构图',line:9,
    original:'图片后面的知识点',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('syncCards()',context);
  assert.ok(elements.get('cardList').innerHTML.indexOf('data-card-id="image"') < elements.get('cardList').innerHTML.indexOf('data-card-id="after-image"'));
  cards.splice(cards.findIndex(c=>c.id==='after-image'),1);
  await vm.runInContext('syncCards()',context);
  assert.match(elements.get('cardList').innerHTML,/data-chapter="网络\.md"/);
  assert.doesNotMatch(elements.get('cardList').innerHTML,/data-image-answer|data-image-favorite|data-image-clear/);
  assert.match(elements.get('cardList').innerHTML,/loading="lazy" decoding="async" src="\/api\/media\/m1\?v=v1"/);
  assert.match(elements.get('cardList').innerHTML,/data-image-annotation/);
  assert.match(elements.get('cardList').innerHTML,/删除图片卡片/);
  assert.ok(mathRenders>=2);
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  const normalFetch=context.fetch;
  context.fetch=async (path,request)=>{
    if(path==='/api/answer') throw Error('连接中断');
    return normalFetch(path,request);
  };
  await vm.runInContext("choose('理解')",context);
  assert.strictEqual(vm.runInContext('picked.id',context),'fresh');
  assert.strictEqual(elements.get('saveStatus').textContent,'');
  assert.strictEqual(cards[0].attempts,0);
  let releaseAnswer;
  context.fetch=async (path,request)=>{
    if(path==='/api/answer') await new Promise(resolve=>{releaseAnswer=resolve});
    return normalFetch(path,request);
  };
  const syncsBeforeAnswer=fullSyncRequests;
  const pendingAnswer=vm.runInContext("choose('理解')",context);
  assert.strictEqual(vm.runInContext('picked.id',context),'image');
  assert.strictEqual(elements.get('saveStatus').textContent,'上一题保存中…');
  assert.strictEqual(elements.get('next').disabled,true);
  releaseAnswer();
  await pendingAnswer;
  context.fetch=normalFetch;
  assert.strictEqual(elements.get('saveStatus').textContent,'');
  assert.strictEqual(fullSyncRequests,syncsBeforeAnswer);
  assert.strictEqual(vm.runInContext('picked.id',context),'image');
  assert.match(elements.get('recentAnswer').innerHTML,/data-recent-back[^>]*>← 上一条知识点已理解/);
  recentBackButton.onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),'fresh');
  elements.get('next').onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),'image');
  assert.match(elements.get('recentAnswer').innerHTML,/上一条知识点：/);
  elements.get('editCurrent').onclick();
  elements.get('imageAnnotationText').value='这是图片注释：$R+1$';
  await elements.get('saveImageAnnotation').onclick();
  assert.match(elements.get('prompt').innerHTML,/这是图片注释：\$R\+1\$/);
  assert.match(elements.get('recentAnswer').innerHTML,/上一条知识点：/);
  await vm.runInContext("choose('不了解')",context);
  assert.strictEqual(vm.runInContext('picked',context),null);
  assert.match(elements.get('recentAnswer').innerHTML,/上一条图片已标记不了解/);
  recentBackButton.onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),'image');
  elements.get('next').onclick();
  assert.strictEqual(vm.runInContext('picked',context),null);
  tabs.find(tab=>tab.dataset.view==='wrong').onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),'image');
  await vm.runInContext("choose('了解')",context);
  assert.match(elements.get('recentAnswer').innerHTML,/取消不理解标记/);
  const syncsBeforeFlag=fullSyncRequests;
  let releaseFlag;
  context.fetch=async (path,request)=>{
    if(path==='/api/flags') await new Promise(resolve=>{releaseFlag=resolve});
    return normalFetch(path,request);
  };
  const pendingFlag=vm.runInContext("setRecentFlag('wrong_marked')",context);
  assert.strictEqual(vm.runInContext("all.find(card=>card.id==='image').wrong_marked",context),false);
  assert.strictEqual(elements.get('saveStatus').textContent,'保存中…');
  assert.strictEqual(elements.get('next').disabled,true);
  releaseFlag();
  await pendingFlag;
  context.fetch=normalFetch;
  assert.strictEqual(fullSyncRequests,syncsBeforeFlag);
  assert.strictEqual(cards[1].wrong_marked,false);
  assert.strictEqual(elements.get('saveStatus').textContent,'');
  context.fetch=async (path,request)=>{
    if(path==='/api/flags') throw Error('连接中断');
    return normalFetch(path,request);
  };
  await vm.runInContext("changeFlags('image',{favorite:true})",context);
  context.fetch=normalFetch;
  assert.strictEqual(vm.runInContext("all.find(card=>card.id==='image').favorite",context),0);
  assert.strictEqual(elements.get('saveStatus').textContent,'');
  assert.match(elements.get('cardList').innerHTML,/1 条知识点/);
  const tree=vm.runInContext("targetMarkup('delete',[{name:'网络.md',sections:[{title:'命令',start:2},{title:'结构图',start:6}]}])",context);
  assert.match(tree,/<details class="chapterGroup">/);
  assert.match(tree,/<details class="topicGroup">/);
  assert.match(tree,/ipconfig 显示网络配置/);
  assert.match(tree,/<summary><input type="checkbox" class="target"/);
  assert.strictEqual((tree.match(/data-scope=/g)||[]).length,3);
  const deleteBoxes=[{checked:false,indeterminate:true},{checked:false,indeterminate:false},{checked:false,indeterminate:false}];
  elements.get('deleteTargets').querySelectorAll=selector=>selector==='input.target'?deleteBoxes:[];
  elements.get('selectAllDelete').onclick();
  assert.ok(deleteBoxes.every(box=>box.checked&&!box.indeterminate));
  elements.get('clearDeleteSelection').onclick();
  assert.ok(deleteBoxes.every(box=>!box.checked&&!box.indeterminate));
  const regularQuerySelectorAll=context.document.querySelectorAll;
  context.document.querySelectorAll=selector=>selector==='.target[data-group="delete"]:checked'
    ? [{dataset:{note:'网络.md',scope:''}}] : regularQuerySelectorAll(selector);
  let releaseDelete;
  context.fetch=async (path,request)=>{
    if(path==='/api/cards/delete-targets') await new Promise(resolve=>{releaseDelete=resolve});
    return path==='/api/cards/delete-targets'
      ? {ok:true,json:async()=>({deleted_cards:0,deleted_knowledge:1})}
      : normalFetch(path,request);
  };
  const pendingDelete=vm.runInContext("deleteSelected('knowledge')",context);
  assert.match(elements.get('deleteStatus').textContent,/正在删除/);
  assert.strictEqual(elements.get('deleteSource').disabled,true);
  releaseDelete();
  await pendingDelete;
  context.fetch=normalFetch;
  context.document.querySelectorAll=regularQuerySelectorAll;
  assert.match(elements.get('deleteStatus').textContent,/列表已更新/);
  assert.strictEqual(elements.get('deleteSource').disabled,false);
  assert.ok(!html.includes('根据修改后的内容生成新题目'));
  tabs.find(tab=>tab.dataset.view==='cards').onclick();
  assert.match(elements.get('cardList').innerHTML,/这是图片注释/);
  const editedHeading={innerHTML:''},editedPreview={innerHTML:''};
  const editedOverviewRow={dataset:{cardId:'fresh'},classList:{add(){}},scrollIntoView(){},
    querySelector(selector){return selector==='small'?editedHeading:selector==='.preview'?editedPreview:null}};
  elements.get('cardList').querySelectorAll=selector=>selector==='[data-card-id]'?[editedOverviewRow]:[];
  vm.runInContext("openKnowledge('fresh')",context);
  elements.get('knowledgeEdited').value='修改后立即显示的知识点';
  const syncsBeforeOverviewEdit=fullSyncRequests;
  await elements.get('saveKnowledge').onclick();
  assert.strictEqual(fullSyncRequests,syncsBeforeOverviewEdit);
  assert.match(editedHeading.innerHTML,/新的卡片标题/);
  assert.match(editedPreview.innerHTML,/修改后立即显示的知识点/);
  assert.strictEqual(elements.get('saveKnowledge').disabled,false);
  elements.get('cardList').querySelectorAll=()=>[];
  await vm.runInContext("pinCard('image')",context);
  assert.strictEqual(storedPin,'image');
  assert.ok(!html.includes('jumpPinnedManage'));
  assert.match(elements.get('cardList').innerHTML,/pinnedCard/);
  tabs.find(tab=>tab.dataset.view==='manage').onclick();
  let jumped=false;
  elements.get('cardList').querySelectorAll=selector=>selector==='[data-card-id]'?[{dataset:{cardId:'image'},closest(){return {open:false}},scrollIntoView(){jumped=true}}]:[];
  elements.get('jumpPinnedOverview').onclick();
  assert.ok(jumped);
  assert.strictEqual(vm.runInContext('activeView',context),'cards');
  assert.doesNotMatch(elements.get('cardList').innerHTML,/data-image-answer|data-image-favorite|data-image-clear/);
  await vm.runInContext("deleteKnowledgePoint('fresh')",context);
  assert.ok(cards.some(c=>c.id==='image'));
  cards.push({id:'adjacent',kind:'knowledge',note:'网络.md',h2:'结构图',section:'结构图',line:6,
    original:'打印机与图片说明',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('syncCards()',context);
  elements.get('cardSearch').value='打印机';
  jumped=false;
  const syncsBeforeSingleDelete=fullSyncRequests;
  await vm.runInContext("deleteKnowledgePoint('adjacent')",context);
  assert.strictEqual(fullSyncRequests,syncsBeforeSingleDelete);
  assert.ok(cards.some(c=>c.id==='image'));
  assert.strictEqual(elements.get('cardSearch').value,'');
  assert.ok(jumped);
  assert.match(elements.get('cardList').innerHTML,/currentEdit.*data-card-id="image"/);
  await vm.runInContext("deleteOverviewImage('image')",context);
  assert.strictEqual(fullSyncRequests,syncsBeforeSingleDelete);
  assert.ok(!cards.some(c=>c.id==='image'));
  assert.strictEqual(storedPin,null);
  for(let i=0;i<5;i++) cards.push({id:`batch-${i}`,kind:'knowledge',note:'网络.md',h2:'批次',section:'批次',line:10+i,
    original:`第 ${i+1} 题`,answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('syncCards()',context);
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  cards.push({id:'returned',kind:'knowledge',note:'网络.md',h2:'复习',section:'复习',line:20,
    original:'降档题',answer:'理解',options:['理解','不理解'],attempts:2,wrong:1,wrong_marked:0,favorite:0,random_return_pending:1});
  for(let i=0;i<5;i++) {
    assert.match(vm.runInContext('picked.id',context),/^batch-/);
    await vm.runInContext("choose('理解')",context);
  }
  assert.strictEqual(vm.runInContext('picked.id',context),'returned');
  await vm.runInContext("choose('理解')",context);
  assert.strictEqual(vm.runInContext('picked',context),null);
  for(let i=0;i<5;i++) cards.push({id:`repeat-${i}`,kind:'knowledge',note:'网络.md',h2:'复习',section:'复习',line:30+i,
    original:`复习 ${i+1}`,answer:'理解',options:['理解','不理解'],attempts:1,wrong:1,wrong_marked:0,favorite:0,random_return_pending:1});
  await vm.runInContext('syncCards()',context);
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  cards.push({id:'late-return',kind:'knowledge',note:'网络.md',h2:'复习',section:'复习',line:40,
    original:'稍后降档',answer:'理解',options:['理解','不理解'],attempts:2,wrong:1,wrong_marked:0,favorite:0,random_return_pending:1});
  await vm.runInContext('syncCards()',context);
  tabs.find(tab=>tab.dataset.view==='wrong').onclick();
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  for(let i=0;i<5;i++) {
    assert.match(vm.runInContext('picked.id',context),/^repeat-/);
    await vm.runInContext("choose('理解')",context);
  }
  assert.strictEqual(vm.runInContext('picked.id',context),'late-return');
  for(const [id,content] of [['split-first','123'],['split-second','abc'],['split-third','ABC']])
    cards.push({id,kind:'knowledge',note:'顺序.md',h2:'同一段',section:'同一段',line:12,
      original:content,answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('syncCards()',context);
  tabs.find(tab=>tab.dataset.view==='cards').onclick();
  const overview=elements.get('cardList').innerHTML;
  assert.ok(overview.indexOf('data-card-id="split-first"') < overview.indexOf('data-card-id="split-second"'));
  assert.ok(overview.indexOf('data-card-id="split-second"') < overview.indexOf('data-card-id="split-third"'));
  cards.push({id:'group-text',kind:'knowledge',note:'组合.md',h2:'图文',section:'图文',line:1,
    original:'文字说明',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0,
    merged_parts:[{id:'group-text',kind:'knowledge',original:'文字说明',section:'图文'},
      {id:'group-image-1',kind:'image',original:'图一',section:'图一',media_id:'m1',annotation:'注释一'},
      {id:'group-image-2',kind:'image',original:'图二',section:'图二',media_id:'m2',annotation:'注释二'}]});
  cards.push({id:'next-image',kind:'image',note:'组合.md',h2:'图文',section:'图文',line:2,
    original:'图三',answer:'了解',options:['了解','不了解'],media_id:'m3',attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('syncCards()',context);
  assert.match(elements.get('cardList').innerHTML,/文字说明[\s\S]*注释一[\s\S]*注释二/);
  assert.match(elements.get('cardList').innerHTML,/<div class="mergedPartHeader"><strong>图一<\/strong><button[^>]*data-image-annotation data-id="group-image-1"/);
  assert.match(elements.get('cardList').innerHTML,/data-merge-image data-id="next-image" data-previous="group-text"/);
  assert.match(elements.get('cardList').innerHTML,/data-edit-group data-id="group-text"/);
  assert.doesNotMatch(elements.get('cardList').innerHTML,/data-unmerge-card/);
  elements.get('groupEditModal').querySelector=selector=>selector==='.modalBox'?{scrollTop:45}:null;
  const groupInputs=new Map([
    ['[data-group-title="0"]',{value:'新组合标题'}],
    ['[data-group-content="0"]',{value:'修改组合文字'}],
    ['[data-group-annotation="1"]',{value:'修改图一注释'}],
    ['[data-group-annotation="2"]',{value:'修改图二注释'}]
  ]);
  elements.get('groupEditParts').querySelector=selector=>groupInputs.get(selector);
  context.fetch=async(path,request={})=>{
    if(path==='/api/cards/edit-group') {
      const {id,parts}=JSON.parse(request.body);
      assert.strictEqual(id,'group-text');
      assert.deepStrictEqual(parts.map(part=>part.id),['group-text','group-image-1','group-image-2']);
      cards.find(card=>card.id===id).original=parts[0].content;
      cards.find(card=>card.id===id).section=parts[0].title;
      cards.find(card=>card.id===id).merged_parts[0].original=parts[0].content;
      cards.find(card=>card.id===id).merged_parts[0].section=parts[0].title;
      cards.find(card=>card.id===id).merged_parts[1].annotation=parts[1].annotation;
      cards.find(card=>card.id===id).merged_parts[2].annotation=parts[2].annotation;
      return {ok:true,json:async()=>({id,updated:parts.length})};
    }
    return normalFetch(path,request);
  };
  vm.runInContext("openGroupEditor('group-text')",context);
  assert.match(elements.get('groupEditParts').innerHTML,/修改|文字说明[\s\S]*注释一[\s\S]*注释二/);
  await elements.get('saveGroupEdit').onclick();
  assert.match(elements.get('cardList').innerHTML,/修改组合文字[\s\S]*修改图一注释[\s\S]*修改图二注释/);
  context.fetch=normalFetch;
  cards.push({id:'study-delete',kind:'knowledge',note:'删除测试.md',h2:'测试',section:'测试',line:1,
    original:'待删除的知识点',answer:'理解',options:['理解','不理解'],attempts:0,wrong:0,wrong_marked:0,favorite:0});
  await vm.runInContext('refresh()',context);
  elements.get('section').value='删除测试.md';
  elements.get('studyOrder').value='source';
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.strictEqual(vm.runInContext('picked.id',context),'study-delete');
  const syncsBeforeCurrentDelete=fullSyncRequests;
  await elements.get('deleteCurrent').onclick();
  assert.strictEqual(fullSyncRequests,syncsBeforeCurrentDelete);
  assert.ok(!cards.some(card=>card.id==='study-delete'));
  assert.strictEqual(elements.get('section').value,'');
  let unexpectedRequests=0;
  context.fetch=async()=>{unexpectedRequests++;throw Error('不应请求服务器')};
  elements.get('section').value='';
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  elements.get('next').onclick();
  assert.strictEqual(unexpectedRequests,0);
  context.fetch=async()=>({ok:false,status:502,headers:{get:()=> 'text/html'},json:async()=>{throw SyntaxError('Unexpected token <')}});
  await assert.rejects(vm.runInContext("api('/api/cards')",context),/服务暂时无法连接（HTTP 502）/);
  context.fetch=async()=>({ok:false,status:502,json:async()=>{throw SyntaxError('Unexpected token <')}});
  await assert.rejects(vm.runInContext("api('/api/cards')",context),/服务暂时无法连接（HTTP 502）/);
  context.fetch=normalFetch;
  vm.runInContext(`all = [
    {id:'pending-random',note:'待抽.md',attempts:0,wrong_marked:0,favorite:0},
    {id:'pending-random-second',note:'待抽.md',attempts:0,wrong_marked:0,favorite:0},
    {id:'returned-random',note:'待抽.md',attempts:2,random_return_pending:1,wrong_marked:0,favorite:0},
    {id:'pending-wrong',note:'第一轮.md',attempts:1,wrong_marked:1,favorite:0,round1_done:0},
    {id:'pending-retry',note:'第二轮.md',attempts:2,wrong_marked:1,favorite:0,round1_done:1},
    {id:'pending-retry-second',note:'第二轮.md',attempts:2,wrong_marked:1,favorite:0,round1_done:1},
    {id:'completed',note:'已完成.md',attempts:1,wrong_marked:0,favorite:0}
  ]; randomBatchIds=[]`,context);
  const section=elements.get('section');
  section.value='';
  elements.get('studyOrder').value='source';
  tabs.find(tab=>tab.dataset.view==='random').onclick();
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'pending-random,pending-random-second,returned-random');
  elements.get('studyOrder').value='random';
  elements.get('studyOrder').onchange();
  assert.strictEqual(vm.runInContext('list.length',context),3);
  assert.match(vm.runInContext('list.map(card=>card.id).join(",")',context),/returned-random/);
  context.Math.random=()=>0;
  elements.get('shuffle').onclick();
  const firstRandomOrder=vm.runInContext('list.map(card=>card.id).join(",")',context);
  context.Math.random=()=>.99;
  elements.get('shuffle').onclick();
  assert.notStrictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),firstRandomOrder);
  elements.get('studyOrder').value='source';
  elements.get('studyOrder').onchange();
  assert.match(section.innerHTML,/value="待抽\.md"/);
  assert.doesNotMatch(section.innerHTML,/value="(?:第一轮|第二轮|已完成)\.md"/);
  section.value='待抽.md';
  tabs.find(tab=>tab.dataset.view==='wrong').onclick();
  assert.strictEqual(section.value,'');
  assert.match(section.innerHTML,/value="第一轮\.md"/);
  assert.doesNotMatch(section.innerHTML,/value="(?:待抽|第二轮|已完成)\.md"/);
  tabs.find(tab=>tab.dataset.view==='retry').onclick();
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'pending-retry,pending-retry-second');
  assert.match(section.innerHTML,/value="第二轮\.md"/);
  assert.doesNotMatch(section.innerHTML,/value="(?:待抽|第一轮|已完成)\.md"/);
  cards.splice(0,cards.length,...['review-a','review-b'].map((id,index)=>({
    id,kind:'knowledge',note:'复习.md',h2:'复习',section:'复习',line:index+1,original:id,
    answer:'理解',options:['理解','不理解'],attempts:2,wrong:1,wrong_marked:1,
    favorite:0,round1_done:1,retry_deferred:0
  })));
  await vm.runInContext('syncCards()',context);
  section.value='';
  elements.get('studyOrder').value='source';
  tabs.find(tab=>tab.dataset.view==='retry').onclick();
  await vm.runInContext("choose('不理解')",context);
  assert.strictEqual(cards[0].retry_deferred,1);
  assert.strictEqual(vm.runInContext('picked.id',context),'review-b');
  await vm.runInContext('refresh()',context);
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'review-b');
  await vm.runInContext("choose('不理解')",context);
  assert.strictEqual(vm.runInContext('list.map(card=>card.id).join(",")',context),'review-a,review-b');
  assert.ok(cards.every(card=>card.retry_deferred===0));
  console.log('knowledge UI tests passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
