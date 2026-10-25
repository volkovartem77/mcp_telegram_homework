"""Fixed-source, offline system diagnostics. No Telegram imports or network operations."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

HOME = Path('/home/ubuntu/.hermes')
PROJECT = Path('/home/ubuntu/mcp_telegram_homework')
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
STAMP = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})')
TOOLS = ('list_dialogs', 'get_message', 'search_chat', 'get_replies', 'read_around')


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds') if value else None


def read_db(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def error_code(value):
    if not value:
        return None
    return value if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', value) else 'redacted_error_text'


def log_lines(name):
    # Fixed files only; never read .env, auth files, Telegram session, or raw user inputs.
    p = HOME / 'logs' / name
    if not p.is_file():
        return
    stamp = None
    with p.open(errors='replace') as f:
        for line in f:
            m = STAMP.match(line)
            if m:
                stamp = datetime.strptime(m[1], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=timezone.utc).timestamp()
            yield stamp, line


def request(db, session=None, at=None, last=False):
    sql = """SELECT m.id,m.session_id,m.timestamp FROM messages m JOIN sessions s ON s.id=m.session_id
             WHERE m.role='user' AND s.source='telegram'"""
    args = []
    if session:
        sql += ' AND m.session_id=?'; args.append(session)
    if at is not None:
        sql += ' AND m.timestamp<=?'; args.append(at)
    for u in rows(db, sql + ' ORDER BY m.timestamp DESC,m.id DESC', args):
        nxt = db.execute("SELECT MIN(timestamp) FROM messages WHERE session_id=? AND role='user' AND id>?",
                         (u['session_id'], u['id'])).fetchone()[0]
        end = nxt if nxt is not None else time.time()
        final = db.execute("""SELECT timestamp,id FROM messages WHERE session_id=? AND id>? AND timestamp<?
                            AND role='assistant' AND (tool_calls IS NULL OR tool_calls IN ('','[]','null'))
                            AND content IS NOT NULL AND content!='' ORDER BY id DESC LIMIT 1""",
                           (u['session_id'], u['id'], end)).fetchone()
        if at is not None and at >= end:
            continue
        if final is None:
            if last:
                continue
            return {**u, 'end': None, 'next_user': nxt}
        return {**u, 'end': final['timestamp'], 'next_user': nxt}
    return None


def request_metrics(hdb, adb, turn):
    sid, start, end = turn['session_id'], turn['timestamp'], turn['end']
    if end is None:
        return {'available': False, 'reason': 'No completed final response in the transcript'}
    trace = rows(hdb, """SELECT id,role,timestamp,tool_calls,tool_call_id,tool_name,content FROM messages
                  WHERE session_id=? AND id>=? AND timestamp<=? ORDER BY id""", (sid, turn['id'], end))
    calls = {}
    assistants = []
    for r in trace:
        if r['role'] != 'assistant':
            continue
        assistants.append(r['timestamp'])
        try:
            entries = json.loads(r['tool_calls'] or '[]') or []
        except (ValueError, TypeError):
            entries = []
        for c in entries:
            f = c.get('function') or {}
            for key in (c.get('id'), c.get('call_id')):
                if key:
                    calls[key] = (f.get('name', ''), r['timestamp'])
    metrics, used, ambiguous = [], set(), 0
    for t, line in log_lines('agent.log'):
        if t is None or not start <= t <= end or f'[{sid}]' not in line:
            continue
        m = re.search(r'API call #(\d+): .*? in=(\d+|\?) out=(\d+|\?) total=(\d+|\?) latency=([\d.]+)s', line)
        if not m:
            continue
        # Background review shares session ID but does not persist transcript rows.
        # Require a matching persisted foreground assistant response, not time alone.
        eligible = [i for i, a in enumerate(assistants) if i not in used and 0 <= a-t <= 2]
        if not eligible:
            ambiguous += 1
            continue
        used.add(min(eligible, key=lambda i: assistants[i]-t))
        metrics.append({'timestamp': utc(t), 'number': int(m[1]),
                        'input_tokens': int(m[2]) if m[2] != '?' else None,
                        'output_tokens': int(m[3]) if m[3] != '?' else None, 'latency_seconds': float(m[5])})
    tool_details, ids = [], set()
    for r in trace:
        if r['role'] != 'tool':
            continue
        name = r['tool_name'] or ''
        if not name.startswith('mcp__telegram_research__') or name.split('__')[-1] not in TOOLS:
            continue
        operation = name.split('__')[-1]
        began = calls.get(r['tool_call_id'], ('', None))[1]
        candidates = []
        # operation_id can be inside a JSON, text or MCP content envelope; never emit the envelope.
        for oid in set(re.findall(UUID, r['content'] or '')):
            candidates += rows(adb, 'SELECT operation_id FROM tool_audit WHERE operation_id=? AND operation=?', (oid, operation))
        method = 'operation_id'
        if not candidates and began is not None:
            candidates = rows(adb, 'SELECT operation_id FROM tool_audit WHERE operation=? AND started_at BETWEEN ? AND ?',
                              (operation, began - 0.5, r['timestamp'] + 0.5))
            method = 'unique operation/time interval'
        oid = candidates[0]['operation_id'] if len(candidates) == 1 else None
        if oid:
            ids.add(oid)
        tool_details.append({'operation': operation, 'call_utc': utc(began), 'result_utc': utc(r['timestamp']),
                             'operation_id': oid, 'correlation': method if oid else 'unresolved'})
    tools, rpcs = audit_records(adb, ids)
    all_usage = bool(metrics) and all(x['input_tokens'] is not None and x['output_tokens'] is not None for x in metrics)
    complete = len(used) == len(assistants) and ambiguous == 0
    return {'start_utc': utc(start), 'final_saved_utc': utc(end), 'session_id': sid, 'user_message_id': turn['id'],
            'elapsed_seconds': round(end-start, 3), 'completion_basis': 'last saved final assistant response before next user',
            'llm_call_count': len(metrics) if complete else None, 'matched_llm_log_records': len(metrics),
            'unattributed_api_log_records': ambiguous, 'metric_coverage': 'complete' if complete else 'incomplete; do not estimate',
            'input_tokens': sum(x['input_tokens'] for x in metrics) if all_usage and complete else None,
            'output_tokens': sum(x['output_tokens'] for x in metrics) if all_usage and complete else None,
            'llm_api_latency_seconds': round(sum(x['latency_seconds'] for x in metrics), 3) if complete else None,
            'telegram_mcp_calls': len(tool_details), 'mcp_trace': tool_details, 'tool_audit': tools, 'rpc_audit': rpcs,
            'research_rpc_count': len(rpcs), 'audit_correlation_complete': len(ids) == len(tool_details),
            'limiter_wait_seconds': round(sum(x['wait_seconds'] for x in rpcs), 3),
            'rpc_execution_seconds': round(sum(x['duration_seconds'] or 0 for x in rpcs), 3),
            'rate_limited_results': sum(x['status']=='rate_limited' for x in tools),
            'notes': ['Token counts are logged usage, never estimates; null means unavailable/incomplete.',
                      'Research RPC totals require tool operation IDs; unrelated pings/updates are excluded.',
                      'Elapsed time includes queueing, LLM/API/network and overhead; no inference-only claim.',
                      'Audit totals are partial if any MCP call lacks correlation; no session-wide token substitution.']}


def audit_records(db, identifiers):
    tools, rpcs = [], []
    for oid in sorted(identifiers):
        for r in rows(db, 'SELECT operation_id,operation,started_at,completed_at,status,error,rpc_count,wait_seconds,rejected_wait_seconds FROM tool_audit WHERE operation_id=?', (oid,)):
            r['start_utc'], r['end_utc'] = utc(r.pop('started_at')), utc(r.pop('completed_at'))
            r['error'] = error_code(r['error']); tools.append(r)
        for r in rows(db, 'SELECT id,operation_id,operation,rpc,queued_at,dispatched_at,completed_at,wait_seconds,outcome,error FROM rpc_audit WHERE operation_id=? ORDER BY id', (oid,)):
            r['duration_seconds'] = round(r['completed_at']-r['dispatched_at'], 6) if r['completed_at'] else None
            for k in ('queued_at', 'dispatched_at', 'completed_at'):
                r[k.replace('_at', '_utc')] = utc(r[k]); del r[k]
            r['error'] = error_code(r['error']); rpcs.append(r)
    return sorted(tools, key=lambda r:r['start_utc']), sorted(rpcs, key=lambda r:r['id'])


def health(db):
    now = time.time()
    state = dict(db.execute('SELECT halted,reason,cooldown,flood,last_time FROM state WHERE id=1').fetchone())
    state['reason'] = error_code(state['reason'])
    state['cooldown_remaining_seconds'] = round(max(0, state['cooldown']-now), 3)
    state['flood_remaining_seconds'] = round(max(0, state['flood']-now), 3)
    state['halt_marker_exists'] = (PROJECT/'data/operations.sqlite3.halted').exists()
    import yaml
    config = yaml.safe_load((PROJECT/'rate_limits.yaml').read_text())
    counts = {w: dict(db.execute('SELECT operation,count(*) FROM rpc_audit WHERE dispatched_at>? GROUP BY operation', (now-seconds,)).fetchall())
              for w,seconds in (('minute',60),('hour',3600),('day',86400))}
    global_counts={w:sum(n for op,n in group.items() if op!='keepalive') for w,group in counts.items()}
    hermes=yaml.safe_load((HOME/'config.yaml').read_text())
    server=(hermes.get('mcp_servers') or {}).get('telegram_research') or {}
    safe_mcp={k:server[k] for k in ('timeout','connect_timeout') if isinstance(server.get(k),(int,float))}
    latest_ping = rows(db, "SELECT id,operation,dispatched_at,completed_at,outcome,error FROM rpc_audit WHERE rpc='PingRequest' ORDER BY id DESC LIMIT 1")
    for r in latest_ping:
        r['dispatched_utc'] = utc(r.pop('dispatched_at')); r['completed_utc'] = utc(r.pop('completed_at')); r['error']=error_code(r['error'])
    boot = int(next(x.split()[1] for x in Path('/proc/stat').read_text().splitlines() if x.startswith('btime ')))
    import os
    procs = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p/'cmdline').read_bytes().replace(b'\0',b' ')
            kind = 'mcp' if b'-m telegram_mcp.server' in cmd else ('gateway' if b'gateway run' in cmd and b'hermes' in cmd else None)
            if not kind:
                continue
            stat = (p/'stat').read_text().rsplit(')',1)[1].split()
            start = boot + int(stat[19])/os.sysconf('SC_CLK_TCK')
            sockets = set()
            for fd in (p/'fd').iterdir():
                try:
                    link=fd.readlink().as_posix()
                    if link.startswith('socket:['): sockets.add(link[8:-1])
                except OSError: pass
            tcp = Counter()
            for proto in ('tcp','tcp6'):
                for line in (p/'net'/proto).read_text().splitlines()[1:]:
                    fields=line.split()
                    if fields[9] in sockets: tcp[fields[3]] += 1
            procs.append({'pid':int(p.name),'kind':kind,'started_utc':utc(start),'tcp_states_hex':dict(tcp),
                          'disk_rate_config_newer_than_process':(PROJECT/'rate_limits.yaml').stat().st_mtime > start})
        except (OSError, ValueError, IndexError):
            continue
    lock = PROJECT/'data/telegram.session.lock'
    locks=[]
    if lock.exists():
        st=lock.stat(); key=f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
        locks=[int(line.split()[4]) for line in Path('/proc/locks').read_text().splitlines() if key in line and line.split()[4].lstrip('-').isdigit()]
    head=(PROJECT/'.git/HEAD').read_text().strip()
    if head.startswith('ref: '): head=(PROJECT/'.git'/head[5:]).read_text().strip()
    failures=rows(db,"SELECT id,operation_id,operation,rpc,dispatched_at,completed_at,outcome,error FROM rpc_audit WHERE outcome NOT IN ('ok','in_flight') ORDER BY id DESC LIMIT 5")
    for r in failures:
        r['dispatched_utc']=utc(r.pop('dispatched_at'));r['completed_utc']=utc(r.pop('completed_at'));r['error']=error_code(r['error'])
    return {'observed_utc':utc(now),'state':state,'counts_by_window':counts,'global_counts':global_counts,'recent_failed_rpcs':failures,
            'hermes_mcp_timeout_config':safe_mcp,
            'disk_limits':config['limits'],'disk_auto_waiting_max':config['auto_waiting_max'],'deployed_commit':head,
            'latest_ping':latest_ping,'processes':procs,'session_lock_owner_pids':locks,
            'notes':['TCP 01=ESTABLISHED; a single socket snapshot cannot prove Telethon health.',
                     'Disk config is not proof of in-memory config; compare PID start and deployment times.',
                     'Historical internal PingRequest rows remain charged under their original labels.']}


def log_events(start, end):
    result=[]
    patterns=('halted','disconnect','connectionerror','floodwait','rate_limited','pingrequest','timeout','connected','background_review','self-improvement')
    for name in ('gateway.log','mcp-stderr.log','agent.log','errors.log','gateway-stdio.log'):
        for t,line in log_lines(name):
            if t is None or not start <= t <= end:
                continue
            tags=[x for x in patterns if x in line.lower()]
            classes=re.findall(r'\b[A-Z][A-Za-z]*(?:Error|Exception|Wait)\b',line)
            frame=re.search(r'File "([^"]+)", line (\d+), in ([a-zA-Z0-9_<>]+)',line)
            if not tags and not classes and not frame:
                continue
            # Emit classifications, IDs and exception classes, never free-form log bodies.
            result.append({'utc':utc(t),'source':name,'tags':tags,'operation_ids':re.findall(UUID,line),
                           'exception_classes':classes,
                           'frame':{'file':Path(frame[1]).name,'line':int(frame[2]),'function':frame[3]} if frame else None})
    result.sort(key=lambda r:r['utc'])
    return {'events':result[-100:], 'truncated':len(result)>100,
            'limitation':'Classified metadata only; raw message/error text and traceback source lines are withheld.'}


def rpc_usage(db, operation=None, hours=24):
    """Count stored dispatches in one rolling snapshot; never infer hidden methods."""
    end = time.time()
    start = end - hours * 3600
    sql = '''SELECT operation,rpc,COUNT(*) AS count,
             MIN(dispatched_at) AS first_at,MAX(dispatched_at) AS last_at
             FROM rpc_audit WHERE dispatched_at>? AND dispatched_at<=?'''
    args = [start, end]
    if operation is not None:
        sql += ' AND operation=?'
        args.append(operation)
    groups = rows(db, sql + ' GROUP BY operation,rpc ORDER BY count DESC,operation,rpc', args)
    totals = Counter()
    for group in groups:
        totals[group['operation']] += group['count']
        group['first_utc'] = utc(group.pop('first_at'))
        group['last_utc'] = utc(group.pop('last_at'))
    return {'mode': 'rpc-usage', 'operation': operation, 'hours': hours,
            'start_exclusive_utc': utc(start), 'end_inclusive_utc': utc(end),
            'start_epoch': start, 'end_epoch': end, 'groups': groups,
            'totals': {'rpc_count': sum(totals.values()), 'by_operation': dict(totals),
                       'internal_ping_requests': sum(g['count'] for g in groups
                           if g['operation'] == 'internal' and g['rpc'] == 'PingRequest')},
            'notes': ['Rolling hours, not calendar-day usage; every recorded dispatch attempt counts, including failures.',
                      'Historical PingRequest rows charged to internal are included, not reclassified as keepalive.',
                      'Historical wrapper-only rows remain wrapper-only; their inner methods were not recorded.',
                      'Operation totals are not an independent global quota total.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('latest-incident','last-request','rpc-usage'))
    parser.add_argument('--operation',help='Stored operation label, e.g. internal; omit for all operations')
    parser.add_argument('--hours',type=int,help='Positive rolling-hour window for rpc-usage (default: 24)')
    parser.add_argument('--report',action='store_true',help='Create only a sanitized report artifact in the auditor cache')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--operation-id',type=lambda x:str(__import__('uuid').UUID(x)))
    group.add_argument('--session',type=lambda x:x if re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',x) else parser.error('Invalid session ID'))
    group.add_argument('--at',help='ISO-8601 timestamp with explicit timezone')
    args=parser.parse_args()
    if args.mode == 'rpc-usage':
        if args.report or args.operation_id or args.session or args.at:
            parser.error('rpc-usage accepts only --operation and --hours; no report-file writes')
        if args.operation is not None and not re.fullmatch(r'[a-z_]{1,64}',args.operation):
            parser.error('Invalid operation label')
        hours = 24 if args.hours is None else args.hours
        if hours <= 0:
            parser.error('--hours must be positive')
        db = read_db(PROJECT/'data/operations.sqlite3')
        try:
            print(json.dumps(rpc_usage(db,args.operation,hours),ensure_ascii=False,separators=(',',':')))
        finally:
            db.close()
        return
    if args.operation is not None or args.hours is not None:
        parser.error('--operation and --hours require rpc-usage')
    at=None
    if args.at:
        dt=datetime.fromisoformat(args.at.replace('Z','+00:00'))
        if dt.tzinfo is None: parser.error('--at requires timezone')
        at=dt.timestamp()
    a,h=read_db(PROJECT/'data/operations.sqlite3'),read_db(HOME/'state.db')
    try:
        incident=None
        if args.operation_id:
            incident=a.execute('SELECT operation_id,started_at FROM tool_audit WHERE operation_id=?',(args.operation_id,)).fetchone()
            if incident is None: raise ValueError('Operation ID not found')
        elif args.mode=='latest-incident' and not args.session and at is None:
            incident=a.execute("SELECT operation_id,started_at FROM tool_audit WHERE status IN ('halted','error','unavailable') ORDER BY started_at DESC LIMIT 1").fetchone()
        selected_session=args.session
        if incident:
            detail=a.execute('SELECT operation,started_at,completed_at FROM tool_audit WHERE operation_id=?',(incident['operation_id'],)).fetchone()
            matches=rows(h,"SELECT DISTINCT session_id FROM messages WHERE role='tool' AND tool_name=? AND timestamp BETWEEN ? AND ?",
                         ('mcp__telegram_research__'+detail['operation'],detail['started_at']-0.5,(detail['completed_at'] or detail['started_at'])+1))
            if len(matches)==1: selected_session=matches[0]['session_id']
        turn=request(h,selected_session,incident['started_at'] if incident else at,last=args.mode=='last-request' and at is None and not incident)
        result={'mode':args.mode,'current_health':health(a)}
        if incident:
            t,r=audit_records(a,{incident['operation_id']}); result['selected_incident']={'tools':t,'rpcs':r}
        if turn:
            result['request']=request_metrics(h,a,turn)
            if incident:
                result['incident_request_correlation_confirmed']=any(r['operation_id']==incident['operation_id'] for r in result['request'].get('tool_audit',[]))
            start=turn['timestamp'];end=turn['end'] or min(time.time(),start+900)
        elif incident:
            start=incident['started_at'];end=start+30
            result['request']={'available':False,'reason':'No matching Telegram transcript'}
        else:
            raise ValueError('No matching completed request or incident')
        result['nearby_logs']=log_events(start-120,end+30)
        result['correlation_caution']='Nearby log events are context, not automatically attributed to this request. Current health is a separate observation.'
        if args.report:
            # The only optional write: a sanitized user-facing report, never source/config/state.
            import os
            path=HOME/'cache/system-auditor'/('audit-'+str(time.time_ns())+'.txt')
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f: json.dump(result,f,ensure_ascii=False,indent=2)
            result['report_path']=str(path)
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    finally:
        a.close();h.close()


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # No traceback or arbitrary exception text, which may contain private data.
        print(json.dumps({'error':type(exc).__name__,'status':'audit_unavailable','action':'Report unavailable evidence; do not repair or retry infrastructure.'}))
        raise SystemExit(1)
