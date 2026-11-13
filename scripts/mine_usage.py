"""Mine observable skill use without treating injected catalogs or copied output as use.

Python standard library only. Input is read-only; this program never executes recorded
commands. Output is metadata and references, not conversation bodies or reasoning.
"""
from __future__ import annotations
import argparse, ast, collections, datetime, hashlib, io, json, os, pathlib, re, sys
from urllib.parse import unquote, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAX_LINE = 16 * 1024 * 1024
TOOL_READ = re.compile(r'\b(?:Get-Content|cat|head|tail|sed|read_file|read_text|ReadAllText|ReadAllLines)\b', re.I)
TOOL_RUN = re.compile(r'\b(?:python\d*|python\.exe|uv|node|node\.exe|bash|pwsh|powershell|npx|bun)\b|(?:^|[;&])\s*(?:\.\/|\/|[A-Za-z]:)', re.I)
SKILL_PATH = re.compile(r'''(?:[A-Za-z]:[/\\]|/|~[/\\])[^\r\n"'<>|;]*?[/\\]SKILL\.md''', re.I)
SCRIPT_PATH = re.compile(r'''(?:[A-Za-z]:[/\\]|/|~[/\\])[^\r\n"'<>|;]*?[/\\](?:scripts|bin)[/\\][^\s"'<>|;]+\.(?:py|js|mjs|cjs|ps1|sh)''', re.I)
RANK = {'declared': 0, 'read': 1, 'executed': 2, 'outcome_reviewed': 3}

def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as f:
        for row in rows: f.write(json.dumps(row, ensure_ascii=False, separators=(',',':'))+'\n')

def digest(value): return hashlib.sha256(value.encode('utf-8')).hexdigest()

def norm(path):
    path = unquote(str(path)).replace('file:///', '').replace('\\', '/')
    while '//' in path: path = path.replace('//','/')
    path = re.sub(r'^/\?/', '', path)
    return path.rstrip('/').lower() if re.match(r'^[A-Za-z]:/',path) else path.rstrip('/')

def text_content(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return '\n'.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type') in ('text','input_text','output_text'))
    return ''

def script_root(path):
    path = path.replace('\\','/')
    found = re.search(r'/(?:scripts|bin)/',path,re.I)
    return path[:found.start()]+'/SKILL.md' if found else path

def plausible_skill_path(path):
    low=norm(path)
    return not any(c in low for c in '$*?{}') and ('/skills/' in low or '/plugins/cache/' in low or '/skill/' in low)

def extract_commands(name, args):
    """Only recognized executable arguments; never scan an entire arbitrary payload."""
    if isinstance(args,str):
        try: parsed=json.loads(args)
        except (ValueError,TypeError): parsed=None
        if isinstance(parsed,dict): args=parsed
    if isinstance(args,dict):
        if name.lower().split('.')[-1] in ('read','read_file','readfile'):
            p=args.get('file_path',args.get('path',''))
            return [('read_file '+str(p),'read')]
        cmd=args.get('cmd',args.get('command'))
        if isinstance(cmd,list): cmd=' '.join(str(x) for x in cmd)
        if isinstance(cmd,str): return [(cmd,'shell')]
        if name.lower().split('.')[-1] in ('skill','skill_invoke','invoke_skill'):
            return [(str(args.get('skill',args.get('name',''))),'declared')]
        if 'code' in args and isinstance(args['code'],str): args=args['code']
        else: return []
    if isinstance(args,str) and (name.endswith('exec') or name.endswith('run_code') or name.endswith('js')):
        # JS orchestrators often quote actual command strings. Extract those values,
        # not source literals that merely create files or report old commands.
        found=[]
        for m in re.finditer(r'''(?:cmd|command)\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',args):
            try: value=json.loads(m[1]) if m[1].startswith('"') else ast.literal_eval(m[1])
            except (ValueError,SyntaxError): continue
            found.append((value,'shell'))
        return found
    return []

def classify(command, mode):
    if mode=='declared': return []  # A name alone is resolved later against inventory.
    # Read commands may be piped/combined; SKILL strings embedded in here-strings,
    # patches, test-fixture source or write statements are not execution evidence.
    if any(x in command for x in ('*** Begin Patch', '@\'','@"', '<<\'','<<"')): return []
    found=[]
    for segment in re.split(r';|\n|&&',command):
        executable_text=re.sub(r'''"(?:`.|\\.|[^"\\])*"|'(?:''|[^'])*' ''', ' ', segment+' ',flags=re.X)
        read=bool(TOOL_READ.search(executable_text))
        run=bool(TOOL_RUN.search(segment))
        if read:
            for m in SKILL_PATH.finditer(segment):
                p=m[0]
                if plausible_skill_path(p): found.append((p,'read'))
        if run and not read and not re.search(r'\b(?:Get-ChildItem|rg|find|ls|echo|Write-Output|print)\b',segment,re.I):
            for m in SCRIPT_PATH.finditer(segment):
                p=script_root(m[0])
                # A plugin-level bin/script is not automatically a skill. Only an
                # actual SKILL ancestor or an explicit /skills/ path establishes it.
                low=norm(p)
                if '/plugins/cache/' in low and '/skills/' not in low.split('/plugins/cache/',1)[1] and not pathlib.Path(p).is_file():continue
                if plausible_skill_path(p): found.append((p,'executed'))
    return sorted(set(found))

