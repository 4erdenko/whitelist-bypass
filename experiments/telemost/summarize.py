from collections import Counter, defaultdict
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

ROOT=None


def rows(name):
    path=ROOT/name
    result=[]
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


def metrics(probes):
    health=[row for row in probes if row['kind']=='health']
    payload=[row for row in probes if row['kind']!='health']
    times=sorted(row['seconds'] for row in health if row['ok'])
    speeds={kind:[row['mbps'] for row in payload if row['kind']==kind and row['ok']]
            for kind in ['download','upload']}
    return {
        'health_passed':sum(row['ok'] for row in health),'health_total':len(health),
        'payload_passed':sum(row['ok'] for row in payload),'payload_total':len(payload),
        'http_median_seconds':statistics.median(times) if times else None,
        'http_p95_seconds':times[math.ceil(.95*len(times))-1] if times else None,
        'download_median_mbps':statistics.median(speeds['download']) if speeds['download'] else None,
        'upload_median_mbps':statistics.median(speeds['upload']) if speeds['upload'] else None,
        'payload_over_30_seconds':sum(row.get('over_30_seconds',False) for row in payload)}


def main():
    results=json.loads((ROOT/'results.json').read_text()) if (ROOT/'results.json').exists() else []
    probes=rows('probes.jsonl')
    api=rows('api-events.jsonl')
    events=rows('events.jsonl')
    buckets=defaultdict(Counter)
    for row in api:
        if row['kind']=='response':
            minute=row['utc'][:16]
            buckets[minute][row['egress']]+=1
    summary={'results':results,'observed_api_requests_per_minute_peak':{key:max((count[key] for count in buckets.values()),default=0) for key in ['mac','vps']},
             'api_status_counts':dict(Counter(str(row['status']) for row in api if row['kind']=='response')),
             'gate_queue_timeouts':sum(row['kind']=='queue_timeout' for row in api),
             'gate_expired_leases':sum(row['kind']=='lease_expired' for row in api),
             'gate_cancelled_waiters':sum(row['kind']=='queue_cancelled' for row in api),
             'ssh_failed_attempts':sum(row['kind']=='ssh_attempt_failed' for row in events),
             'ssh_recoveries':sum(row['kind']=='ssh_recovered' for row in events),
             'ssh_forward_restarts':sum(row['kind']=='ssh_forward_restart' for row in events),
             'measurements':{}}
    for result in results:
        name=result['config']['name']
        phase=[row for row in probes if row['phase']==name and not row.get('readiness')]
        times=sorted(row['seconds'] for row in phase if row['kind']=='health' and row['ok'])
        summary['measurements'][name]={'http_median_seconds':statistics.median(times) if times else None,
              'http_p95_seconds':times[math.ceil(.95*len(times))-1] if times else None,
              'download_median_mbps':statistics.median([row['mbps'] for row in phase if row['kind']=='download' and row['ok']]) if any(row['kind']=='download' and row['ok'] for row in phase) else None,
              'upload_median_mbps':statistics.median([row['mbps'] for row in phase if row['kind']=='upload' and row['ok']]) if any(row['kind']=='upload' and row['ok'] for row in phase) else None}
        summary['measurements'][name]['payload_over_30_seconds']=sum(row.get('over_30_seconds',False) for row in phase if row['kind']!='health')
        requests=[row for row in api if row.get('room','').rsplit('-',1)[0]==name]
        waits=sorted(row['queue_seconds'] for row in requests if row['kind']=='granted')
        summary['measurements'][name]['api_statuses']=dict(Counter(str(row['status']) for row in requests if row['kind']=='response'))
        network_errors=[row for row in requests if row['kind']=='response' and row['status']==0]
        summary['measurements'][name]['api_network_error_seconds']=[row['elapsed_seconds'] for row in network_errors]
        summary['measurements'][name]['api_protocols']=dict(Counter(row.get('protocol','unknown') for row in requests if row['kind']=='response' and row['status']!=0))
        summary['measurements'][name]['queue_p95_seconds']=waits[math.ceil(.95*len(waits))-1] if waits else None
        summary['measurements'][name]['rate_limit_operations']=[{key:row[key] for key in ['utc','account','egress','role','endpoint','retry_after_present','retry_after_seconds','cooldown_seconds']} for row in requests if row['kind']=='response' and row['status']==429]
        hold=next((row for row in events if row['kind']=='hold_start' and row['phase']==name),None)
        if hold:
            started=datetime.fromisoformat(hold['utc']).timestamp()
            duration=min(result['hold_seconds'],result['config']['hold'])
            steady=[row for row in requests if row['kind']=='response' and started<=datetime.fromisoformat(row['utc']).timestamp()<started+duration]
            rate=Counter(row['egress']+':'+row['role'] for row in steady)
            summary['measurements'][name]['steady_api_rpm']={key:value*60/duration for key,value in rate.items()} if duration else {}
            summary['measurements'][name]['successful_connection_refreshes_in_hold']=sum(row['endpoint']=='connection' and row['status']==200 for row in steady)
            comparison=[row for row in phase if started<=datetime.fromisoformat(row['utc']).timestamp()<started+600]
            summary['measurements'][name]['first_ten_minutes']={**metrics(comparison),
                'api_429':sum(row['status']==429 for row in steady if datetime.fromisoformat(row['utc']).timestamp()<started+600)}
            groups={}
            for group in ['mac','vps']:
                group_rows=[row for row in phase if ('vps' if row['room']<=result['config']['vps_rooms'] else 'mac')==group]
                if group_rows:
                    groups[group]=metrics(group_rows)
            summary['measurements'][name]['creator_egress_groups']=groups
            transfers=defaultdict(list)
            for row in phase:
                if row['kind']!='health':
                    begin=datetime.fromisoformat(row['utc']).timestamp()
                    transfers[row['room']].append((begin,begin+row.get('seconds',0)))
            health_groups={'overlapping_file_transfer':[],'without_file_transfer':[]}
            for row in phase:
                if row['kind']=='health':
                    begin=datetime.fromisoformat(row['utc']).timestamp()
                    end=begin+row.get('seconds',0)
                    busy=any(left<=end and right>=begin for left,right in transfers[row['room']])
                    health_groups['overlapping_file_transfer' if busy else 'without_file_transfer'].append(row)
            summary['measurements'][name]['health_by_load']={key:metrics(value) for key,value in health_groups.items()}
            polls=defaultdict(list)
            for row in steady:
                if row['endpoint']=='peers':
                    polls[row['room']].append(datetime.fromisoformat(row['utc']).timestamp())
            intervals=[b-a for values in polls.values() for a,b in zip(values,values[1:])]
            summary['measurements'][name]['observed_waiting_poll_median_seconds']=statistics.median(intervals) if intervals else None
    (ROOT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    lines=['# Telemost room stability experiment', '',
           '| Phase | Rooms | Hold minutes | Health | Files | 429 | Clean |',
           '|---|---:|---:|---:|---:|---:|---|']
    for result in results:
        config=result['config']
        lines.append(f"| {config['name']} | {result['ready_rooms']}/{config['rooms']} | {min(result['hold_seconds'],config['hold'])/60:.1f} | {result['health_passed']}/{result['health_total']} | {result['payload_passed']}/{result['payload_total']} | {result['api_429']} | {result['clean']} |")
    lines.extend(['', 'Speeds, latency, matched ten-minute windows and load overlap are in summary.json.',
                  'Final file transfers follow the requested hold. A completed hold is not an error-free pass.',
                  'This experiment does not establish a provider quota or mobile allowlist availability.'])
    (ROOT/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'completed_phases':len(results),'all_completed_phases_clean':bool(results) and all(result['clean'] for result in results)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',required=True,type=Path)
    ROOT=parser.parse_args().state.resolve()
    main()
