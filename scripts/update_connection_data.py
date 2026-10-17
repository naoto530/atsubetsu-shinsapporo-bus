#!/usr/bin/env python3
"""Update Chuo and subway data from official HTML; no third party dependencies."""
import argparse
import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBWAY_URL = 'https://www.city.sapporo.jp/st/subway/route_time/h26/t19.html'
CHUO_URLS = {35: 'https://transfer.navitime.biz/chuo-bus/pc/diagram/BusDiagram?course=0005604129&orvCode=00301746&stopNo=10', 38: 'https://transfer.navitime.biz/chuo-bus/pc/diagram/BusDiagram?course=0005604128&orvCode=00301746&stopNo=19'}
DAYS = ('weekday','saturday','holiday')

def parse_chuo(source):
    out = {d: [] for d in DAYS}
    table = re.search(r'<table class="diagram-table[^>]*>.*?</table>', source, re.S)
    if not table: raise ValueError('Chuo table missing')
    for row in re.findall(r'<tr\b.*?</tr>',table[0],re.S):
        h = re.search(r'<th class="hour">\s*(\d+)',row)
        if not h: continue
        for day,cls in zip(DAYS,('wkd','std','snd')):
            cell = re.search(r'<td class="'+cls+r'">(.*?)</td>',row,re.S)
            if cell:
                out[day] += [f'{int(h[1]):02}:{int(m):02}' for m in re.findall(r'<span aria-hidden="true">(\d+)</span>',cell[1])]
    for times in out.values():
        if not times or len(times) != len(set(times)): raise ValueError('Invalid Chuo times')
    return out

def parse_subway(source):
    tables = re.findall(r'<table\b.*?</table>',source,re.S)
    if len(tables)!=2: raise ValueError('Expected two subway tables')
    result=[]
    for table in tables:
        times=[]
        for row in re.findall(r'<tr\b.*?</tr>',table,re.S):
            cells=re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>',row,re.S)
            if len(cells)!=2: continue
            h=re.search(r'\d+', re.sub('<[^>]*>','',cells[0]))
            if not h: continue
            hour=int(h[0]) or 24
            times += [f'{hour:02}:{int(m):02}' for m in re.findall(r'\b\d{2}\b',re.sub('<[^>]*>',' ',cells[1]))]
        if len(times)<100 or times[-1]!='24:00': raise ValueError('Invalid subway times')
        result.append(times)
    return dict(zip(DAYS,(result[0],result[1],result[1])))

def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 timetable-checker'})
    with urllib.request.urlopen(request,timeout=45) as r: return r.read().decode('utf-8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--write',action='store_true');args=parser.parse_args()
    today=datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    path=ROOT/'data/timetable.json';data=json.loads(path.read_text(encoding='utf-8'));changes=[]
    for number,url in CHUO_URLS.items():
        source=fetch(url+'&date='+today)
        if f'白{number}' not in source or '小野幌神社' not in source or '新さっぽろ駅' not in source: raise ValueError('Chuo route identity mismatch')
        times=parse_chuo(source)
        route=next((r for r in data['routes'] if r['id']==f'chuo-{number}-onopporo'),None)
        if route is None:
            route={'id':f'chuo-{number}-onopporo','operator':'北海道中央バス','routeName':f'白{number} '+('小野幌線' if number==35 else '厚別通線'),'boardingStop':'小野幌神社','destinationStop':'新さっぽろ駅','lane':'中央バス 2番のりば','travelTimeMinutes':7,'color':'#b52b48','operationInfo':{'url':url,'label':'中央バス公式時刻表','note':'小野幌神社 → 新さっぽろ駅'},'sourceNote':'中央バス公式停留所時刻表。所要時間は公式路線図の目安約7分。時間帯により変動。'}
            data['routes'].append(route)
        if route.get('timetables')!=times:
            route['timetables']=times;changes.append(f'Chuo {number} timetable updated')
        route['checkedAt']=today
    subway={'station':'新さっぽろ','direction':'宮の沢方面','walkMinutes':7,'sourceUrl':SUBWAY_URL,'timetables':parse_subway(fetch(SUBWAY_URL))}
    old=data.get('subway',{}).copy();old.pop('checkedAt',None)
    if old!=subway: changes.append('Subway timetable updated')
    subway['checkedAt']=today;data['subway']=subway
    if changes and args.write:
        data['metadata']['version']=f'connections-{today}';data['metadata']['updatedAt']=today
        path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('\n'.join(changes) if changes else 'No Chuo/subway timetable changes detected.')

if __name__=='__main__': main()
