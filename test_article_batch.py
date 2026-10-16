"""Batch transport controls, independent of network and model execution."""
import json
import unittest
from unittest.mock import patch

import lawkit


class ArticleBatchTests(unittest.TestCase):
    def setUp(self):
        self.requests = [dict(name='예시법', jo='제1조', efYd='20260101'),
                         dict(name='예시법', jo='제1조', efYd='20261006')]

    def test_rpc_preserves_each_complete_result_and_distinct_requested_dates(self):
        def article(**request):
            return dict(requestedDate=request['efYd'], articles=[dict(text='전문 (예외 포함)')],
                        history_coverage_verified=False, source='test://source')
        expected = [article(**r) for r in self.requests]
        with patch.object(lawkit, 'law_article', side_effect=article) as fetch:
            response = lawkit.handle_rpc(json.dumps(dict(jsonrpc='2.0', id=1, method='tools/call',
                params=dict(name='law_articles', arguments=dict(requests=self.requests, refresh=True)))))
        result = json.loads(response['result']['content'][0]['text'])
        self.assertEqual([i['result'] for i in result['items']], expected)
        self.assertEqual([i['request'] for i in result['items']], self.requests)
        self.assertEqual(fetch.call_args_list[0].kwargs, dict(self.requests[0], refresh=True))
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(result['all_requests_succeeded'])
        self.assertFalse(result['history_coverage_verified'])

    def test_failed_item_does_not_hide_later_success_or_fallback(self):
        with patch.object(lawkit, 'law_article', side_effect=[LookupError('edition mismatch'), {'text': 'later'}]) as fetch:
            result = lawkit.law_articles(self.requests)
        self.assertEqual([i['status'] for i in result['items']], ['error', 'ok'])
        self.assertEqual(result['items'][0]['error']['type'], 'LookupError')
        self.assertNotIn('result', result['items'][0])
        self.assertEqual(result['items'][1]['result'], {'text': 'later'})
        self.assertEqual((result['succeeded_count'], result['failed_count']), (1, 1))
        self.assertFalse(result['all_requests_succeeded'])
        self.assertEqual(fetch.call_count, 2)

    def test_full_input_validation_precedes_any_fetch(self):
        invalid = [None, [], self.requests * 16,
                   [self.requests[0], dict(name='예시법', jo='제1조')],
                   [self.requests[0], dict(self.requests[1], efYd='20260230')],
                   [dict(self.requests[0], efYd='2026-01-01')],
                   [dict(self.requests[0], jo='not an article')],
                   [dict(self.requests[0], name=' ')],
                   [dict(self.requests[0], extra=True)]]
        for requests in invalid:
            with self.subTest(requests=requests), patch.object(lawkit, 'law_article') as fetch:
                with self.assertRaises(ValueError):
                    lawkit.law_articles(requests)
                fetch.assert_not_called()
        with patch.object(lawkit, 'law_article') as fetch, self.assertRaises(ValueError):
            lawkit._call('law_articles', dict(requests=self.requests, refresh='false'))
        fetch.assert_not_called()

    def test_all_failures_remain_explicit_and_schema_is_discoverable(self):
        with patch.object(lawkit, 'law_article', side_effect=RuntimeError('source unavailable')):
            result = lawkit.law_articles(self.requests)
        self.assertEqual((result['succeeded_count'], result['failed_count']), (0, 2))
        self.assertFalse(result['all_requests_succeeded'])
        listed = lawkit.handle_rpc(json.dumps(dict(jsonrpc='2.0', id=2, method='tools/list')))
        tool = next(t for t in listed['result']['tools'] if t['name'] == 'law_articles')
        self.assertEqual(tool['inputSchema']['properties']['requests']['items']['required'], ['name', 'jo', 'efYd'])


if __name__ == '__main__':
    unittest.main()
