"""Caps apply before each HTTP attempt and survive restart without invented prices."""
from dataclasses import replace
from decimal import Decimal
import threading

import httpx
import pytest

from ai_comment_generator import CommentGenerator, SYSTEM_INSTRUCTION
from api_client import IntegrationError
from budget import BudgetGuard, BudgetError
from config import Config
from database import Database
from models import Video


@pytest.fixture
def db(tmp_path):
    result = Database(tmp_path / 'history.sqlite3')
    yield result
    result.close()


def priced(**values):
    return Config(input_price_per_million=1, output_price_per_million=2, **values)


def task(db):
    db.add_video(Video('123', 'https://www.tiktok.com/@fixture/video/123', description='#cs2 skins'))
    return db.ensure_task('account', '123')


def response(text='реклама: что обсудим в RubUP?', usage=None):
    return {'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':text}]}],
            'usage': usage if usage is not None else {'input_tokens':100,'output_tokens':10}}


def test_finite_limit_unknown_tariff_blocks_before_request(db):
    guard = BudgetGuard(db, 1, Config(), limit_usd=1)
    with pytest.raises(BudgetError) as exc:
        guard.before_request('контекст', 'инструкция', 128)
    assert exc.value.reason == 'unknown_price'
    assert guard.summary()['requests'] == 0


def test_conservative_reservation_reconciles_actual_and_persists_limit(db):
    config=priced()
    guard=BudgetGuard(db, 1, config, limit_usd=1)
    token=guard.before_request('контекст', 'инструкция', 128)
    reserved=guard.summary()['reserved_usd']
    assert reserved >= (len('инструкция\nконтекст'.encode()) + 1024 + 2*128)/1_000_000
    guard.settle(token, .2)
    assert guard.summary()['spent_usd'] == .2
    assert guard.summary()['reserved_usd'] == 0
    reopened=BudgetGuard(db, 1, config, limit_usd=100)
    assert reopened.summary()['limit_usd'] == 1
    assert reopened.summary()['remaining_usd'] == .8
    with pytest.raises(BudgetError,match='лимит'):
        reopened.before_request('x'*1_000_000, '', 100)


def test_unsettled_reservation_after_resume_blocks_even_without_cap(db):
    first=BudgetGuard(db, 7, priced())
    first.before_request('контекст', 'инструкция', 100)
    resumed=BudgetGuard(db, 7, priced())
    with pytest.raises(BudgetError) as exc:
        resumed.before_request('новый контекст', 'инструкция',100)
    assert exc.value.reason == 'unknown_cost'
    assert resumed.summary()['requests'] == 1
    assert resumed.summary()['reserved_usd'] > 0


def test_missing_usage_holds_reserve_and_blocks_next_request(db):
    guard=BudgetGuard(db, 1, priced(),limit_usd=1)
    token=guard.before_request('x','instructions',100)
    amount=guard.summary()['reserved_usd']
    guard.settle(token,None,usage_known=False)
    assert guard.summary()['unknown_requests'] == 1
    assert guard.summary()['reserved_usd'] == amount
    with pytest.raises(BudgetError) as exc:
        guard.before_request('x','instructions',100)
    assert exc.value.reason == 'unknown_cost'


def test_unlimited_valid_usage_with_unknown_tariff_is_unpriced_not_uncertain(db):
    guard=BudgetGuard(db, 1, Config())
    for number in range(3):
        token=guard.before_request('x','instructions',100)
        guard.settle(token,None,usage_known=True)
    assert guard.summary()['unpriced_requests'] == 3
    assert guard.summary()['unknown_requests'] == 0
    assert guard.summary()['total_cost_known'] is False


def test_rejected_request_releases_reservation_but_uncertainty_cannot_be_reset(db):
    guard=BudgetGuard(db,1,priced(),limit_usd=.01)
    token=guard.before_request('x','instructions',100)
    guard.settle(token,None,known_rejected=True)
    assert guard.summary()['spent_usd'] == 0
    assert guard.summary()['reserved_usd'] == 0
    unknown=guard.before_request('x','instructions',100)
    guard.settle(unknown,None)
    guard.settle(unknown,0,known_rejected=True)
    assert guard.summary()['unknown_requests'] == 1


