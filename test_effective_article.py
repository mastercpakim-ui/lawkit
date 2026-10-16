"""Synthetic transport controls; actual cached publisher replay is separate."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import lawkit


class EffectiveArticleTests(unittest.TestCase):
    def setUp(self):
        self.rows=[dict(mst='9',efYd='20260101',pubDate='20251201',pubNo='1'),
                   dict(mst='10',efYd='20260101',pubDate='20251201',pubNo='2'),
                   dict(mst='11',efYd='20270101',pubDate='20260901',pubNo='3')]
        self.body={'법령':{'기본정보':{'법령명_한글':'예시법','시행일자':'20260101',
                    '공포일자':'20251201','공포번호':'2'},
                    '조문':{'조문단위':{'조문여부':'조문','조문번호':'1','조문제목':'예시',
                    '조문시행일자':'20260101','조문내용':'제1조 제2조에 따른다. <img src="https://example.test/form.png">'}}}}
        self.calls=[]
        def get(path,params,ttl,refresh=False,valid=None):
            self.calls.append(dict(path=path,params=deepcopy(params)))
            return deepcopy(self.body),lawkit._public_url(path,params)
        self.get=patch.object(lawkit,'_get',side_effect=get);self.get.start()
        self.versions=patch.object(lawkit,'law_versions',return_value=dict(versions=self.rows));self.versions.start()
        self.addCleanup(self.get.stop);self.addCleanup(self.versions.stop)

    def test_default_today_excludes_future_and_numeric_mst_tiebreak(self):
        with patch.object(lawkit.time,'strftime',return_value='20261006'):
            result=lawkit._call('law_article',dict(name='예시법',jo='제1조'))
        self.assertEqual(result['requestedDate'],'20261006')
        self.assertEqual((result['mst'],result['efYd']),('10','20260101'))
        self.assertEqual(self.calls[0]['params'],dict(target='eflaw',MST='10',efYd='20260101',JO='000100'))
        self.assertEqual(result['articles'][0]['image_links'],['https://example.test/form.png'])
        for flag in ['legal_application_verified','history_coverage_verified']:
            self.assertFalse(result[flag])
        self.assertFalse(result['articles'][0]['image_visual_check_performed'])

    def test_invalid_calendar_date_or_no_edition_rejects_before_body(self):
        for date in ['20260230','20250101']:
            with self.subTest(date=date),self.assertRaises((ValueError,LookupError)):
                lawkit.law_article('예시법','제1조',date)
        self.assertEqual(self.calls,[])

    def test_each_identity_field_is_required_and_must_match(self):
        original=deepcopy(self.body)
        for key in ['법령명_한글','시행일자','공포일자','공포번호']:
            for missing in [True,False]:
                with self.subTest(key=key,missing=missing):
                    self.body=deepcopy(original)
                    if missing:self.body['법령']['기본정보'].pop(key)
                    else:self.body['법령']['기본정보'][key]='synthetic mismatch'
                    before=len(self.calls)
                    with self.assertRaises(LookupError):lawkit.law_article('예시법','제1조','20261006')
                    self.assertEqual(len(self.calls),before+1)

    def test_empty_duplicate_or_wrong_article_never_falls_back(self):
        unit=deepcopy(self.body['법령']['조문']['조문단위'])
        cases=[[],[unit,deepcopy(unit)],dict(unit,조문번호='2'),dict(unit,조문내용='')]
        for units in cases:
            with self.subTest(units=units):
                self.body['법령']['조문']['조문단위']=units
                before=len(self.calls)
                with self.assertRaises(LookupError):lawkit.law_article('예시법','제1조','20261006')
                self.assertEqual(len(self.calls),before+1)

    def test_legacy_refs_calls_validated_articles_without_new_options(self):
        calls = []
        def get(path, params, ttl, refresh=False, valid=None):
            calls.append(deepcopy(params))
            body = deepcopy(self.body)
            if params['JO'] == '000200':
                body['법령']['조문']['조문단위'].update(
                    조문번호='2', 조문내용='제2조 검증할 원문')
            return body, lawkit._public_url(path, params)
        with patch.object(lawkit, '_get', side_effect=get):
            result = lawkit._call('law_refs', dict(name='예시법', jo='제1조', efYd='20261006'))
        self.assertEqual([c['JO'] for c in calls], ['000100', '000200'])
        self.assertTrue(all(c['target'] == 'eflaw' for c in calls))
        self.assertEqual(result['refs'][0]['head'], '제2조 검증할 원문')
        self.assertNotIn('error', result['refs'][0])


if __name__=='__main__':unittest.main()
