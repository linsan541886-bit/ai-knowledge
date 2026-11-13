#!/usr/bin/env python3
"""Build a generation from inventory/usage evidence. Never overwrite a written ledger."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

def dump(x):
    return json.dumps(x,ensure_ascii=False,sort_keys=True,allow_nan=False)

def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding='utf-8-sig').splitlines() if x.strip()]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def short(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:18]

def normalize_path(value):
    # This import describes Windows observations; POSIX IDs are not case folded.
    text=(value or '').replace('\\','/').rstrip('/')
    if len(text)>2 and text[1]==':' or text.startswith('//'):
        return text.casefold()
    return text

def when(value):
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,AttributeError):
        return None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    src=args.source.resolve(); out=(args.out or src).resolve()
    if (out/'data/ledger.jsonl').exists() and (out/'data/ledger.jsonl').stat().st_size:
        raise ValueError('This generation has a ledger. Propose changes or build a NEW generation; never overwrite its baseline.')
    inventory=read_lines(src/'data/inventory.jsonl')
    usage=read_lines(src/'data/usage.jsonl')
    projects=read_lines(src/'data/projects.jsonl')
    if not inventory:
        raise ValueError('No inventory. Run inventory.py first.')
    ic=json.loads((src/'reports/inventory-coverage.json').read_text(encoding='utf-8'))
    uc=json.loads((src/'reports/usage-coverage.json').read_text(encoding='utf-8')) if (src/'reports/usage-coverage.json').exists() else {}
    curated=json.loads((src/'data/curated.json').read_text(encoding='utf-8')) if (src/'data/curated.json').exists() else {'nodes':[],'edges':[]}
    extra_reviews=read_lines(src/'reports/high-frequency-review.jsonl')
    by_usage={u['id']:u for u in usage}
    for review in extra_reviews:
        if review['usage_id'] in by_usage:
            u=by_usage[review['usage_id']]
            u['outcome']=review['outcome'];u['assessment']=review['assessment']
            u['manual_review_id']=review['id']
            u['manual_review_evidence']={'path':str(src/'reports/high-frequency-review.jsonl')}
    nodes={}; edges={}; now=datetime.now(timezone.utc).isoformat()
    def node(i,k,title,summary='',properties=None,evidence=None,tags=None,status='active',aliases=None):
        if i in nodes:
            raise ValueError('duplicate node: '+i)
        nodes[i]={'id':i,'kind':k,'title':title,'summary':summary,'status':status,'properties':properties or {},'evidence':evidence or [],'tags':tags or [],'aliases':aliases or [],'updated_at':now}
        return nodes[i]
    def edge(s,t,r,summary='',evidence=None,properties=None):
        i='edge:'+short(s+'|'+t+'|'+r)
        edges[i]={'id':i,'source':s,'target':t,'relation':r,'summary':summary,'evidence':evidence or [],'properties':properties or {}}
    file_hashes={}
    def evidence(rel,line=1,kind='derived_snapshot'):
        p=src/rel
        if not p.exists() and rel.startswith('docs/'):
            p=Path(__file__).resolve().parents[1]/rel
        if not p.exists():
            return []
        if str(p) not in file_hashes:
            file_hashes[str(p)]=sha(p)
        return [{'path':str(p),'line':line,'sha256':file_hashes[str(p)],'kind':kind}]
    grouped=defaultdict(list); paths={}
    for item in inventory:
        grouped[item['logical_id']].append(item)
        for path in item.get('locations',[item['path']]):
            paths[normalize_path(path)]=item['logical_id']
        if item.get('resolved_path'):
            paths.setdefault(normalize_path(item['resolved_path']),item['logical_id'])
    by_skill=defaultdict(list)
    for u in usage:
        # Only exact path remapping can resolve a formerly unknown current catalog entry.
        original=u.get('logical_id')
        if original not in grouped and normalize_path(u.get('skill_path')) in paths:
            u['original_logical_id']=original
            u['logical_id']=paths[normalize_path(u['skill_path'])]
            u['current_mapping_basis']='exact_path_to_new_inventory; historical_content_version_unknown'
        by_skill[u['logical_id']].append(u)
    project_by={p['id']:p for p in projects}
    cutoff=when(uc.get('history_cutoff_exclusive_utc'))
    def metrics(us):
        actual=[u for u in us if set(u.get('levels_observed',[u['level']])) & {'read','executed'}]
        reviewed=[u for u in actual if u.get('manual_review_id') or u.get('outcome',{}).get('status') in {'verified_help','mixed','failure'}]
        helped=[u for u in reviewed if u['outcome']['status']=='verified_help']
        dates=[u['timestamp'] for u in actual if u.get('timestamp')]
        project_ids={u.get('project_id') for u in actual if u.get('project_id')}
        groups={project_by[x].get('project_group_id') for x in project_ids if x in project_by}-{None}
        m={'observed_episodes':len(actual),'read_episodes':sum('read' in u.get('levels_observed',[u['level']]) for u in actual),'executed_episodes':sum('executed' in u.get('levels_observed',[u['level']]) for u in actual),'declared_only_episodes':len(us)-len(actual),'successful_tool_episodes':sum(u.get('status')=='success' for u in actual),'tool_status_counts':dict(Counter(u.get('status','unknown') for u in actual)),'observed_workspaces':len(project_ids),'verified_repository_groups':len(groups),'distinct_threads':len({u['thread_id'] for u in actual}),'first_observed':min(dates) if dates else None,'last_observed':max(dates) if dates else None,'reviewed_outcome_episodes':len(reviewed),'verified_help_episodes':len(helped),'outcome_review_coverage':len(reviewed)/len(actual) if actual else None,'known_help_workspace_count':len({u.get('project_id') for u in helped if u.get('project_id')}),'applicable_opportunities':None,'missed_use_opportunities':None,'measured_time_saved':None,'historical_content_version':'unknown unless explicit snapshot evidence','window_cutoff':uc.get('history_cutoff_exclusive_utc'),'interpretation':'Observed read/execution attempts; neither tool success nor frequency is project effectiveness. No observation is not zero real-world usage.'}
        m['document_access_with_output_discarded_episodes']=sum(bool(u.get('discarded_output_operations')) and u.get('discarded_output_operations')==u.get('operation_count') for u in actual)
        m['verified_help_real_task_episodes']=sum(u['outcome'].get('scope')!='synthetic_validation' for u in helped)
        m['verified_help_synthetic_episodes']=sum(u['outcome'].get('scope')=='synthetic_validation' for u in helped)
        for days in (30,90):
            m[f'observed_last_{days}_days']=sum(bool(when(u.get('timestamp')) and cutoff-timedelta(days=days)<=when(u['timestamp'])<cutoff) for u in actual) if cutoff else None
        return m
    availability_order={'configured_enabled':0,'local_discoverable':1,'cached_unknown':2,'marketplace_cache':3,'vendored_dependency':4,'disabled':5,'backup':6}
    for logical, versions in sorted(grouped.items()):
        selected=min(versions,key=lambda x:availability_order.get(x['availability'],9))
        m=metrics(by_skill[logical])
        props={k:v for k,v in selected.items() if k not in {'id','logical_id','evidence','name','description','tags','duplicate_of','same_name_ids','other_version_ids'}}
        props.update({'logical_id':logical,'version_ids':[v['id'] for v in versions],'usage_metrics':m,'effectiveness':{'assessment':'scoped_help_cases_available' if m['verified_help_episodes'] else 'unknown','reviewed_cases':m['reviewed_outcome_episodes'],'verified_help_cases':m['verified_help_episodes'],'representative_success_rate':None,'sampling':'purposeful evidence-rich cases, not a random sample','interpretation':'See each usage case for scope; document routing does not establish practical success.'}})
        node(logical,'skill',selected['name'],selected['content_summary'],props,selected.get('evidence'),selected['tags'],aliases=[selected['name'],selected.get('description','')])
        for v in versions:
            vp={k:val for k,val in v.items() if k not in {'id','name','description','evidence','tags'}}
            vp['historical_usage_attribution']='Current document snapshot only; not the content at historical usage time.'
            node(v['id'],'skill_version',v['name']+' · '+v['sha256'][:8],v['content_summary'],vp,v.get('evidence'),v['tags'],status='deprecated' if v['availability']=='backup' else 'active')
            edge(logical,v['id'],'has_version','当前文档快照；不把历史调用绑定到当前正文',v.get('evidence'))
    # Historical skills can be absent from the current filesystem without being invented as available skills.
    for logical, us in sorted(by_skill.items()):
        if logical not in nodes:
            node(logical,'skill',us[0].get('name',logical),'历史记录中观察到的技能，当前安装目录未匹配。',{'availability':'unavailable','availability_detail':'historical_only_or_unresolved','logical_id':logical,'usage_metrics':metrics(us),'effectiveness':{'assessment':'unknown'},'warnings':['仅有历史使用定位，不能保证当前可调用。']},us[0].get('evidence'),['历史技能','待定位'])
    categories=defaultdict(list)
    for item in inventory:
        categories[(item.get('tags') or ['其他'])[0]].append(item['logical_id'])
    for category, skills in categories.items():
        i='scenario:'+short(category)
        descriptions=[]
        for s in sorted(set(skills)):
            descriptions.extend(nodes[s]['properties'].get('scenarios',[])[:1])
        node(i,'scenario',category,'按具体目标选择能力，再读取适用边界与历史证据。',{'goal':'从'+category+'任务寻找合适的方法','examples':descriptions[:12],'logical_entries':len(set(skills)),'selection_rules':['先核对本次需求和依赖','历史频率与效果分别看','仅缓存或禁用条目先确认可用性']},evidence('data/inventory.jsonl'),[category],aliases=descriptions[:12])
        for s in set(skills):
            edge(i,s,'offers_method','文档场景路由；不是实际效果证明',nodes[s]['evidence'])
    for item in inventory:
        if item.get('duplicate_of') in nodes:
            edge(item['id'],item['duplicate_of'],'same_document_content','正文hash相同；脚本、宿主和授权可能不同',item.get('evidence'))
    for line,p in enumerate(projects,1):
        node(p['id'],'project',p.get('title',p.get('cwd',p['id'])),'观察到的工作目录；不自动等同独立业务项目。',p,evidence('data/projects.jsonl',line),['工作目录','历史项目'])
        group=p.get('project_group_id')
        if group:
            if group not in nodes:
                common=p.get('project_group_basis',{}).get('common_git_dir','')
                title='仓库组 · '+Path(common.replace('\\','/')).parent.name
                node(group,'project',title,'由实际Git common-dir归一；同仓库可含多个业务任务。',{'identity':p.get('project_group_basis'),'workspace_ids':[]},evidence('data/projects.jsonl',line),['仓库组','项目归一'])
            nodes[group]['properties']['workspace_ids'].append(p['id'])
            edge(p['id'],group,'belongs_to_repository','当前Git元数据确认归一，不倒推所有历史任务语义',evidence('data/projects.jsonl',line))
    for line,u in enumerate(usage,1):
        p={k:v for k,v in u.items() if k not in {'id','summary','evidence','name'}}
        p.setdefault('current_user_instruction','未逐条人工提取；按证据定位原轮次')
        p.setdefault('observed_action',u['summary'])
        p.setdefault('direct_change','技能读取/执行观察不自动证明任何业务文件修改；需查调用对应补丁')
        p.setdefault('passive_effect','未建立；不能用时间接近替代因果')
        if isinstance(u.get('assessment'),dict):
            p.update(u['assessment'])
        refs=u.get('evidence',[])+evidence('data/usage.jsonl',line)
        if u.get('manual_review_evidence'):refs.append(u['manual_review_evidence'])
        node(u['id'],'usage',u.get('name','skill')+' · '+u.get('timestamp','未知时间')[:10],u['summary'],p,refs,[u.get('name',''),u.get('family',''),u['level']],aliases=[u.get('cwd','')])
        edge(u['logical_id'],u['id'],'observed_in','观察到使用，效果另见outcome',u.get('evidence'))
        if u.get('project_id') in nodes:
            edge(u['id'],u['project_id'],'occurred_in','会话记录的工作目录',u.get('evidence'))
        if u.get('outcome',{}).get('status')=='verified_help' and u.get('project_id') in nodes:
            edge(u['logical_id'],u['project_id'],'helped_in_reviewed_case','仅限已审查案例范围，非项目全部成功归因',u['outcome'].get('evidence'),{'usage_id':u['id'],'validation_level':u['outcome'].get('validation_level')})
    for n in curated['nodes']:
        if n['id'] in nodes:
            raise ValueError('curated ID conflict '+n['id'])
        nodes[n['id']]=n
        for source in n.get('properties',{}).get('source_logical_ids',[]):
            if source in nodes:
                edge(n['id'],source,'derived_from','从文档提炼方法；不是新增实测成功',n.get('evidence'))
    for e in curated.get('edges',[]):
        edges[e['id']]=e
    # Host-specific constraints belong in explicitly supplied curated nodes/edges.
    actual=[u for u in usage if set(u.get('levels_observed',[u['level']])) & {'read','executed'}]
    helped=[u for u in actual if u.get('outcome',{}).get('status')=='verified_help']
    real_helped=[u for u in helped if u['outcome'].get('scope')!='synthetic_validation']
    top=sorted((nodes[s] for s in by_skill),key=lambda n:(-n['properties']['usage_metrics']['observed_episodes'],n['id']))
    summary={'physical_skill_files':ic['physical_skill_files'],'logical_skills':ic['logical_skills'],'logical_identity_meaning':'Namespaced entrypoints, not deduplicated human capabilities','current_document_versions':len(inventory),'unique_document_hashes':ic['unique_document_hashes'],'historical_unresolved_entries':sum(n['kind']=='skill' for n in nodes.values())-len(grouped),'observed_episodes':len(actual),'declared_only_episodes':len(usage)-len(actual),'skills_with_observed_use':len({u['logical_id'] for u in actual}),'skills_with_verified_help':len({u['logical_id'] for u in helped}),'verified_help_cases':len(helped),'outcome_reviewed_cases':sum(u.get('outcome',{}).get('status') in {'verified_help','mixed','failure'} for u in actual),'observed_workspaces':len(projects),'verified_repository_groups':len({p.get('project_group_id') for p in projects}-{None}),'inventory_snapshot':ic.get('snapshot_at_utc'),'history_cutoff':uc.get('history_cutoff_exclusive_utc'),'source_files':uc.get('source_files'),'decoded_complete_bytes':uc.get('complete_bytes'),'top_observed_skills':[n['id'] for n in top[:20]],'availability_counts':ic['availability_counts'],'effectiveness_population_success_rate':None,'measured_skill_selection_lift':None,'review_sampling':'Purposeful evidence-rich cases; not representative of all use','coverage_notes':['Known host and discovered project skill roots; not a whole-disk guarantee','Only recoverable public history; copied exports and inherited calls deduplicated','Reading/execution attempts, tool success and project help are distinct','Current skill-document SHA is not historical full-bundle version','No arbitrary AI client compatibility or browser-interaction acceptance claimed']}
    node('observation:global-summary','observation','本机能力与历史使用 · 覆盖和口径','先看范围，再解释频率和效果；不以一个综合分数代替证据。',summary,evidence('reports/inventory-coverage.json')+evidence('reports/usage-coverage.json'),['总览','统计','覆盖','评估'])
    summary['skills_with_verified_help_including_synthetic']=summary['skills_with_verified_help']
    summary['skills_with_verified_help']=len({u['logical_id'] for u in real_helped})
    summary['verified_help_real_task_cases']=len(real_helped)
    summary['verified_help_synthetic_cases']=len(helped)-len(real_helped)
    summary['outcome_reviewed_cases']=sum(bool(u.get('manual_review_id')) or u.get('outcome',{}).get('status') in {'verified_help','mixed','failure'} for u in actual)
    summary['discarded_output_operations']=uc.get('discarded_output_operations')
    summary['episodes_with_only_discarded_output']=uc.get('episodes_with_only_discarded_output')
    node('procedure:knowledge-evaluation','procedure','评估一种方法是否值得复用','结合适用性、证据、历史效果、成本与维护性作选择；未知不打零分。',{'goal':'选择、复核与收录可复用能力','when_to_use':['需要比较技能或判断新经验是否应入库'],'inputs':['任务目标与边界','使用片段与结果证据'],'steps':['区分文件、逻辑技能、版本、使用片段与结果案例','先核对适用性和可用性，再看历史频次','分别审查实际作用、验证层、反例与维护成本','明确采样与覆盖缺口，按状态写回并保留来源'],'success_criteria':['可从结论追溯到具体案例','不把未知、相关性或工具成功冒充效果'],'stop_conditions':['缺少关键证据时保留待复核，不继续编造评价'],'limitations':['本图谱不自动建立反事实因果或普遍成功率'],'source_document':'docs/evaluation-method.md'},evidence('docs/evaluation-method.md'),['评价','效果','技能选择','复用'])
    for i,e in edges.items():
        if e['source'] not in nodes or e['target'] not in nodes:
            raise ValueError('dangling edge '+i)
    from kb import KnowledgeBase, validate_node, validate_edge
    for n in nodes.values():
        try: validate_node(n)
        except ValueError as exc: raise ValueError('Invalid assembled node '+n['id']+': '+str(exc)) from exc
    for e in edges.values(): validate_edge(e)
    out.joinpath('data').mkdir(parents=True,exist_ok=True);out.joinpath('reports').mkdir(parents=True,exist_ok=True)
    manifest={'schema':'ai-knowledge-baseline/v1','created_at':now,'files':{}}
    for filename,records in [('nodes.jsonl',nodes),('edges.jsonl',edges)]:
        payload=''.join(dump(records[i])+'\n' for i in sorted(records)).encode('utf-8')
        path=out/'data'/filename; temp=path.with_suffix('.pending')
        temp.write_bytes(payload);temp.replace(path)
        manifest['files'][filename]={'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}
    (out/'data/baseline_manifest.json').write_text(dump(manifest)+'\n',encoding='utf-8')
    (out/'reports/summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'reports/build.json').write_text(json.dumps({'schema':'ai-knowledge-build/v1','created_at':now,'nodes':len(nodes),'edges':len(edges),'inputs':file_hashes,'summary':summary},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # Validate with the same runtime that readers will use.
    health=KnowledgeBase(out).health()
    print(dump({'ok':health['ok'],'nodes':len(nodes),'edges':len(edges),'summary':summary}))

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
