import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
import pytest
from app import settings
from app.modules.transfer.service import current_week, form

HEADER = ['档案件', '修订号', '提交时间', '结案日', '查询日', '保管政策', '留置区段', '原始备注']
POLICY = '2020-01-01/2030-01-01/1'


def days_for(closed):
    year, month, day = map(int, closed.split('-'))
    return (date(year + 1, month, day) - date(year, month, day)).days


def source_csv(items):
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(HEADER)
    for key, closed in items:
        writer.writerow([key, 1, '2026-11-18T09:30:00+08:00', closed, '2026-06-01', POLICY, '无', ''])
    return out.getvalue().encode()


def book_csv(pairs):
    out = StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['档案件', '外部记录值(天)'])
    writer.writerows(pairs)
    return out.getvalue().encode()


def matched_book(items):
    return book_csv([(key, days_for(closed)) for key, closed in items])


def run_id_of(client, source, book):
    return client.post('/api/runs', json={'data': source.decode(), 'book': book.decode()}).json()['manifest']['run_id']


def transfer(client, run_id, **extra):
    return client.post('/api/transfer', json={'run_id': run_id, **extra})


@pytest.fixture
def small_row(monkeypatch):
    monkeypatch.setattr(settings, 'ROW_SLOTS', 4)


def test_weekly_list_packs_whole_groups(client, small_row):
    items = [('A01', '2024-01-01'), ('A02', '2024-01-01'), ('A03', '2024-01-01'),
             ('B01', '2025-01-01'), ('B02', '2025-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    response = transfer(client, run_id, week='2026-W47')
    assert response.status_code == 200
    result = response.json()
    assert (result['week'], result['status'], result['capacity'], result['used']) == ('2026-W47', '待复核', 4, 3)
    assert [row['slot'] for row in result['items']] == [1, 2, 3]
    assert [row['key'] for row in result['items']] == ['A01', 'A02', 'A03']
    for row in result['items']:
        assert (row['run_id'], row['group'], row['disposal']) == (run_id, '2024', '2025-01-01')
    assert [group['group'] for group in result['deferred']] == ['2025']
    assert result['deferred'][0]['keys'] == ['B01', 'B02'] and result['excluded'] == []


def test_smaller_group_fills_remaining(client, small_row):
    items = [('C01', '2023-01-01'), ('C02', '2023-01-01'),
             ('A01', '2024-01-01'), ('A02', '2024-01-01'), ('A03', '2024-01-01'),
             ('B01', '2025-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    result = transfer(client, run_id).json()
    assert [row['key'] for row in result['items']] == ['C01', 'C02', 'B01']
    assert [row['slot'] for row in result['items']] == [1, 2, 3]
    assert len(result['deferred']) == 1 and result['deferred'][0]['count'] == 3
    assert result['deferred'][0]['group'] == '2024'


def test_discrepancy_excluded(client, small_row):
    items = [('X01', '2024-01-01'), ('X02', '2024-01-01'), ('X03', '2024-01-01')]
    book = book_csv([('X01', 366), ('X02', 365)])
    run_id = run_id_of(client, source_csv(items), book)
    result = transfer(client, run_id).json()
    assert [row['key'] for row in result['items']] == ['X01']
    assert [(row['key'], row['code']) for row in result['excluded']] == [('X02', 'AMOUNT_DELTA'), ('X03', 'MISSING_BOOK')]
    assert all('账差未清' in row['detail'] for row in result['excluded'])


def test_idempotent_same_week(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    first = transfer(client, run_id).json()
    second = transfer(client, run_id).json()
    assert first['list_id'] == second['list_id'] and first['items'] == second['items']
    assert len(client.get('/api/transfer').json()) == 1


def test_concurrent_form_one_list(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: form(run_id, None, 1), range(4)))
    assert all(item['list_id'] == results[0]['list_id'] for item in results)
    assert len(client.get('/api/transfer').json()) == 1


def test_upstream_update_invalidates(client, small_row):
    first_items = [('A01', '2024-01-01')]
    run_a = run_id_of(client, source_csv(first_items), matched_book(first_items))
    stale = transfer(client, run_a).json()
    assert stale['status'] == '待复核'
    second_items = [('A01', '2024-01-01'), ('A02', '2024-01-01')]
    run_b = run_id_of(client, source_csv(second_items), matched_book(second_items))
    expired = client.get(f"/api/transfer/{stale['list_id']}").json()
    assert expired['status'] == '已失效'
    blocked = client.post(f"/api/transfer/{stale['list_id']}/review",
                          json={'version': 0, 'decision': '确认', 'note': ''})
    assert (blocked.status_code, blocked.json()['code']) == (409, 'LIST_EXPIRED')
    fresh = transfer(client, run_b).json()
    assert fresh['status'] == '待复核' and fresh['list_id'] != stale['list_id']
    assert len(fresh['items']) == 2
    history = client.get('/api/transfer').json()
    assert len(history) == 2 and {row['status'] for row in history} == {'已失效', '待复核'}


def test_stale_run_rejected(client, small_row):
    first_items = [('A01', '2024-01-01')]
    run_a = run_id_of(client, source_csv(first_items), matched_book(first_items))
    second_items = [('A02', '2024-01-01')]
    run_id_of(client, source_csv(second_items), matched_book(second_items))
    stale = transfer(client, run_a)
    assert (stale.status_code, stale.json()['code']) == (409, 'STALE_RUN')


def test_review_flow(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    list_id = transfer(client, run_id).json()['list_id']

    def review(version, decision='确认', note=''):
        return client.post(f'/api/transfer/{list_id}/review',
                           json={'version': version, 'decision': decision, 'note': note})

    assert review(0).json()['version'] == 1
    assert client.get(f'/api/transfer/{list_id}').json()['status'] == '确认'
    assert (review(0).status_code, review(0).json()['code']) == (409, 'STALE')
    unnoted = review(1, '退回')
    assert (unnoted.status_code, unnoted.json()['code']) == (422, 'REVIEW_NOTE')
    assert review(1, '退回', '库位复核未过').json()['version'] == 2
    detail = client.get(f'/api/transfer/{list_id}').json()
    assert detail['status'] == '退回'
    assert [row['version'] for row in detail['reviews']] == [2, 1]


def test_week_occupied(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    assert transfer(client, run_id).status_code == 200
    assert transfer(client, run_id).json()['list_id'] == transfer(client, run_id).json()['list_id']
    occupied = transfer(client, run_id, rows=2)
    assert (occupied.status_code, occupied.json()['code']) == (409, 'WEEK_OCCUPIED')


def test_list_unknown(client):
    missing = client.get('/api/transfer/not-found')
    assert (missing.status_code, missing.json()['code']) == (404, 'LIST_UNKNOWN')
    review = client.post('/api/transfer/not-found/review', json={'version': 0, 'decision': '确认', 'note': ''})
    assert (review.status_code, review.json()['code']) == (404, 'LIST_UNKNOWN')


def test_week_format(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    for bad in ('2026-W99', '2026-W00', 'W47', '2026-47'):
        response = transfer(client, run_id, week=bad)
        assert (response.status_code, response.json()['code']) == (422, 'WEEK_FORMAT')
    assert transfer(client, run_id, week='2026-W07').status_code == 200


def test_run_unknown(client):
    response = transfer(client, '0' * 64)
    assert (response.status_code, response.json()['code']) == (404, 'RUN_UNKNOWN')


def test_default_week_is_current(client, small_row):
    items = [('A01', '2024-01-01')]
    run_id = run_id_of(client, source_csv(items), matched_book(items))
    assert transfer(client, run_id).json()['week'] == current_week()


def test_sample_week_list(client):
    run_id = client.post('/api/runs', json={}).json()['manifest']['run_id']
    result = transfer(client, run_id).json()
    assert (result['capacity'], result['used']) == (16, 13)
    assert {row['group'] for row in result['items']} == {'2024'}
    assert result['deferred'][0]['group'] == '2025' and result['deferred'][0]['count'] == 11
    assert [(row['key'], row['code']) for row in result['excluded']] == [('00102', 'AMOUNT_DELTA'),
                                                                         ('00103', 'MISSING_BOOK')]
    assert result['items'][0]['disposal'] == '2025-01-18'
