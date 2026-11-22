import json
import re
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from app import settings
from app.errors import DataError
from app.pipeline import stages
from app.store import connection
from app.modules.runs.repository import lookup
from .repository import expire_live, insert_list, latest_run_id, lookup_list, payload, reviews

WEEK = re.compile(r'^\d{4}-W(\d{2})$')


def current_week():
    year, week, _ = datetime.now(timezone.utc).date().isocalendar()
    return f'{year}-W{week:02d}'


def check_week(week):
    matched = WEEK.fullmatch(week or '')
    if not matched or not 1 <= int(matched.group(1)) <= 53:
        raise DataError('WEEK_FORMAT', '周须为 YYYY-Www 形式')
    return week


def build(result, source, run_id, capacity):
    derived = stages(source)
    closed = {row['key']: row['fields']['结案日'] for row in derived['quality']['ready']}
    codes = {row['key']: row['code'] for row in result['book']['rows']}
    groups, excluded = {}, []
    for item in result['accepted']:
        key = item['key']
        code = codes.get(key, 'MISSING_BOOK')
        if code != 'MATCH':
            excluded.append({'key': key, 'code': code, 'detail': '账差未清，不得进入本周清单'})
            continue
        disposal = next(part['value'] for part in item['proof'] if part['metric'] == '最早处置日')
        year = closed[key][:4]
        groups.setdefault(year, []).append({'key': key, 'closed': closed[key], 'disposal': disposal,
                                            'group': year, 'run_id': run_id})
    ordered = sorted(groups.items(), key=lambda pair: (min(row['disposal'] for row in pair[1]), pair[0]))
    items, deferred, remaining = [], [], capacity
    for year, members in ordered:
        members.sort(key=lambda row: (row['disposal'], row['key']))
        if len(members) <= remaining:
            items.extend(members)
            remaining -= len(members)
        else:
            deferred.append({'group': year, 'count': len(members), 'keys': [row['key'] for row in members],
                             'reason': f'同年结案整组 {len(members)} 件，剩余库位 {remaining} 个放不下'})
    for slot, item in enumerate(items, 1):
        item['slot'] = slot
    return {'items': items, 'deferred': deferred,
            'excluded': sorted(excluded, key=lambda row: row['key']), 'used': len(items)}


def form(run_id, week, rows):
    week = check_week(week) if week else current_week()
    capacity = rows * settings.ROW_SLOTS
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if run_id is None:
            run_id = latest_run_id(db)
            if run_id is None:
                raise DataError('RUN_UNKNOWN', '请先运行处置批次', 404)
        run = lookup(db, run_id)
        result = json.loads(run['result'])
        list_id = sha256(f'{settings.RULE_VERSION}:{run_id}:{week}:{capacity}'.encode()).hexdigest()
        existing = db.execute('SELECT * FROM transfer_lists WHERE id=?', (list_id,)).fetchone()
        if existing is not None:
            if existing['status'] == '已失效':
                raise DataError('STALE_RUN', '上游处置结果已更新，旧清单已失效', 409)
            return {**payload(existing), 'reviews': reviews(db, list_id)}
        if latest_run_id(db) != run_id:
            raise DataError('STALE_RUN', '上游处置结果已更新，请基于最新批次形成清单', 409)
        record = {'list_id': list_id, 'week': week, 'run_id': run_id, 'capacity': capacity,
                  'created': datetime.now(timezone.utc).isoformat(),
                  **build(result, run['source'], run_id, capacity)}
        try:
            insert_list(db, record)
        except sqlite3.IntegrityError:
            concurrent = db.execute('SELECT * FROM transfer_lists WHERE id=?', (list_id,)).fetchone()
            if concurrent is not None and concurrent['status'] != '已失效':
                return {**payload(concurrent), 'reviews': reviews(db, list_id)}
            raise DataError('WEEK_OCCUPIED', '本周已有生效清单，不能重复占位', 409)
        return {**record, 'status': '待复核', 'reviews': []}


def detail(list_id):
    with connection() as db:
        row = lookup_list(db, list_id)
        return {**payload(row), 'reviews': reviews(db, list_id)}


def decide(list_id, version, decision, note):
    note = note.strip()
    if decision not in {'确认', '退回'}:
        raise DataError('REVIEW_DECISION', '复核结论无效')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = lookup_list(db, list_id)
        if row['status'] == '已失效':
            raise DataError('LIST_EXPIRED', '清单已失效，请按最新批次重新形成', 409)
        current = db.execute('SELECT COALESCE(MAX(version),0) FROM transfer_reviews WHERE list_id=?',
                             (list_id,)).fetchone()[0]
        if current != version:
            raise DataError('STALE', '复核版本已过期，请刷新', 409)
        if decision == '退回' and not note:
            raise DataError('REVIEW_NOTE', '退回清单须填写复核意见')
        changed = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO transfer_reviews VALUES (?,?,?,?,?)',
                   (list_id, current + 1, decision, note, changed))
        db.execute('UPDATE transfer_lists SET status=?,changed=? WHERE id=?', (decision, changed, list_id))
        return {'version': current + 1, 'decision': decision, 'note': note, 'changed': changed}
