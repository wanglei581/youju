"""读运行日志的两只工具：AI 成员在终端里只看到格式固定的摘要，日志正文、IP、用户编号一律不出现
（影响评估第六节（二）第 6 条、（三）第 7 条）。

样本全是编的：IP 用文档专用地址段（RFC 5737、RFC 3849），用户编号、openid、环境号随手写；云托管接口用假模块
代替，不碰线上日志。漏遮写法照 Grok 10/23 找出的那几种（"认出编号再遮"的旧做法都会漏），加上 #346 补的几种。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1] / 'team'
REPO = TEAM.parents[1]
SPEC = importlib.util.spec_from_file_location('cloud_logs_show', TEAM / 'cloud_logs_show.py')
SHOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHOW)

IP4 = '203.0.113.77'
IP6 = '2001:db8:85a3::8a2e:370:7334'
UID = '9f3a1c0b7d2e4a68'
UID_B = 'b1a2c3d4e5f60718'
OPENID = 'oTEST' + 'x' * 23
ENV = 'prod-env.7072-prod-env-1300000000'
RID = '0f' * 16
REVIEWER = '核对人甲'
TS = '2026-10-19 11:00:00.000'
# 终端输出里不许出现的原始标识，任何一段都不行（样本里的时间、容器名、路径都避开了这些字样）
FORBIDDEN = ('203', '113', '.77', '9f3a', '1c0b', '7d2e', '4a68', '1a2c3d4e', 'oTEST', 'xxxxxxxx', 'prod-env', '7072',
             '1300000000', 'db8', '8a2e', '7334', 'testclient', REVIEWER, RID, 'fake-secret')
# 完整的值：cloud-logs.cjs 的终端输出里带着随机的临时目录路径，那里只查完整的值
PERSONAL = (IP4, IP6, UID, UID_B, OPENID, ENV)

# Grok 10/23 找出的漏遮写法（\\n 是日志文字里真有反斜杠和字母 n），#346 补的几种，以前的对照
LEAKY = [
    'connect from 203.0.113\\n.77',
    'uid 9f3a1c0b\\n7d2e4a68 done',
    f'cloud://\\n{ENV}/advisor/out/{UID}/12.xlsx',
    'connect from 203%2e0%2e113%2e77',
    'advisor/out/9f3a1c0b7d2e4a%368/1.xlsx',
    f'cloud:\\/\\/{ENV}\\/advisor\\/out\\/{UID}\\/12.xlsx',
    'user_' + OPENID,
    'id-' + OPENID,
    '1203.0.113.77',
    'connect from 203.0.113\n.77',
    f'connect from {IP4}', f'openid {OPENID}', f'cloud://{ENV}/advisor/out/{UID}/12.xlsx',
    '\\' + 'u005c' * 10000 + 'n' + UID,
    f'C:\\work\\{UID_B}\\1.xlsx', f'C:\\work\\u005c\\u0062{UID_B[1:]}\\1.xlsx', '\\b' + UID, '\\f' + IP4,
    f'[{IP6}]:443', 'IPv6:' + IP6, '::ffff:203.0.113.9', '\\u5740' + IP4, '\\u003e' + UID, '\\x07' + UID,
    '{"message":"rows\\n' + UID + '\\t' + OPENID + '"}', f'X-Original-Forwarded-For: 198.51.100.7, {IP4}',
    f"Duplicate entry '{OPENID}' for key 'users.openid'", f'删除 advisor/out/{UID}/12.xlsx 失败', REVIEWER,
]


def assert_clean(test, text):
    for value in FORBIDDEN:
        test.assertNotIn(value, text)


def access_line(ip=IP4, uid=UID, **fields):
    line = {'event': 'access', 'service': 'api', 'method': 'GET', 'path': '/api/v1/search', 'status': 200, 'ms': 35,
            'ip': ip, 'rid': RID, 'at': '2026-10-19T03:00:00.123456+00:00', 'uid': uid, 'saved': True}
    return json.dumps({**line, **fields}, ensure_ascii=False)


def result(content, container='youju-api-054', ts=TS):
    return {'Timestamp': ts, 'TopicId': 't', 'Source': '10.0.0.5',
            'Content': json.dumps({'__CONTENT__': content, '__TAG__': {'container_name': container}}, ensure_ascii=False)}


def every_slot(sample):
    """把一个样本放进摘要会读的每个位置。"""
    audit = json.dumps({'event': sample, 'action': sample, 'ip': sample, 'reviewer': REVIEWER})
    return [
        result(sample), result(access_line(sample, sample)),
        result(access_line(method=sample, path=sample, status=sample, ms=sample)),
        result(access_line(path='/api/v1/' + sample + '?q=' + sample)),
        result({'event': 'access', 'method': sample, 'path': sample, sample: sample}),
        result('x', container=sample), result('x', ts=sample),
        result(f'2026-10-21 10:38:02,317 INFO {sample} rest'), result(f'2026-10-21 10:38:02,317 INFO youju.audit {audit}'),
        result(f'INFO:     {sample}'), result(sample + 'Error ' + sample + ' X' + sample + 'Exception'),
        result(f'Traceback (most recent call last):\n  File "x.py"\nValueError: {sample}'),
        {'Timestamp': sample, 'Content': sample}, {'Timestamp': TS, 'Content': {'__CONTENT__': sample, '__TAG__': {'container_name': sample}}},
        sample,
    ]


def run_show_file(test, data, *keys):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'logs.json'
        path.write_text(json.dumps(data, ensure_ascii=False))
        return subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(path), *keys], capture_output=True, text=True, timeout=60)


class SummaryTests(unittest.TestCase):
    def test_access_line_shows_only_whitelisted_fields(self):
        for ip in (IP4, IP6, '::ffff:203.0.113.9', 'testclient', ''):
            with self.subTest(ip=ip):
                self.assertEqual(SHOW.summary(result(access_line(ip))), f'{TS} youju-api-054 GET /api/v1/search 200 35ms')

    def test_access_fields_are_checked(self):
        cases = [
            ({'path': f'/api/v1/notices/123/attachments?q={IP4}#x'}, 'GET /api/v1/notices/123/attachments 200 35ms'),
            ({'path': f'/api/v1/x/{UID}/{IP4}/核对/a%2eb/12345678901/v1'}, 'GET /api/v1/x/*/*/*/*/*/v1 200 35ms'),
            ({'path': 'no-slash'}, 'GET ? 200 35ms'),
            ({'method': 'BREW', 'status': True, 'ms': -1}, '? /api/v1/search ? ?ms'),
            ({'status': '200', 'ms': '35'}, 'GET /api/v1/search ? ?ms'),
            # True 在 Python 里是 1：不能当成 1 毫秒显示
            ({'ms': True}, 'GET /api/v1/search 200 ?ms'),
            ({'status': 503, 'ms': 1234.9}, 'GET /api/v1/search 503 1234ms'),
        ]
        for fields, want in cases:
            with self.subTest(fields=fields):
                self.assertEqual(SHOW.summary(result(access_line(**fields))), f'{TS} youju-api-054 {want}')

    def test_other_lines_show_only_their_category(self):
        audit = json.dumps({'event': 'admin', 'action': 'approve', 'ip': IP4, 'reviewer': REVIEWER, 'deadline_id': 1}, ensure_ascii=False)
        cases = [
            (f'2026-10-21 10:38:02,317 INFO youju.audit {audit}', 'INFO youju.audit admin/approve'),
            (f'2026-10-21 04:00:00,001 WARNING youju.backup: 备份失败 {UID}', 'WARNING youju.backup'),
            (f'2026-10-21 04:00:00,001 ERROR {UID}: x', 'ERROR'),
            ('INFO:     Application startup complete.', 'INFO uvicorn'),
            (f'Traceback (most recent call last):\n  File "a.py"\nsqlalchemy.exc.OperationalError: (2003, "on {IP4}")', 'Traceback OperationalError'),
            ('{"event": "alert-probe", "configured": true}', 'alert-probe'),
            (f'{{"event": "{UID}", "action": "b{UID}"}}', '-'),
            (f'工具任务：processed 3 {IP4}', '-'),
        ]
        for content, want in cases:
            with self.subTest(content=content[:40]):
                self.assertEqual(SHOW.summary(result(content)), f'{TS} youju-api-054 {want}')

    def test_time_and_container_must_look_right(self):
        self.assertEqual(SHOW.summary(result('x', container=UID)), f'{TS} ? -')
        self.assertEqual(SHOW.summary(result('x', container='youju-api-054-6d8f7c9b5-x2k4q')), f'{TS} youju-api-054-6d8f7c9b5-x2k4q -')
        self.assertEqual(SHOW.summary(result('x', ts=f'{IP4} {UID}')), '? youju-api-054 -')
        self.assertEqual(SHOW.summary(result('x', ts='2026-10-19T11:00:00')), '2026-10-19T11:00:00 youju-api-054 -')
        self.assertEqual(SHOW.summary(f'not a record {IP4}'), '? ? -')

    def test_no_raw_identifier_reaches_the_terminal(self):
        # 每个漏遮样本放进每个位置，整份结果交给显示脚本：输出里不许出现任何一段原始标识
        records = [r for sample in LEAKY for r in every_slot(sample)]
        for keys in ([], ['connect'], ['失败', 'x'], ['advisor']):
            with self.subTest(keys=keys):
                run = run_show_file(self, {'count': len(records), 'listOver': True, 'pages': 1, 'results': records}, *keys)
                self.assertEqual(run.returncode, 0, run.stderr)
                assert_clean(self, run.stdout + run.stderr)
                body = [l for l in run.stdout.splitlines() if not l.startswith('#')]
                self.assertTrue(body)
                if not keys:
                    self.assertEqual(len(body), len(records))

    def test_error_file_shows_only_kind_and_codes(self):
        cases = [
            ({'error': 'api', 'code': 'InvalidParameter', 'message': f'bad request from {IP4} uid {UID}'}, '# 出错：api，code=InvalidParameter'),
            ({'error': 'agent', 'ret': -1}, '# 出错：agent，ret=-1'),
            ({'error': 'exception', 'name': 'SyntaxError', 'code': 'ECONNRESET', 'message': f'x {OPENID}'}, '# 出错：exception，code=ECONNRESET，name=SyntaxError'),
        ]
        for data, want in cases:
            with self.subTest(data=data):
                run = run_show_file(self, data)
                self.assertEqual(run.returncode, 1)
                self.assertEqual(run.stdout.strip(), want + '；出错原文在结果文件里，这里不显示')
        for sample in LEAKY:
            with self.subTest(sample=sample[:30]):
                run = run_show_file(self, {'error': sample, 'code': sample, 'name': sample, 'ret': sample, 'message': sample})
                self.assertEqual(run.returncode, 1)
                assert_clean(self, run.stdout + run.stderr)
        run = run_show_file(self, {'error': 'agent', 'ret': True, 'code': UID, 'name': OPENID})
        self.assertEqual(run.stdout.strip(), '# 出错：agent；出错原文在结果文件里，这里不显示')

    def test_summary_line_values_are_checked(self):
        for sample in LEAKY[:12]:
            with self.subTest(sample=sample[:30]):
                run = run_show_file(self, {'count': sample, 'listOver': sample, 'pages': sample, 'truncated': sample, 'results': []})
                self.assertEqual(run.returncode, 0, run.stderr)
                assert_clean(self, run.stdout + run.stderr)
                self.assertIn('# 共 ? 行，listOver=?，pages=?；', run.stdout)
        run = run_show_file(self, {'count': 20, 'listOver': False, 'pages': 20, 'truncated': True, 'results': []})
        self.assertIn('# 共 20 行，listOver=False，pages=20，到了 20 页上限没取完；', run.stdout)

    def test_keywords_find_lines_without_showing_them(self):
        data = {'count': 3, 'listOver': True, 'pages': 1, 'results': [
            result(f'工具任务：processed 3 {IP4}'), result('INFO:     Application startup complete.'),
            result(access_line(path='/api/v1/search')), result('{"event": "alert-probe", "configured": true}')]}
        run = run_show_file(self, data, '工具任务')
        self.assertEqual(run.stdout.splitlines()[1:], [f'{TS} youju-api-054 -', '# 含关键词的有 1 行'])
        run = run_show_file(self, data, 'Application startup complete', '"configured": true')
        self.assertEqual(run.stdout.splitlines()[1:], [f'{TS} youju-api-054 INFO uvicorn', f'{TS} youju-api-054 alert-probe', '# 含关键词的有 2 行'])
        self.assertEqual(run_show_file(self, data, 'not_configured').stdout.splitlines()[-1], '# 含关键词的有 0 行')
        # 完整的 IP、用户编号、openid 不能当关键词：按人查由创始人在控制台自己查
        for key in (IP4, IP6, '::ffff:203.0.113.9', UID, OPENID, f'from {IP4}.', 'x' + UID):
            with self.subTest(key=key):
                run = run_show_file(self, data, '工具任务', key)
                self.assertEqual(run.returncode, 2)
                self.assertEqual(run.stdout.strip(), '# 完整的 IP、用户编号、openid 不能当关键词：按人查由创始人在云托管控制台的日志页自己查')
        for key in ('203.0.', '9f3a', 'oTES', 'v1/search'):
            with self.subTest(key=key):
                self.assertEqual(run_show_file(self, data, key).returncode, 0)

    def test_deep_nesting_and_long_lines_stay_bounded(self):
        # Grok 第 6 种：深层嵌套的 JSON 以前会让显示脚本抛 RecursionError
        deep = {'Content': '{"__CONTENT__":' + '[' * 2000 + ']' * 2000 + '}', 'Timestamp': 't'}
        self.assertEqual(list(SHOW.lines({'results': [deep]})), ['? ? -'])
        self.assertEqual(list(SHOW.lines({'results': [deep]}, ['x'])), [])
        run = run_show_file(self, {'results': [deep, result('[' * 5000 + ']' * 5000)]})
        self.assertEqual((run.returncode, run.stderr), (0, ''))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'deep.json'
            path.write_text('[' * 100000 + ']' * 100000)
            run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(path)], capture_output=True, text=True, timeout=60)
            self.assertEqual((run.returncode, run.stdout.strip(), run.stderr), (1, '# 结果文件读不了（RecursionError）', ''))
        started = time.monotonic()
        for content in ('A' * 200000, 'a' * 200000, '1:' * 100000, ('\\' + 'u005c') * 100000, '\\' * 200000 + 'q', 'Error' * 40000):
            self.assertIn(SHOW.summary(result(content)), (f'{TS} youju-api-054 -',))
            self.assertEqual(list(SHOW.lines({'results': [result(content)]}, ['zz'])), [])
        self.assertLess(time.monotonic() - started, 5)

    def test_usage_and_bad_files(self):
        run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py')], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run_show_file(self, [1, 2]).stdout.strip(), '# 这不是 cloud-logs.cjs 写下的结果文件')
        run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), '/nonexistent/x.json'], capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stdout.strip()), (1, '# 结果文件读不了（FileNotFoundError）'))


@unittest.skipUnless(shutil.which('node'), '需要 node')
class CloudLogsTests(unittest.TestCase):
    WINDOW = ['env-test', '2026-10-19 11:00:00', '2026-10-19 12:00:00', '*']

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'lib/api').mkdir(parents=True)
        (self.tmp / 'out').mkdir()
        # 假的 wxcloud 接口代理：按顺序返回 pages.json 里的回应，最后一页一直重复；每次调用记一行。
        # {"throw": …} 表示抛异常，{"asyncThrow": …} 表示在 await 接不住的地方抛；
        # {"lateThrow": …} 表示回应给完以后过一会儿再抛（连接后来才出错）。
        # FAKE_NOISY 时像真库一样把东西打出来，每条保护各有一个只有它能拦住的出口：NODE_ENV=DEBUG 时直接写终端，
        # DEBUG 有值时把假密钥直接写进 2 号文件描述符，另外还走 console 和 process.stderr.write
        (self.tmp / 'lib/api/base.js').write_text(
            "const fs = require('fs');\n"
            "const pages = JSON.parse(fs.readFileSync(process.env.FAKE_PAGES, 'utf8'));\n"
            "let i = 0;\n"
            "exports.fetchApi = async (url, body) => {\n"
            "  fs.appendFileSync(process.env.FAKE_CALLS, JSON.stringify(body) + '\\n');\n"
            "  const page = pages[Math.min(i++, pages.length - 1)];\n"
            "  if (process.env.FAKE_NOISY) {\n"
            "    if (process.env.NODE_ENV === 'DEBUG') process.stdout.write(JSON.stringify(page) + '\\n');\n"
            "    if (process.env.DEBUG) fs.writeSync(2, 'X-CloudRunCli-Key: fake-secret-debug\\n');\n"
            "    for (const k of ['log', 'info', 'warn', 'error', 'debug']) console[k](JSON.stringify(page));\n"
            "    process.stderr.write('fake-secret-stderr ' + JSON.stringify(page) + '\\n');\n"
            "  }\n"
            "  if (page.throw) throw Object.assign(new Error(page.throw.message), { code: page.throw.code });\n"
            "  if (page.asyncThrow) {\n"
            "    setTimeout(() => { throw Object.assign(new Error(page.asyncThrow.message), { code: page.asyncThrow.code }); }, 0);\n"
            "    return new Promise(() => {});\n"
            "  }\n"
            "  if (page.lateThrow) setTimeout(() => { throw Object.assign(new Error(page.lateThrow.message), { code: page.lateThrow.code }); }, 20);\n"
            "  return page;\n"
            "};\n")
        self.out = self.tmp / 'out/logs.json'

    def env(self, pages, **extra_env):
        (self.tmp / 'pages.json').write_text(json.dumps(pages))
        return {**os.environ, 'WXCLOUD_CLI_LIB': str(self.tmp / 'lib'), 'FAKE_PAGES': str(self.tmp / 'pages.json'),
                'FAKE_CALLS': str(self.tmp / 'calls.txt'), **extra_env}

    def run_logs(self, pages, *args, **extra_env):
        return subprocess.run(['node', str(TEAM / 'cloud-logs.cjs'), *args], capture_output=True, text=True,
                              env=self.env(pages, **extra_env))

    def calls(self):
        path = self.tmp / 'calls.txt'
        return path.read_text().splitlines() if path.exists() else []

    def run_show(self, *keys):
        return subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(self.out), *keys], capture_output=True, text=True)

    def assert_nothing_personal(self, text):
        for value in PERSONAL:
            self.assertNotIn(value, text)

    def assert_saved_privately(self):
        self.assertFalse(self.out.is_symlink())
        self.assertEqual(os.stat(self.out).st_mode & 0o777, 0o600)
        # 临时文件都换上去或删掉了
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), [self.out.name])

    @staticmethod
    def page(results, context=None, list_over=False):
        body = {'Response': {'LogResults': {'Results': results, 'Context': context, 'ListOver': list_over}}}
        return {'base_resp': {'ret': 0}, 'content': json.dumps(body, ensure_ascii=False)}

    def test_results_go_only_to_the_file(self):
        pages = [self.page([result(access_line())], 'c1'),
                 self.page([result(access_line(IP6)), result(f"Duplicate entry '{OPENID}'")], None, True)]
        # 原来就有一个别人也能读的旧文件：换成只有本人可读的新文件
        self.out.write_text('old')
        os.chmod(self.out, 0o644)
        run = self.run_logs(pages, *self.WINDOW, '--out', str(self.out))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_nothing_personal(run.stdout + run.stderr)
        summary = json.loads(run.stdout.splitlines()[0])
        # 终端只有条数、页数、路径（listOver、truncated 只在结果文件里）
        self.assertEqual(summary, {'count': 3, 'pages': 2, 'out': str(self.out.resolve())})
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(json.loads(json.loads(self.calls()[1])['postdata'])['Context'], 'c1')
        saved = json.loads(self.out.read_text())
        self.assertEqual((saved['count'], saved['pages'], len(saved['results'])), (3, 2, 3))
        self.assertIn(IP4, saved['results'][0]['Content'])
        self.assert_saved_privately()
        # 提示里是绝对路径，在仓库根目录以外照抄也能跑
        self.assertIn(f"python3 '{TEAM / 'cloud_logs_show.py'}' '{self.out.resolve()}'", run.stdout)
        show = self.run_show()
        self.assertEqual(show.returncode, 0, show.stderr)
        assert_clean(self, show.stdout + show.stderr)
        self.assertEqual(show.stdout.count('youju-api-054 GET /api/v1/search 200 35ms'), 2)
        self.assertIn('youju-api-054 -', show.stdout)

    def test_symlink_target_is_not_written(self):
        target = self.tmp / 'target.json'
        target.write_text('do not touch')
        self.out.symlink_to(target)
        run = self.run_logs([self.page([result(access_line())], None, True)], '--out', str(self.out), *self.WINDOW)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(target.read_text(), 'do not touch')
        self.assertEqual(json.loads(self.out.read_text())['count'], 1)
        self.assert_saved_privately()

    def test_usage_errors_do_not_query(self):
        window = self.WINDOW
        # 任何 git 仓库里都不让放：主仓库（.git 是目录）、worktree（.git 是文件）、上级目录是仓库、大小写不同的写法
        (self.tmp / 'repo/.git').mkdir(parents=True)
        (self.tmp / 'repo/sub').mkdir()
        (self.tmp / 'wt').mkdir()
        (self.tmp / 'wt/.git').write_text('gitdir: /somewhere/else\n')
        inside = [REPO / 'tools/tests/cloud-logs-should-not-exist.json', REPO / 'cloud-logs-should-not-exist.json',
                  self.tmp / 'repo/sub/logs.json', self.tmp / 'wt/logs.json',
                  Path(str(REPO).upper()) / 'cloud-logs-should-not-exist.json']
        for args in [window, [*window, '100'], ['--out', str(self.out), *window[:3]],
                     ['--out', str(self.tmp / 'missing/logs.json'), *window],
                     *(['--out', str(p), *window] for p in inside[:-2]),
                     [f'--out={inside[-2]}', *window], ['--out', str(inside[-1]), *window]]:
            with self.subTest(args=args):
                run = self.run_logs([self.page([result(access_line())], None, True)], *args)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertEqual(self.calls(), [])
                self.assert_nothing_personal(run.stdout + run.stderr)
        for p in inside:
            self.assertFalse(p.exists(), p)
        # 用法错误的提示里也是查看脚本的绝对路径
        run = self.run_logs([self.page([], None, True)], *window)
        self.assertIn(f"python3 '{TEAM / 'cloud_logs_show.py'}' <结果文件>", run.stdout)
        self.assertEqual(sorted(p.name for p in (self.tmp / 'repo/sub').iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.tmp / 'wt').iterdir()), ['.git'])

    def test_library_output_never_reaches_terminal(self):
        # 真库在 NODE_ENV=DEBUG 时把请求和回应原文 console.log 出来；假库还另外直接写终端、走 console 的各个出口
        pages = [self.page([result(access_line()), result(f"Duplicate entry '{OPENID}'")], None, True)]
        run = self.run_logs(pages, '--out', str(self.out), *self.WINDOW, NODE_ENV='DEBUG', DEBUG='*', FAKE_NOISY='1')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_nothing_personal(run.stdout + run.stderr)
        self.assertNotIn('fake-secret', run.stdout + run.stderr)
        self.assertEqual(run.stderr, '')
        self.assertEqual(json.loads(run.stdout.splitlines()[0])['count'], 2)
        self.assertEqual(len(run.stdout.splitlines()), 2)

    def test_closed_terminal_keeps_saved_results(self):
        # 终端提前关掉（接了 | head -1）：写不出提示不能变成异常，把存好的结果盖成出错信息
        env = self.env([self.page([result(access_line())], None, True)])
        for _ in range(5):
            proc = subprocess.Popen(['node', str(TEAM / 'cloud-logs.cjs'), '--out', str(self.out), *self.WINDOW],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            proc.stdout.close()
            err = proc.stderr.read().decode()
            proc.stderr.close()
            self.assertEqual(proc.wait(), 0, err)
            self.assert_nothing_personal(err)
            saved = json.loads(self.out.read_text())
            self.assertEqual((saved.get('count'), saved.get('error')), (1, None))
            self.assert_saved_privately()

    def test_late_error_after_saving_keeps_results(self):
        # 回应给完、结果存好以后连接才出错（异步抛出）：只退出，不能用出错信息盖掉存好的结果
        page = {**self.page([result(access_line())], None, True), 'lateThrow': {'message': f'read ECONNRESET {IP4}', 'code': 'ECONNRESET'}}
        run = self.run_logs([page], '--out', str(self.out), *self.WINDOW)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assert_nothing_personal(run.stdout + run.stderr)
        saved = json.loads(self.out.read_text())
        self.assertEqual((saved.get('count'), saved.get('error')), (1, None))
        self.assert_saved_privately()

    def test_uncaught_exception_in_library(self):
        pages = [{'asyncThrow': {'message': f'socket {IP4} uid {UID}', 'code': UID}}]
        run = self.run_logs(pages, '--out', str(self.out), *self.WINDOW)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assert_nothing_personal(run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout.splitlines()[0]), {'error': 'exception', 'out': str(self.out.resolve())})
        self.assertIn(IP4, json.loads(self.out.read_text())['message'])

    def test_long_error_message_is_kept_whole(self):
        # 原文截在 IP 或编号中间时，残缺的部分遮蔽时认不出来：结果文件里整段存下，显示时先遮再截
        for message, leaked in [('x' * 140 + f' uid {UID} tail', UID[:5]), ('x' * 145 + f' from {IP4} tail', '203.0.113')]:
            with self.subTest(leaked=leaked):
                error = {'base_resp': {'ret': 0}, 'content': json.dumps({'Response': {'Error': {'Code': 'X', 'Message': message}}})}
                run = self.run_logs([error], '--out', str(self.out), *self.WINDOW)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertEqual(json.loads(self.out.read_text())['message'], message)
                show = self.run_show()
                assert_clean(self, show.stdout + show.stderr)
                self.assertIn('出错原文在结果文件里，这里不显示', show.stdout)

    def test_errors_go_to_the_file_and_replace_old_results(self):
        def api_error(code):
            return {'base_resp': {'ret': 0}, 'content': json.dumps(
                {'Response': {'Error': {'Code': code, 'Message': f'bad request from {IP4} uid {UID}'}}})}
        cases = [([api_error('InvalidParameter')], 'api', {'code': 'InvalidParameter'}),
                 ([api_error(IP4)], 'api', {'code': IP4}),
                 ([{'base_resp': {'ret': -1}}], 'agent', {'ret': -1}),
                 ([{'base_resp': {'ret': IP6}}], 'agent', {'ret': IP6}),
                 ([{'base_resp': {'ret': 0}, 'content': f'not json {IP4}'}], 'exception', {'name': 'SyntaxError'}),
                 ([{'throw': {'message': f'connect to {IP4} failed', 'code': UID}}], 'exception', {'code': UID})]
        for pages, kind, detail in cases:
            with self.subTest(kind=kind, detail=detail):
                self.out.write_text(json.dumps({'count': 1, 'results': [result(access_line())]}))
                run = self.run_logs(pages, '--out', str(self.out), *self.WINDOW)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assert_nothing_personal(run.stdout + run.stderr)
                self.assertEqual(json.loads(run.stdout.splitlines()[0]), {'error': kind, 'out': str(self.out.resolve())})
                saved = json.loads(self.out.read_text())
                self.assertEqual(saved['error'], kind)
                self.assertEqual({k: saved.get(k) for k in detail}, detail)
                self.assertNotIn('results', saved)
                self.assert_saved_privately()
                show = self.run_show()
                self.assertEqual(show.returncode, 1)
                assert_clean(self, show.stdout + show.stderr)

    def test_write_failure_prints_only_the_error_name(self):
        self.out.mkdir()
        run = self.run_logs([self.page([result(access_line())], None, True)], '--out', str(self.out), *self.WINDOW)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout.splitlines()[0]), {'error': 'exception', 'write': 'EISDIR'})
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), [self.out.name])

    def test_stops_after_20_pages(self):
        run = self.run_logs([self.page([result(access_line())], 'more')], '--out', str(self.out), *self.WINDOW, '10')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout.splitlines()[0]),
                         {'count': 20, 'pages': 20, 'out': str(self.out.resolve())})
        self.assertIn('到了 20 页上限没取完', run.stdout.splitlines()[1])
        self.assertEqual(len(self.calls()), 20)
        self.assertEqual(json.loads(json.loads(self.calls()[0])['postdata'])['Limit'], 10)
        self.assertIn('到了 20 页上限没取完', self.run_show().stdout)


if __name__ == '__main__':
    unittest.main()