def test_budget_reservation_is_atomic_between_competing_threads(db):
    guard=BudgetGuard(db,1,priced(),limit_usd=.002)
    accepted=[]
    def reserve():
        try: accepted.append(guard.before_request('x','instructions',100))
        except BudgetError: pass
    threads=[threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    assert len(accepted) == 1
    assert guard.summary()['reserved_usd'] <= .002


def test_generator_budget_checks_zero_cap_before_network(db):
    row=task(db)
    client=httpx.Client(transport=httpx.MockTransport(lambda request:pytest.fail('cap did not block HTTP')))
    config=priced()
    guard=BudgetGuard(db,1,config,limit_usd=0)
    generator=CommentGenerator(config,db,client,'local-test-key')
    with pytest.raises(BudgetError):
        generator.generate(row['id'],row,budget_guard=guard)
    assert db.usage_summary()['requests'] == 0


def test_429_each_actual_attempt_has_reservation_rejected_cost_zero(db):
    row=task(db)
    replies=iter([httpx.Response(429),httpx.Response(200,json=response())])
    client=httpx.Client(transport=httpx.MockTransport(lambda request:next(replies)))
    config=priced()
    guard=BudgetGuard(db,1,config,limit_usd=.05)
    generator=CommentGenerator(config,db,client,'local-test-key')
    generator.http.sleep=lambda _:None
    assert 'RubUP' in generator.generate(row['id'],row,budget_guard=guard)
    assert guard.summary()['requests'] == db.usage_summary()['requests'] == 2
    assert guard.summary()['spent_usd'] == .00012
    assert guard.summary()['reserved_usd'] == 0


def test_paid_candidate_repair_must_fit_remaining_cap(db):
    row=task(db); config=priced()
    probe=CommentGenerator(config,db,api_key='test')
    preview=BudgetGuard(db,'preview',config)
    amount=preview.estimate_request(probe._context(row,[]),SYSTEM_INSTRUCTION,config.max_output_tokens)
    guard=BudgetGuard(db,1,config,limit_usd=float(amount))
    measured=int(amount*1_000_000)-1
    calls=[]
    client=httpx.Client(transport=httpx.MockTransport(lambda request:calls.append(request) or
        httpx.Response(200,json=response('нет бренда',{'input_tokens':measured,'output_tokens':0}))))
    generator=CommentGenerator(config,db,client,'test')
    with pytest.raises(BudgetError):
        generator.generate(row['id'],row,budget_guard=guard)
    assert len(calls) == 1
    assert db.usage_summary()['requests'] == 1
    assert guard.summary()['requests'] == 1
    probe.close()


def test_ambiguous_transport_blocks_next_generation_even_unlimited(db):
    row=task(db); config=priced()
    calls=[]
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('sensitive URL must not leak')
    generator=CommentGenerator(config,db,httpx.Client(transport=httpx.MockTransport(timeout)),'test')
    guard=BudgetGuard(db,1,config)
    with pytest.raises(IntegrationError):generator.generate(row['id'],row,budget_guard=guard)
    with pytest.raises(BudgetError):generator.generate(row['id'],row,budget_guard=guard)
    assert len(calls)==1
    assert guard.summary()['unknown_requests']==1
    assert db.usage_summary()['total_cost'] is None


@pytest.mark.parametrize('input_tokens', [True, 1.9, '100'])
def test_malformed_usage_never_becomes_a_known_bill(db, input_tokens):
    row=task(db); config=priced()
    data=response(usage={'input_tokens':input_tokens,'output_tokens':10})
    generator=CommentGenerator(config,db,httpx.Client(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json=data))),'test')
    guard=BudgetGuard(db,1,config,limit_usd=.05)
    assert 'RubUP' in generator.generate(row['id'],row,budget_guard=guard)
    assert guard.summary()['unknown_requests']==1
    assert db.usage_summary()['total_cost'] is None
