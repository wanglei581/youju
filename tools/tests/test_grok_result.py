import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('grok_result', Path(__file__).parents[1] / 'team/grok_result.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GrokResultTests(unittest.TestCase):
    def check_result(self, events, code=0, log=''):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'stream.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            (p/'log.txt').write_text(log)
            (p/'out.md').write_text('旧结果')
            result = MODULE.parse_result(p, code)
            return result, (p/'out.md').read_text(), json.loads((p/'status.json').read_text())

    def test_timestamp_and_generated_402_are_not_quota_failure(self):
        r, body, status = self.check_result([{'type':'result','subtype':'success','is_error':False,'result':'审阅第402条'}], log='2026-10-15T06:41:20.179402Z WARN unrelated plugin')
        self.assertEqual(r, 0)
        self.assertEqual(body, '审阅第402条')
        self.assertTrue(status['ok'])

    def test_real_quota_failure(self):
        r, body, _ = self.check_result([], 1, 'HTTP 402 Payment Required')
        self.assertEqual((r, body), (2, ''))

    def test_interrupted_and_error_results_never_publish_partial_output(self):
        for event, code in [({'type':'result','subtype':'success','is_error':False,'result':'partial'}, 1),
                            ({'type':'result','subtype':'error_max_turns','is_error':True,'result':'partial'}, 0),
                            ({'type':'result','subtype':'success','is_error':False,'result':''}, 0)]:
            with self.subTest(event=event, code=code):
                r, body, _ = self.check_result([event], code)
                self.assertEqual((r, body), (1, ''))

    def test_missing_result_clears_old_output(self):
        self.assertEqual(self.check_result([{'type':'assistant','text':'working'}])[:2], (1, ''))

    def test_final_failure_overrides_earlier_success(self):
        events = [{'type':'result','subtype':'success','is_error':False,'result':'old'},
                  {'type':'result','subtype':'error','is_error':True,'result':'failed'}]
        self.assertEqual(self.check_result(events)[:2], (1, ''))

if __name__ == '__main__':
    unittest.main()