def output_status(output, explicit=None):
    if explicit is not None:
        if isinstance(explicit,int): return 'success' if explicit==0 else 'failed'
        if str(explicit).lower() in ('failed','error','interrupted','cancelled','canceled'): return 'failed'
    if isinstance(output,dict):
        if 'exit_code' in output and output['exit_code'] is not None: return output_status('',output['exit_code'])
        if output.get('isError') is True: return 'failed'
        output=json.dumps(output,ensure_ascii=False)
    output=str(output or '')
    # Native output may be JSON wrapped in a text-content envelope.
    m=re.search(r'(?:Process exited with code|exit code:|"exit_code"\s*:)\s*(-?\d+)',output,re.I)
    if m:return 'success' if int(m[1])==0 else 'failed'
    if re.search(r'(?:Cannot find path|FileNotFoundError|is not recognized|CommandNotFoundException)',output): return 'failed'
    return 'unknown'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=pathlib.Path,default=ROOT)
    parser.add_argument('--cutoff',default=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),help='Exclusive UTC ISO timestamp; defaults to this invocation start time')
    parser.add_argument('--source',action='append',help='Override sources: FAMILY=directory, repeatable (codex_live, codex_cli, claude, dsh).')
    parser.add_argument('--discover-hosts',action='store_true',help='Explicitly scan known history directories in this user home')
    parser.add_argument('--home',type=pathlib.Path,default=pathlib.Path.home(),help='Home used only with --discover-hosts')
    opts=parser.parse_args();root=opts.root
    source_roots=[]
    if opts.discover_hosts:
        source_roots=[('codex_live',opts.home/'.codex/sessions'),('codex_archived',opts.home/'.codex/archived_sessions'),('codex_cli',opts.home/'.codex-cli/sessions'),('claude',opts.home/'.claude/projects'),('dsh',opts.home/'.dsh/sessions')]
    if opts.source:
        for value in opts.source:
            if '=' not in value or not all(value.split('=',1)):
                parser.error('--source must be FAMILY=directory')
            family,path=value.split('=',1)
            if family not in {'codex_live','codex_archived','codex_cli','codex_selected_export','claude','dsh'}:
                parser.error('Unsupported source family: '+family)
            source_roots.append((family,pathlib.Path(path)))
    if not source_roots:
        parser.error('Select --source FAMILY=directory or explicitly enable --discover-hosts')
    files=[]
    for family,p in source_roots:
        if p.exists():files.extend((family,x,x.stat().st_size) for x in sorted(p.rglob('*.jsonl.zstd' if family=='dsh' else '*.jsonl')))
    manifests=[];calls={};call_lookup={};declared=[];sessions={};duplicate_calls=0;excluded=collections.Counter();event_types=collections.Counter();timestamps=[]
    for family,path,limit in files:
        source={'path':str(path),'family':family,'frozen_bytes':limit,'complete_bytes':0,'lines':0,'oversize_lines':0,'invalid_lines':0,'excluded_after_cutoff':0}
        h=hashlib.sha256();thread='unknown:'+digest(str(path))[:12];cwd='';turn='unknown';first_meta=False;created_at=''
        compressed=family=='dsh'
        if compressed:
            try:
                from compression import zstd
            except ImportError:
                source['unsupported']='DSH zstd parsing requires Python 3.14 compression.zstd';manifests.append(source);continue
            with path.open('rb') as raw:blob=raw.read(limit)
            source['sha256_compressed_prefix']=hashlib.sha256(blob).hexdigest()
            opened=zstd.open(io.BytesIO(blob),'rb')
        else:opened=path.open('rb')
        with opened as f:
            while compressed or f.tell()<limit:
                line_no=source['lines']+1
                remaining=MAX_LINE+1 if compressed else limit-f.tell();line=f.readline(min(MAX_LINE+1,remaining))
                if not line:break
                if not line.endswith(b'\n'):
                    if len(line)>MAX_LINE:
                        source['oversize_lines']+=1
                        h.update(line)
                        while (compressed or f.tell()<limit) and line and not line.endswith(b'\n'):
                            line=f.readline(MAX_LINE+1 if compressed else min(MAX_LINE+1,limit-f.tell()));h.update(line)
                        if line.endswith(b'\n'):source['complete_bytes']=f.tell();source['lines']+=1
                        continue
                    source['trailing_incomplete_bytes']=len(line);break
                h.update(line);source['complete_bytes']=f.tell();source['lines']+=1
                try:o=json.loads(line)
                except (ValueError,UnicodeError):source['invalid_lines']+=1;continue
                if compressed:
                    dt=o.get('time',o.get('createdAt'))
                    stamp=datetime.datetime.fromtimestamp(dt/1000,datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z') if isinstance(dt,(int,float)) else ''
                    kind=o.get('type');d=o.get('data',{});translated=None
                    if kind=='session':translated={'type':'session_meta','payload':{'id':o.get('id'),'timestamp':stamp,'cwd':o.get('cwd',''),'parent_thread_id':o.get('parentSession')}}
                    elif kind=='turn/start':translated={'type':'event_msg','payload':{'type':'task_started','turn_id':str(d.get('turn'))}}
                    elif kind=='tool/call':translated={'type':'response_item','payload':{'type':'function_call','call_id':d.get('callId'),'name':d.get('name',''),'arguments':d.get('arguments',{})}}
                    elif kind=='tool/result':
                        msg=d.get('message',{});translated={'type':'response_item','payload':{'type':'function_call_output','call_id':msg.get('toolCallId'),'output':{'content':msg.get('content',[]),'isError':msg.get('isError',False)}}}
                    elif kind=='assistant/message':translated={'type':'response_item','payload':{'type':'message','role':'assistant','content':d.get('message',{}).get('content',[])}}
                    if translated is None:excluded['dsh:'+str(kind)]+=1;continue
                    o={**translated,'timestamp':stamp}
                ts=o.get('timestamp','');typ=o.get('type');q=o.get('payload',{})
                if not isinstance(q,dict):q={}
                event_types[(family,typ)]+=1
                if typ=='session_meta':
                    if first_meta:
                        excluded['copied_session_meta']+=1
                        continue
                    first_meta=True;created_at=q.get('timestamp',ts)
                    thread=q.get('id',q.get('session_id',thread));cwd=q.get('cwd','');turn='unknown'
                    sessions.setdefault(thread,{'thread_id':thread,'cwd':cwd,'family':'dsh' if compressed else 'codex','sources':[],'created_at':created_at,'parent_thread_id':q.get('parent_thread_id'),'forked_from_id':q.get('forked_from_id'),'git_commit':q.get('git',{}).get('commit_hash'),'git_branch':q.get('git',{}).get('branch')})['sources'].append(str(path))
                    source['thread_id']=thread
                    continue
                if family=='claude':
                    thread=o.get('sessionId',o.get('session_id',thread));cwd=o.get('cwd',cwd)
                    sessions.setdefault(thread,{'thread_id':thread,'cwd':cwd,'family':'claude','sources':[]})
                    if str(path) not in sessions[thread]['sources']:sessions[thread]['sources'].append(str(path))
                    source['thread_id']=thread
                if ts and ts>=opts.cutoff:
                    source['excluded_after_cutoff']+=1;continue
                if family!='claude' and created_at and ts and ts<created_at:
                    excluded['copied_events_before_file_session_created']+=1
                    continue
                if typ=='turn_context':turn=q.get('turn_id',turn);cwd=q.get('cwd',cwd);continue
                if typ=='event_msg' and q.get('type')=='task_started':turn=q.get('turn_id',turn);continue
                if typ=='user' and family=='claude' and not any(x.get('type')=='tool_result' for x in o.get('message',{}).get('content',[]) if isinstance(x,dict)):
                    turn=o.get('uuid',turn)
                # Only allowlisted observable messages/calls/results are parsed.
                candidates=[];results=[];announcement=''
                if typ=='response_item' and q.get('type') in ('function_call','custom_tool_call'):
                    name=q.get('name','');args=q.get('arguments',q.get('input',{}));cid=q.get('call_id',q.get('id',''))
                    candidates=[(cid,name,args,'response')]
                elif typ=='response_item' and q.get('type') in ('function_call_output','custom_tool_call_output'):
                    results=[(q.get('call_id',q.get('id','')),q.get('output',''),None)]
                elif typ=='response_item' and q.get('type')=='message' and q.get('role')=='assistant' and q.get('channel') not in ('analysis','summary'):
                    announcement=text_content(q.get('content',[]))
                elif typ=='event_msg' and q.get('type')=='agent_message' and q.get('phase') not in ('analysis','summary'):
                    announcement=q.get('message','')
                elif typ=='event_msg' and q.get('type')=='exec_command_end':
                    cid=q.get('call_id','');cmd=q.get('command','')
                    candidates=[(cid,'exec_command',{'cmd':cmd},'exec_end')]
                    results=[(cid,q.get('aggregated_output',''),q.get('exit_code'))]
                elif typ=='event_msg' and q.get('type')=='item_completed':
                    item=q.get('item',{});it=item.get('type')
                    if it=='CommandExecution':
                        cid=item.get('id','');cmd=item.get('command','')
                        if isinstance(cmd,list):cmd=cmd[-1] if len(cmd)>1 else ' '.join(cmd)
                        candidates=[(cid,'exec_command',{'cmd':cmd},'native')]
                        results=[(cid,item.get('aggregated_output',''),item.get('exit_code'))]
                    elif it=='McpToolCall':
                        cid=item.get('id','');candidates=[(cid,item.get('tool',''),item.get('arguments',{}),'native_mcp')]
                        results=[(cid,item.get('result',{}),item.get('status'))]
                    elif it=='AgentMessage' and item.get('phase') not in ('analysis','summary'):
                        announcement=text_content(item.get('content',[]))
                    else:excluded['native_'+str(it)]+=1
                elif family=='claude' and typ=='assistant':
                    for item in o.get('message',{}).get('content',[]):
                        if not isinstance(item,dict):continue
                        if item.get('type')=='tool_use':candidates.append((item.get('id',''),item.get('name',''),item.get('input',{}),'claude'))
                        elif item.get('type')=='text':announcement+='\n'+item.get('text','')
                elif family=='claude' and typ=='user':
                    for item in o.get('message',{}).get('content',[]):
                        if isinstance(item,dict) and item.get('type')=='tool_result':results.append((item.get('tool_use_id',''),'', 'failed' if item.get('is_error') else 'completed'))
                else:excluded[str(typ)+':'+str(q.get('type'))]+=1
                if announcement and len(announcement)<6000 and re.search(r'(?:使用|采用|调用|应用|using|use |following).{0,100}(?:skill|技能)|(?:skill|技能).{0,100}(?:使用|采用|调用|应用)',announcement,re.I):
                    # Keep only name candidates; full prose might contain sensitive user data.
                    names=re.findall(r'(?:\$|`)([a-z][a-z0-9_-]*(?::[a-z][a-z0-9_-]*)?)',announcement)
                    for name in names:declared.append({'name':name,'thread_id':thread,'turn_id':turn,'cwd':cwd,'timestamp':ts,'path':str(path),'line':line_no})
                for cid,name,args,source_kind in candidates:
                    if re.search(r'(?:apply_patch|write_file|spawn_agent|send_message|followup_task)',name):continue
                    for command,mode in extract_commands(name,args):
                        if mode=='declared':
                            declared.append({'name':command,'thread_id':thread,'turn_id':turn,'cwd':cwd,'timestamp':ts,'path':str(path),'line':line_no});continue
                        matches=classify(command,mode)
                        if not matches:continue
                        # Identical calls copied into forked pages count once globally.
                        command_hash=digest(command)
                        key=(cid or command_hash,command_hash)
                        if key in calls:
                            duplicate_calls+=1
                            continue
                        calls[key]={'call_id':cid,'thread_id':thread,'turn_id':turn,'cwd':cwd,'timestamp':ts,'family':family if family in ('claude','dsh') else 'codex','source_kind':source_kind,'source_path':str(path),'line':line_no,'matches':matches,'status':'unknown','command_hash':command_hash,'output_discarded':bool(re.search(r'\|\s*Out-Null|>\s*/dev/null|>\s*\$null',command,re.I)),'result_evidence':[]}
                        call_lookup.setdefault((thread,cid),[]).append(calls[key])
                        if ts:timestamps.append(ts)
                for cid,output,status in results:
                    # Output is examined only for process status, never for skill paths/names.
                    linked_calls=call_lookup.get((thread,cid),[])
                    for call in linked_calls:
                        result_status=output_status(output,status)
                        if result_status!='unknown' and (len(linked_calls)==1 or call['source_kind'] in ('native','exec_end')):call['status']=result_status
                        ref={'path':str(path),'line':line_no,'call_id':cid,'kind':'tool_result'}
                        if ref not in call['result_evidence']:call['result_evidence'].append(ref)
        source['sha256_complete_prefix']=h.hexdigest();manifests.append(source)
        print(json.dumps({'source':str(path),'lines':source['lines'],'skill_calls_so_far':len(calls)},ensure_ascii=False),file=sys.stderr)
    inventory=[]
    inv=root/'data/inventory.jsonl'
    if inv.exists():inventory=[json.loads(x) for x in inv.read_text(encoding='utf-8-sig').splitlines() if x.strip()]
    bypath={};byname=collections.defaultdict(list);bylogical={}
    for row in inventory:
        for p in [row.get('path',''),row.get('resolved_path','')]+row.get('locations',[]):
            if isinstance(p,dict):p=p.get('path','')
            if p:bypath[norm(p)]=row
        byname[row.get('name','')].append(row)
        bylogical[row['logical_id'].lower()]=row
    def resolve(path):
        row=bypath.get(norm(path))
        name=path.replace('\\','/').split('/')[-2]
        if row:return row.get('logical_id'),row.get('id'),row.get('name',name),'exact_path_current_catalog'
        # Preserve historical plugin namespaces and versions; current content hash is unknown.
        low=path.replace('\\','/')
        low=re.sub(r'(/plugins/cache/openai-primary-runtime)/skills/',r'\1/',low,flags=re.I)
        m=re.search(r'/plugins/cache/([^/]+)/([^/]+)/([^/]+)/skills/(.*)/SKILL\.md$',low,re.I)
        if m:logical='skill:'+m[1]+'/'+m[2]+'/'+name
        elif '/.system/' in low:logical='skill:system/'+name
        elif '/.codex/skills/' in low or '/.claude/skills/' in low:logical='skill:local/'+name
        else:logical='skill:historical-unresolved/'+name
        current=bylogical.get(logical.lower())
        if current:return current['logical_id'],current['id'],current.get('name',name),'historical_namespace_name_match_content_unknown'
        return logical,None,name,'historical_path_unmatched'
    episodes={};global_ops=set()
    for call in sorted(calls.values(),key=lambda c:(c['source_kind'] not in ('native','exec_end'),c['status']=='unknown')):
        for path,level in call['matches']:
            logical,current_id,name,mapping=resolve(path)
            # Nested executable tool + outer wrapper: same command hash/thread/turn is one op.
            op=(call['thread_id'],call['turn_id'],logical,call['command_hash'])
            if op in global_ops:continue
            global_ops.add(op)
            key=(call['thread_id'],call['turn_id'],logical)
            row=episodes.setdefault(key,{'id':'usage:'+digest('|'.join(key))[:20],'logical_id':logical,'skill_id':None,'current_catalog_id':current_id,'skill_version_at_use':'unknown','skill_path':path,'name':name,'mapping_basis':mapping,'thread_id':call['thread_id'],'turn_id':call['turn_id'],'cwd':call['cwd'],'timestamp':call['timestamp'],'family':call['family'],'level':level,'status':call['status'],'levels_observed':[],'operation_count':0,'successful_operations':0,'failed_operations':0,'unknown_operations':0,'discarded_output_operations':0,'evidence':[],'outcome':{'status':'unknown','claim':'Only observable skill interaction is established; project benefit has not been independently attributed.','validation_level':'not_reviewed','evidence':[]},'dedup_key':'|'.join(key)})
            if RANK[level]>RANK[row['level']]:row['level']=level
            if level not in row['levels_observed']:row['levels_observed'].append(level)
            row['operation_count']+=1
            if call['output_discarded']:row['discarded_output_operations']+=1
            row[{'success':'successful_operations','failed':'failed_operations','unknown':'unknown_operations'}[call['status']]]+=1
            row['evidence'].append({'path':call['source_path'],'line':call['line'],'call_id':call['call_id'],'kind':level,'command_sha256':call['command_hash'],'output_discarded_in_command':call['output_discarded']})
            row['evidence'].extend(call['result_evidence'][:2])
            if call['timestamp'] and call['timestamp']<row['timestamp']:row['timestamp']=call['timestamp']
    # Declared only is not counted as observed read/run and must match a catalog name.
    for d in declared:
        name=d['name'].split(':')[-1];rows=byname.get(name,[])
        logicals={r['logical_id'] for r in rows}
        if len(logicals)!=1:continue
        logical=next(iter(logicals));key=(d['thread_id'],d['turn_id'],logical)
        if key in episodes:
            if 'declared' not in episodes[key]['levels_observed']:episodes[key]['levels_observed'].append('declared')
            continue
        episodes[key]={'id':'usage:'+digest('|'.join(key))[:20],'logical_id':logical,'skill_id':None,'current_catalog_id':None,'skill_version_at_use':'unknown','skill_path':None,'name':name,'mapping_basis':'unique_declared_catalog_name','thread_id':d['thread_id'],'turn_id':d['turn_id'],'cwd':d['cwd'],'timestamp':d['timestamp'],'family':sessions.get(d['thread_id'],{}).get('family','unknown'),'level':'declared','status':'unknown','levels_observed':['declared'],'operation_count':0,'successful_operations':0,'failed_operations':0,'unknown_operations':0,'evidence':[{'path':d['path'],'line':d['line'],'kind':'public_announcement'}],'outcome':{'status':'unknown','claim':'Public declaration only; actual loading, use and benefit are not established.','validation_level':'not_reviewed','evidence':[]},'dedup_key':'|'.join(key)}
    projects={}
    for row in episodes.values():
        cwd=norm(row['cwd']);pid='workspace:'+digest(cwd or row['thread_id'])[:16];row['project_id']=pid
        project=projects.setdefault(pid,{'id':pid,'kind':'observed_workspace','cwd':row['cwd'],'title':row['cwd'] or 'Unknown workspace','thread_ids':[],'project_group_id':None,'project_group_basis':'not_established'})
        if row['thread_id'] not in project['thread_ids']:project['thread_ids'].append(row['thread_id'])
        row['summary']={'read':'Observed skill document read','executed':'Observed bundled script execution','declared':'Public declaration only','outcome_reviewed':'Independently reviewed contribution'}[row['level']]+': '+row['name']
        if row['failed_operations'] and row['successful_operations']:row['status']='mixed'
        elif row['failed_operations']:row['status']='failed'
        elif row['successful_operations']:row['status']='success'
    # Existing .git metadata is read-only evidence for merging worktrees into a repo.
    for project in projects.values():
        p=pathlib.Path(project['cwd']);marker=p/'.git'
        try:
            if marker.is_file():
                value=marker.read_text(encoding='utf-8').strip()
                if value.startswith('gitdir:'):
                    target=pathlib.Path(value[7:].strip())
                    if not target.is_absolute():target=(p/target).resolve()
                    common=target/'commondir'
                    canonical=(target/common.read_text(encoding='utf-8').strip()).resolve() if common.exists() else target.resolve()
                    project['project_group_id']='repo:'+digest(norm(canonical))[:16];project['project_group_basis']={'git_marker':str(marker),'common_git_dir':str(canonical),'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
            elif marker.is_dir():
                project['project_group_id']='repo:'+digest(norm(marker.resolve()))[:16];project['project_group_basis']={'git_marker':str(marker),'common_git_dir':str(marker.resolve()),'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        except (OSError,UnicodeError):pass
    review_source=root/'reports/usage-evidence.jsonl';review_matches=[]
    if review_source.exists():
        for line_no,line in enumerate(review_source.read_text(encoding='utf-8').splitlines(),1):
            if not line.strip():continue
            review=json.loads(line);match=review['match'];matched=[]
            for row in episodes.values():
                if row['thread_id']==match['thread_id'] and row['turn_id']==match['turn_id'] and row['name']==match['skill_name']:
                    row['outcome']=review['outcome'];row['manual_review_id']=review['id'];row['manual_review_evidence']={'path':str(review_source),'line':line_no};row['knowledge_feedback']=review.get('knowledge_feedback',{});row['assessment']=review.get('assessment',{});matched.append(row['id'])
            review_matches.append({'review_id':review['id'],'matched_usage_ids':matched})
    data=sorted(episodes.values(),key=lambda r:(r['timestamp'],r['id']))
    jsonl(root/'data/usage.jsonl',data);jsonl(root/'data/projects.jsonl',projects.values())
    stats=collections.Counter(r['level'] for r in data)
    rollups=collections.defaultdict(lambda:{'episodes':0,'read_episodes':0,'executed_episodes':0,'declared_only_episodes':0,'workspaces':set(),'threads':set(),'process_success_episodes':0})
    for r in data:
        x=rollups[r['logical_id']];x['episodes']+=1;x['workspaces'].add(r['project_id']);x['threads'].add(r['thread_id'])
        if 'read' in r['levels_observed']:x['read_episodes']+=1
        if 'executed' in r['levels_observed']:x['executed_episodes']+=1
        if r['level']=='declared':x['declared_only_episodes']+=1
        if r['status']=='success':x['process_success_episodes']+=1
    rolls=[]
    for key,value in rollups.items():
        value['workspace_count']=len(value.pop('workspaces'));value['thread_count']=len(value.pop('threads'));rolls.append({'logical_id':key,**value})
    coverage={'schema':'skill-usage-evidence/v1','snapshot_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'history_cutoff_exclusive_utc':opts.cutoff,'cutoff_reason':'Explicit exclusive timestamp (default: invocation start). Select a pre-audit cutoff to exclude audit activity.','source_roots':[{'family':f,'path':str(p),'exists':p.exists()} for f,p in source_roots],'source_files':len(files),'complete_bytes':sum(x['complete_bytes'] for x in manifests),'sources':manifests,'observed_session_ids':len(sessions),'candidate_skill_calls':len(calls),'deduplicated_call_records':duplicate_calls,'usage_episodes':len(data),'episodes_by_level':dict(stats),'skills_with_observed_reads_or_execution':len({r['logical_id'] for r in data if r['level']!='declared'}),'skills_with_any_declaration':len(rollups),'observed_workspaces':len(projects),'verified_repository_groups':len({p['project_group_id'] for p in projects.values() if p['project_group_id']}),'unmapped_workspaces':sum(not p['project_group_id'] for p in projects.values()),'earliest_observed_use':min(timestamps,default=None),'latest_observed_use':max(timestamps,default=None),'rollups':sorted(rolls,key=lambda r:-r['episodes']),'excluded_types':{str(k):v for k,v in excluded.items()},'limitations':['Only explicitly selected source directories are inspected; coverage of deleted, inaccessible or unselected history is unknown.','The source_roots and per-file manifest define observed coverage; supported adapters do not imply that every provider was scanned.','Only recognizable concrete skill-file reads, bundled script executions and explicit named public declarations are counted. Relative variable-only paths and unrecognized tool formats can be missed.','Skill reading and successful subprocess exit do not prove instruction adherence, product correctness, time savings or causal benefit.','Historical skill-content hashes are unknown unless independently recovered; current catalog hashes are never assigned retroactively.','cwd workspaces are not project counts. Only existing .git common-dir evidence merges worktrees. A repo may still contain multiple products.','Public announcements are separated from observed reads/runs; process status from a compound shell command does not establish every subcommand succeeded.','No hidden reasoning, injected catalogs, encrypted payload, copied tool output or attachments are mined for skill-use evidence.']}
    coverage['manual_review_matches']=review_matches
    coverage['manually_reviewed_episodes']=sum('manual_review_id' in r for r in data)
    coverage['verified_help_real_task_skills']=sorted({r['logical_id'] for r in data if r['outcome']['status']=='verified_help' and r['outcome'].get('scope')!='synthetic_validation'})
    coverage['verified_help_real_task_workspaces']=sorted({r['project_id'] for r in data if r['outcome']['status']=='verified_help' and r['outcome'].get('scope')!='synthetic_validation'})
    coverage['discarded_output_operations']=sum(r.get('discarded_output_operations',0) for r in data)
    coverage['episodes_with_only_discarded_output']=sum(r.get('discarded_output_operations',0)>0 and r.get('discarded_output_operations')==r['operation_count'] and r['level']=='read' for r in data)
    coverage['limitations'].extend(['DSH .jsonl.zstd evidence line numbers address the decompressed UTF-8 JSONL; both compressed prefix and decoded complete-prefix SHA are recorded. Python 3.14 compression.zstd is required for this source adapter.','An episode is a unique caller thread + recorded turn + logical skill, not an independent user task. Steering messages can share a turn.','Reads piped to Out-Null/dev-null are flagged; file access without returned content does not demonstrate agent knowledge acquisition.','Only the first session_meta belongs to a file; later inherited metadata and events dated before that file session creation are excluded. Recovered copied-only history without the original caller is not attributed as a fresh use.'])
    coverage['limitations'].append('Variable/wildcard skill paths are not statically resolved; plugin-level helper scripts without a known SKILL ancestor are omitted. Catalog mapping by historical namespace/name does not establish the content version used.')
    catalog_logicals={r['logical_id'] for r in inventory}
    observed_logicals={r['logical_id'] for r in data if r['level']!='declared'}
    coverage['current_catalog_logical_skills_with_observed_activity']=len(observed_logicals & catalog_logicals)
    coverage['historical_only_or_unmapped_logical_skills']=sorted(observed_logicals-catalog_logicals)
    dump(root/'reports/usage-coverage.json',coverage)
    print(json.dumps({k:coverage[k] for k in ['source_files','complete_bytes','observed_session_ids','usage_episodes','episodes_by_level','skills_with_observed_reads_or_execution','observed_workspaces','verified_repository_groups','earliest_observed_use','latest_observed_use']},ensure_ascii=False))

if __name__=='__main__':main()
