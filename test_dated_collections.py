"""Synthetic controls for dated full-text search and publication addenda."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import lawkit


class DatedCollectionTests(unittest.TestCase):
    def setUp(self):
        self.edition=dict(mst='100',efYd='20260401',pubDate='20260201',pubNo='10')
        self.display=dict(mst='99',efYd='20250101',pubDate='20250101',pubNo='9')
        self.rows=[self.edition,dict(self.edition,efYd='20260201')]
        self.addenda=[dict(부칙공포번호=str(i),부칙공포일자='20260'+str(i)+'01',부칙내용=text)
            for i,text in [(4,'적용례 긴 원문입니다'),(3,'적용례 다른 원문'),(2,'관계없는 문장'),(1,'적용례 과거 원문')]]
        self.calls=[];self.mutate=None
        def get(path,params,ttl,refresh=False,valid=None):
            self.calls.append(deepcopy(params));info=self.display if params['MST']=='99' else self.edition
            data={'법령':{'기본정보':{'법령명_한글':'예시법','공포일자':info['pubDate'],'공포번호':info['pubNo'],
                '시행일자':info['efYd'] if params['target']=='eflaw' else info['pubDate']},
                '부칙':{'부칙단위':deepcopy(self.addenda)},'조문':{'조문단위':[
                    {'조문여부':'조문','조문번호':str(i),'조문제목':'검토','조문내용':f'제{i}조 검토 항목'} for i in [1,2]]}}}
            if self.mutate:self.mutate(data)
            return data,lawkit._public_url(path,params)
        for target,kwargs in [('law_versions',dict(side_effect=lambda *a:dict(versions=self.rows))),
                              ('find_law',dict(return_value=self.display)),('_get',dict(side_effect=get))]:
            mock=patch.object(lawkit,target,**kwargs);mock.start();self.addCleanup(mock.stop)

    def test_grep_default_uses_dated_body_and_counts_real_full_scan(self):
        with patch.object(lawkit.time,'strftime',return_value='20261006'):
            r=lawkit._call('law_grep',dict(name='예시법',keywords=['검토'],limit=1))
        self.assertEqual(self.calls,[dict(target='eflaw',MST='100',efYd='20260401')])
        self.assertEqual((r['requestedDate'],r['efYd']),('20261006','20260401'))
        self.assertEqual((r['scanned'],r['total_articles'],r['matched_total'],r['matched']),(2,2,2,1))
        self.assertEqual(r['articles'][0]['jo'],'제1조');self.assertTrue(r['truncated'])

    def test_addenda_preserves_publication_and_selected_dates_separately(self):
        r=lawkit._call('law_addenda',dict(name='예시법',efYd='20261006',limit=10,max_chars=100))
        self.assertEqual((r['requestedDate'],r['selected_efYd'],r['efYd']),('20261006','20260401','20260201'))
        self.assertEqual([a['text'] for a in r['addenda']],[a['부칙내용'] for a in self.addenda])
        self.assertEqual(r['metadata_checked'],['법령명_한글','공포일자','공포번호'])
        self.assertFalse(r['mst_identity_verified']);self.assertFalse(r['applicable_text_verified'])
        self.assertTrue(r['coverage']['full_stored_addenda_returned'])

    def test_explicit_mst_and_legacy_default_paths(self):
        pinned=lawkit._call('law_addenda',dict(name='예시법',mst='100'))
        with patch.object(lawkit.time,'strftime',return_value='20261006'):
            default=lawkit._call('law_addenda',dict(name='예시법'))
        self.assertEqual([c['MST'] for c in self.calls],['100','99'])
        self.assertIsNone(pinned['requestedDate']);self.assertIsNone(pinned['selected_efYd'])
        self.assertEqual(default['selection'],'current_display_publication')

    def test_metadata_mismatch_and_inconsistent_mst_rows_fail(self):
        for key in ['법령명_한글','공포일자','공포번호']:
            with self.subTest(key=key):
                self.mutate=lambda d,k=key:d['법령']['기본정보'].__setitem__(k,'mismatch')
                with self.assertRaises(LookupError):lawkit.law_addenda('예시법',mst='100')
        self.mutate=None;before=len(self.calls)
        self.rows.append(dict(self.edition,pubNo='11'))
        with self.assertRaises(LookupError):lawkit.law_addenda('예시법',mst='100')
        self.assertEqual(len(self.calls),before)

    def test_missing_structure_is_not_an_empty_list(self):
        self.mutate=lambda d:d['법령'].pop('부칙')
        with self.assertRaises(LookupError):lawkit.law_addenda('예시법',mst='100')
        self.mutate=lambda d:d['법령'].__setitem__('부칙',{'부칙단위':[]})
        empty=lawkit.law_addenda('예시법',mst='100')
        self.assertEqual(empty['coverage']['response_structure'],'explicit_empty')
        self.assertEqual(empty['count'],0);self.assertIn('미확인',empty['note'])

    def test_every_filter_and_text_limit_remains_visible(self):
        r=lawkit.law_addenda('예시법',mst='100',last=3,keywords=['적용례'],limit=1,max_chars=3)
        self.assertEqual(r['coverage'],dict(response_structure='present',omitted_by_last=1,
            excluded_by_keywords=1,omitted_by_limit=1,truncated_body_count=1,full_stored_addenda_returned=False))
        self.assertTrue(r['addenda'][0]['text_truncated'])
        self.assertIn('생략',r['addenda'][0]['text'])

    def test_conflicting_selectors_and_invalid_limits_fail_before_transport(self):
        for kwargs in [dict(mst='100',efYd='20261006'),dict(limit=0),dict(max_chars=None),dict(last=-1)]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):lawkit.law_addenda('예시법',**kwargs)
        self.assertEqual(self.calls,[])

    def test_malformed_full_body_unit_is_not_silently_excluded(self):
        for invalid in [None,{},dict(조문여부='알수없음'),dict(조문여부='전문',조문내용='미확인 문장')]:
            with self.subTest(invalid=invalid):
                self.mutate=lambda d,u=invalid:d['법령']['조문']['조문단위'].append(u)
                with self.assertRaises(LookupError):lawkit.law_grep('예시법',['검토'],efYd='20261006')
        self.mutate=lambda d:d['법령']['조문']['조문단위'].append(dict(조문여부='전문',조문내용='제1장 총칙'))
        self.assertEqual(lawkit.law_grep('예시법',['검토'],efYd='20261006')['scanned'],2)

    def test_branched_headings_do_not_block_full_article_scan(self):
        # Heading forms observed in the T04 live DRF bodies, including a deleted section.
        headings = ['제2절의2 삭제 <2024.12.31>',
                    '제2장의3 각 연결사업연도의 소득에 대한 법인세 <신설 2008.12.26>',
                    '제6관의2 소득공제', '제5장의10 사업재편계획을 위한 조세특례']
        for heading in headings:
            with self.subTest(heading=heading):
                self.mutate=lambda d,t=heading:d['법령']['조문']['조문단위'].insert(
                    1,dict(조문여부='전문',조문내용='              '+t))
                result=lawkit.law_grep('예시법',['검토'],efYd='20261006')
                self.assertEqual((result['scanned'],result['matched_total']),(2,2))
                self.assertEqual([a['jo'] for a in result['articles']],['제1조','제2조'])

    def test_malformed_branch_heading_still_blocks_scan(self):
        for heading in ['제2절의 삭제', '제2절의0 삭제', '제2절의2삭제',
                        '제2조의2 실제 조문인데 전문으로 잘못 표시']:
            with self.subTest(heading=heading):
                self.mutate=lambda d,t=heading:d['법령']['조문']['조문단위'].append(
                    dict(조문여부='전문',조문내용=t))
                with self.assertRaises(LookupError):
                    lawkit.law_grep('예시법',['검토'],efYd='20261006')


if __name__=='__main__':unittest.main()
