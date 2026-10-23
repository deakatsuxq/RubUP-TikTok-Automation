"""Different accounts receive wording diversity, while demo has no API calls."""
import httpx
import pytest

from ai_comment_generator import CommentGenerator
from config import Config
from database import Database
from models import Video


def reply(text):
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':text}]}],
            'usage':{'input_tokens':100,'output_tokens':10}}


def test_near_copy_for_second_account_regenerates_using_global_history(tmp_path):
    db=Database(tmp_path/'history.sqlite3')
    db.add_video(Video('123','https://www.tiktok.com/@fixture/video/123'))
    one=db.ensure_task('one','123');two=db.ensure_task('two','123')
    db.update_task(one['id'],comment='реклама: RubUP, какой скин выбрать 123?')
    values=iter(['реклама: в рубапе какой скин выбрать 456?',
                 'реклама: что думаете об обменах в RubUP?'])
    calls=[]
    client=httpx.Client(transport=httpx.MockTransport(lambda request:calls.append(request) or
        httpx.Response(200,json=reply(next(values)))))
    generator=CommentGenerator(Config(),db,client,'test')
    result=generator.generate(two['id'],two)
    assert result == 'реклама: что думаете об обменах в RubUP?'
    assert len(calls)==2
    assert db.usage_summary()['requests']==2
    db.close()


def test_dry_run_fifty_videos_two_accounts_has_hundred_explicit_unique_drafts(tmp_path):
    db=Database(tmp_path/'demo.sqlite3')
    config=Config(dry_run=True)
    def no_network(request):pytest.fail('Demo made a network request')
    generator=CommentGenerator(config,db,httpx.Client(transport=httpx.MockTransport(no_network)))
    comments=[]
    for number in range(50):
        video_id=str(9000000000000000000+number)
        db.add_video(Video(video_id,f'https://www.tiktok.com/@test_fixture/video/{video_id}',is_test=True))
        for slot in (1,2):
            task=db.ensure_task(f'account-{slot}',video_id)
            text=generator.generate(task['id'],{**task,'account_slot':slot})
            assert text.startswith('тест:') and 'RubUP' in text
            db.update_task(task['id'],comment=text)
            comments.append(text)
    assert len(comments)==len(set(comments))==100
    assert all(comments[index] != comments[index+1] for index in range(0,100,2))
    assert db.usage_summary()['requests']==0
    assert db.stats()['published']==0
    assert db.stats()['telegram_sent']==0
    assert db.stats()['confirmed_reward']==0
    db.close()
