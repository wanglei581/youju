"""读运行日志的两只工具：AI 成员默认看不到完整的来源 IP 和用户编号（影响评估第六节（二）第 6 条、（三）第 7 条）。

样本全是编的：IP 用文档专用地址段（RFC 5737、RFC 3849），编号和标识是随手写的；云托管接口用假模块代替，
不碰线上日志。
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
OPENID = 'oTEST' + 'x' * 23
RID = '0f' * 16
PERSONAL = (IP4, IP6, UID, OPENID, '113.77', '8a2e')


def access_line(ip=IP4, uid=UID):
    return json.dumps({'event': 'access', 'service': 'api', 'method': 'GET', 'path': '/api/v1/search', 'status': 200,
                       'ms': 35, 'ip': ip, 'rid': RID, 'at': '2026-10-19T03:00:00.123456+00:00', 'uid': uid,
                       'saved': True}, ensure_ascii=False)


def result(content, container='youju-api-054'):
    return {'Timestamp': '2026-10-19 11:00:00.000', 'TopicId': 't', 'Source': '10.0.0.5',
            'Content': json.dumps({'__CONTENT__': content, '__TAG__': {'container_name': container}}, ensure_ascii=False)}


FIELD_CASES = {
    f'{{"ip":"{IP4}","uid":"{UID}"}}': '{"ip":"203.0.*.*","uid":"9f3a*"}',
    f'{{\\"ip\\": \\"{IP4}\\", \\"uid\\": \\"{UID}\\"}}': '{\\"ip\\": \\"203.0.*.*\\", \\"uid\\": \\"9f3a*\\"}',
    f"{{'ip': '{IP6}', 'uid': '{UID}', 'saved': False}}": "{'ip': '2001:db8:*', 'uid': '9f3a*', 'saved': False}",
    '{"uid": "abc", "ip": null}': '{"uid": "*", "ip": null}',
    '{"ip": "203.0.113.77*x"}': '{"ip": "*"}',
}
TEXT_CASES = {
    'connect from 198.51.100.23:51234': 'connect from 198.51.*.*:51234',
    'client 192.0.2.1.': 'client 192.0.*.*.',
    '客户端 192.0.2.1。': '客户端 192.0.*.*。',
    'X-Original-Forwarded-For: 198.51.100.7, 203.0.113.9': 'X-Original-Forwarded-For: 198.51.*.*, 203.0.*.*',
    f'[{IP6}]:443': '[2001:db8:*]:443',
    'addr:2001:db8::1': 'addr:2001:db8:*',
    'peer 2001:DB8:0:1::1.': 'peer 2001:db8:*.',
    'IPv6 2001:db8:85a3:8d3:1319:8a2e:370:7348 end': 'IPv6 2001:db8:* end',
    # IPv4 映射成的 IPv6 按 IPv4 遮；其他嵌着 IPv4 的 IPv6 按 IPv6 遮
    'via ::ffff:203.0.113.9 ok': 'via ::ffff:203.0.*.* ok',
    'ip::ffff:203.0.113.9': 'ip::ffff:203.0.*.*',
    'nat64 64:ff9b::203.0.113.9 ok': 'nat64 64:ff9b:* ok',
    'embedded 2001:db8::203.0.113.9': 'embedded 2001:db8:*',
    'time=10:20:203.0.113.77': 'time=10:20:203.0.*.*',
    # 紧贴英文标签的 IPv6：标签照旧，地址留前两组；第一个冒号前有 4 位的按整串算，最多露两组
    'IPv6:2001:db8::1': 'IPv6:2001:db8:*',
    'ip6:2001:db8:85a3::1.': 'ip6:2001:db8:*.',
    'IPv6::1': 'IPv6:0:0:*',
    'IP2001:db8:85a3:1::1': 'IP2001:db8:*',
    'mycafe:2001:db8::1': 'mycafe:2001:*',
    f'删除 advisor/out/{UID}/12.xlsx 失败': '删除 advisor/out/9f3a*/12.xlsx 失败',
    f'youju log-uid -> {UID}': 'youju log-uid -> 9f3a*',
    f'cloud://prod-env.7072-prod-env-1300000000/advisor/out/{UID}/12.xlsx done': 'cloud://… done',
    f"Duplicate entry '{OPENID}' for key 'users.openid'": "Duplicate entry 'oTES*' for key 'users.openid'",
    # 日志内容本身是 JSON 文字时的反斜杠转义：先还原再认（转义里的字母、数字会紧贴在编号、IP 前面）
    '{"message":"rows\\n' + UID + '\\t' + OPENID + '"}': '{"message":"rows 9f3a* oTES*"}',
    'addr \\u5740203.0.113.77': 'addr 址203.0.*.*',
    '\\u53f7' + UID: '号9f3a*',
    '\\u57402001:db8::1': '址2001:db8:*',
    '\\u003e' + UID: '>9f3a*',
    'path\\\\n' + UID: 'path 9f3a*',
    '\\u0007' + UID + ' \\u005cn': ' 9f3a*  ',
    '\\u005cn' + UID: ' 9f3a*',
    '\\u005c\\u006e' + OPENID: ' oTES*',
    '\\x07' + UID: ' 9f3a*',
    '\\x5cn' + OPENID: ' oTES*',
}
UNTOUCHED = ['C:\\Program Files\\x', '\\"quoted\\"', f'rid {RID}', 'sha256 ' + 'ab' * 32, 'Python 3.12.4', '2026-10-19T03:00:00.123456+00:00',
             'elapsed 12:34:56', 'GET /api/v1/notices/123 200 35ms', '工具任务：processed 3',
             'Uvicorn running on http://[::]:80', 'mac aa:bb:cc:dd:ee:ff']
ACCESS_IPS = [(IP4, '203.0.*.*'), (IP6, '2001:db8:*'), ('::ffff:203.0.113.9', '::ffff:203.0.*.*'),
              ('testclient', '*'), ('', '')]


class MaskTests(unittest.TestCase):
    def test_access_line_keeps_json_and_masks_ip_and_uid(self):
        for ip, shown in ACCESS_IPS:
            with self.subTest(ip=ip):
                masked = json.loads(SHOW.mask(access_line(ip)))
                self.assertEqual(masked, {**json.loads(access_line(ip)), 'ip': shown, 'uid': '9f3a*'})

    def test_field_variants(self):
        for raw, want in FIELD_CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(SHOW.mask(raw), want)

    def test_ips_user_ids_and_file_ids_in_text(self):
        for raw, want in TEXT_CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(SHOW.mask(raw), want)

    def test_ordinary_content_is_untouched(self):
        for raw in UNTOUCHED:
            with self.subTest(raw=raw):
                self.assertEqual(SHOW.mask(raw), raw)

    def test_masking_twice_changes_nothing(self):
        for raw in [*FIELD_CASES, *TEXT_CASES, *UNTOUCHED, *(access_line(ip) for ip, _ in ACCESS_IPS)]:
            with self.subTest(raw=raw):
                once = SHOW.mask(raw)
                self.assertEqual(SHOW.mask(once), once)

    def test_long_runs_stay_fast(self):
        # 很长的十六进制串（比如日志里打了一段十六进制数据）不能让匹配来回试：20 万字也要瞬间完成
        started = time.monotonic()
        self.assertEqual(SHOW.mask('a' * 200000), 'a' * 200000)
        self.assertEqual(SHOW.mask('1:' * 50000), '1:' * 50000)
        self.assertLess(time.monotonic() - started, 2)

    def test_deep_escape_nesting_is_hidden_with_bounded_work(self):
        # 每层都可能再制造一个反斜杠，不能只解一层后把编号原样打印，也不能无限循环。
        raw = '\\' + 'u005c' * 10000 + 'n' + UID
        started = time.monotonic()
        self.assertEqual(SHOW.mask(raw), '[转义嵌套过深，内容已隐藏]')
        self.assertLess(time.monotonic() - started, 2)

    def test_windows_path_ids_are_masked_before_control_escape_decoding(self):
        # 路径分隔符后的 b/f 也是 UID 的首字母，不能被当成控制字符吞掉后漏出余下15位。
        for first in 'bf':
            uid = first + '1a2c3d4e5f60718'
            for raw in (
                'C:\\work\\' + uid + '\\1.xlsx',
                r'C:\work\u005c' + f'\\u{ord(first):04x}' + uid[1:] + r'\1.xlsx',
            ):
                with self.subTest(raw=raw):
                    shown = SHOW.mask(raw)
                    self.assertEqual(shown, 'C:\\work\\' + uid[:4] + '*\\1.xlsx')
                    self.assertNotIn(uid[1:], shown)
                    self.assertEqual(SHOW.mask(shown), shown)
        # 真正的控制字符后跟编号、IP，依然要解开边界后遮蔽。
        for escape in (r'\b', r'\f'):
            self.assertEqual(SHOW.mask(escape + UID), ' 9f3a*')
            self.assertEqual(SHOW.mask(escape + IP4), ' 203.0.*.*')

    def test_backslash_runs_without_escape_suffix_have_bounded_work(self):
        # 解码会制造长反斜杠串，下一轮不能从每个位置重新试整个后缀。
        # 独立进程有硬超时，旧规则退化时不会把测试本身无限卡住。
        code = (
            'import importlib.util, sys; '
            'spec = importlib.util.spec_from_file_location("show", sys.argv[1]); '
            'show = importlib.util.module_from_spec(spec); spec.loader.exec_module(show); '
            'slash = chr(92); '
            'assert show.mask((slash + "u005c") * 100000) == slash * 100000; '
            'assert show.mask(slash * 100000 + "q") == slash * 100000 + "q"; '
            'print("ok")'
        )
        run = subprocess.run(
            [sys.executable, '-B', '-c', code, str(TEAM / 'cloud_logs_show.py')],
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, 'ok\n', ''))

    def test_lines_mask_before_truncating_and_filter_on_masked_text(self):
        data = {'results': [result(access_line()), result('x' * 290 + f' {IP4} tail'), result(f'删除 advisor/out/{UID}/1.xlsx 失败'),
                            {'Timestamp': 't', 'Content': f'not json {IP4}'},
                            {'Timestamp': f'{IP4} uid={UID}', 'Content': json.dumps({'__CONTENT__': 'hello', '__TAG__': {'container_name': IP6}})},
                            result('第一行\r\n第二行'),
                            # 内容、时间不是字符串时转成文字会把换行写成 \n：要先遮再转
                            {'Timestamp': [f'x\n{UID}', '\x07' + UID], 'Content': json.dumps({'__CONTENT__': {'note': f'rows\n{UID}\t{OPENID}'}})},
                            # 内容是 JSON 文字、换行写成 \n；字典的键里有换行；str() 会写成 \x07 的控制字符
                            result('{"message":"rows\\n' + UID + '"}'),
                            {'Timestamp': 't', 'Content': json.dumps({'__CONTENT__': {f'x\n{UID}': 1, 'bell': '\x07' + UID, '\x07' + UID: 2}})}]}
        shown = list(SHOW.lines(data))
        self.assertEqual(len(shown), 9)
        for line in shown:
            for value in [*PERSONAL, '113', '\r', '\n']:
                self.assertNotIn(value, line)
        self.assertTrue(shown[0].startswith('2026-10-19 11:00:00.000 youju-api-054 {"event": "access"'))
        self.assertTrue(shown[1].endswith(' 203.0.*.*'))
        self.assertEqual(shown[3], 't ? not json 203.0.*.*')
        self.assertEqual(shown[4], '203.0.*.* uid=9f3a* 2001:db8:* hello')
        # 拿完整的 IP、编号当关键词逐个试，试不出来；中文关键词和遮蔽后的前缀照常能用
        self.assertEqual(list(SHOW.lines(data, [IP4, UID, IP6])), [])
        self.assertEqual(len(list(SHOW.lines(data, ['失败']))), 1)
        self.assertEqual(len(list(SHOW.lines(data, ['203.0.']))), 3)

    def test_main_masks_summary_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'logs.json'
            for data, code in [({'count': IP4, 'listOver': UID, 'pages': 1, 'results': [result(access_line())]}, 0),
                               ({'count': {'a': f'x\n{UID}', '\x07' + UID: 1}, 'listOver': [f'y\t{OPENID}', '\x07' + OPENID], 'pages': f'{IP4}', 'results': []}, 0),
                               ({'error': 'api', 'code': IP4, 'message': f'bad request from {IP6} uid {UID}'}, 1),
                               # 转成 JSON 后换行、制表符成了 \n、\t，紧跟的编号前面就是字母：要先遮再转
                               ({'error': 'api', 'message': f'rows\n{UID}\t{OPENID}', 'ret': [f'x\n{IP4}']}, 1),
                               ({'error': 'exception', 'message': f'{IP4} {UID} ' * 500}, 1)]:
                with self.subTest(data=str(data)[:80]):
                    path.write_text(json.dumps(data))
                    run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(path)], capture_output=True, text=True)
                    self.assertEqual(run.returncode, code, run.stderr)
                    for value in PERSONAL:
                        self.assertNotIn(value, run.stdout)
                    self.assertIn('203.0.*.*', run.stdout)
                    # 很长的出错原文：遮蔽之后再限长
                    self.assertLessEqual(len(run.stdout), 2001 if code else 10000)
            # 原样截到 2000 字正好停在 IP 的第三段后面：必须先遮再截
            prefix = json.dumps({'error': 'exception', 'message': ''}, ensure_ascii=False)[:-2]
            path.write_text(json.dumps({'error': 'exception', 'message': 'y' * (2000 - len(prefix) - 9) + f'{IP4} tail'}))
            run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(path)], capture_output=True, text=True)
            self.assertNotIn('203.0.113', run.stdout)
            path.write_text('[1, 2]')
            run = subprocess.run([sys.executable, str(TEAM / 'cloud_logs_show.py'), str(path)], capture_output=True, text=True)
            self.assertEqual((run.returncode, run.stderr), (1, ''))


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
        self.assert_nothing_personal(show.stdout)
        self.assertIn('"ip": "203.0.*.*"', show.stdout)
        self.assertIn('"ip": "2001:db8:*"', show.stdout)
        self.assertIn('"uid": "9f3a*"', show.stdout)
        self.assertIn("Duplicate entry 'oTES*'", show.stdout)

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
                self.assertNotIn(leaked, show.stdout)
                self.assertIn('9f3a*' if leaked == UID[:5] else '203.0.*.*', show.stdout)

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
                self.assert_nothing_personal(show.stdout)

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
